import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from runtime import load_engine
engine = load_engine()
WORKFLOW = ROOT / "swarm-skills/math-research/scripts/workflow.py"
spec = importlib.util.spec_from_file_location("lean_check", WORKFLOW.with_name("lean_check.py"))
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

TARGET = "(a b c : Nat) (h : a ≤ b) : a + c ≤ b + c"
REAL_LEAN_AVAILABLE = Path(checker.lean_executable()).is_file()


class FixtureBackend(engine.AgentBackend):
    """Scripted agents for deterministic tests, never used by the live runner."""
    def __init__(self, *, stop=False, fail_researcher=False, bad_first_proof=False):
        super().__init__()
        self.prompts = []
        self.active = 0
        self.max_active = 0
        self.stop = stop
        self.fail_researcher = fail_researcher
        self.bad_first_proof = bad_first_proof
        self.proofs = 0

    async def run(self, prompt, opts, schema_json, *, call_key=None):
        self.prompts.append(prompt)
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if self.fail_researcher and prompt.startswith("Investigate your independent") and json.loads(prompt.split("Branch context: ")[1])["assignment"] == "skeptical route":
            raise RuntimeError("Simulated researcher outage")
        fields = schema_json["properties"]
        if "tasks" in fields:
            data = {"tasks": ["constructive route", "skeptical route", "induction route"], "formal_statement": TARGET, "rationale": "Independent approaches"}
        elif "approach" in fields:
            data = {"approach": "Monotonicity", "evidence": "Addition preserves natural-number order.",
                    "risks": "Check domain and direction.", "next_step": "Apply omega.", "source_ids": [],
                    "discovery_ids": [], "search_query": "natural number order", "candidate_complete": True}
        elif "resolution_test" in fields:
            ready = not self.stop and '"previous_report": null' not in prompt
            data = {"claim": "Order preservation", "feedback": "Address the natural-number domain explicitly.",
                    "evidence": "A concrete natural-number argument is required.", "resolution_test": "Check zero and the inductive step.",
                    "foundation_refuted": self.stop, "made_progress": ready, "target_aligned": not self.stop,
                    "ready_for_proof": ready, "unresolved": [] if ready else ["Domain argument missing"], "alternative_query": "order induction"}
        elif "assignment" in fields:
            data = {"assignment": "fresh induction approach", "paper_id": "none", "rationale": "Avoid the failed foundation",
                    "avoided_failure": "Do not reuse unsupported assumptions"}
        else:
            self.proofs += 1
            data = {"proof": "by\n  rfl" if self.bad_first_proof and self.proofs == 1 else "by\n  omega",
                    "explanation": "Arithmetic monotonicity."}
        self.budget.add(10)
        if self.workflow_budget is not None:
            self.workflow_budget.add(10)
        return engine.AgentResult(structured=data, tokens=10)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_stop_preserves_completed_work(self):
        from runner import execute
        for limit in [0, 45, 65]:
            with self.subTest(limit=limit), patch.dict(os.environ, {"SWARM_TOKEN_BUDGET": str(limit)}):
                backend = FixtureBackend()
                result = await execute({"episode_id": "fixture-budget-" + uuid.uuid4().hex,
                                        "statement": "Order preservation", "lean_statement": TARGET},
                                       backend=backend, progress=lambda event: None)
                self.assertEqual(result["stop_reason"], "token_budget")
                self.assertEqual(result["status"], "candidate")
                self.assertFalse((result.get("proof") or {}).get("verified", False))
                self.assertGreaterEqual(result["token_usage"]["spent"], limit)
                if limit:
                    self.assertEqual(len(result["reports"]), 3)
                    self.assertTrue(all(result["reports"]))
                    self.assertTrue(result["discoveries"])

    async def run_flow(self, backend, **extra):
        args = {"statement": "Adding the same natural number preserves order.", "lean_statement": TARGET, "proof_attempts": 2, **extra}
        events = []
        result = await engine.run_workflow(str(WORKFLOW), args=args, backend=backend,
            cap=3, progress_sink=events.append, budget=engine.BudgetLedger(total=5000))
        return result, events

    async def test_reports_critique_and_revision_are_consumed(self):
        backend = FixtureBackend()
        with patch.dict(os.environ, {"SWARM_LEAN_BIN": "nonexistent-lean-for-test"}):
            result, events = await self.run_flow(backend)
        self.assertEqual(result["status"], "candidate")
        self.assertFalse(result["proof"]["verified"])
        self.assertEqual(result["stop_reason"], "checker_unavailable")
        self.assertEqual(backend.proofs, 1)
        self.assertEqual(backend.max_active, 3)
        self.assertTrue(any('"shared_discoveries": [{"id"' in p for p in backend.prompts))
        writer = next(p for p in backend.prompts if p.startswith("Write a Lean"))
        self.assertIn("Addition preserves natural-number order", writer)
        self.assertIn("Address the natural-number domain", writer)
        self.assertTrue(any(e.message and '"kind": "discovery"' in e.message for e in events))

    async def test_critic_can_stop_proof_work(self):
        backend = FixtureBackend(stop=True)
        result, _ = await self.run_flow(backend)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(backend.proofs, 0)
        self.assertTrue(any(d["kind"] == "abandoned" for d in result["discoveries"]))
        self.assertTrue(all(b["generation"] > 0 for b in result["branches"]))

    async def test_failed_researcher_is_reassigned(self):
        backend = FixtureBackend(fail_researcher=True, stop=True)
        with patch.dict(os.environ, {"SWARM_LEAN_BIN": "nonexistent-lean-for-test"}):
            result, events = await self.run_flow(backend)
        self.assertTrue(all(result["reports"]))
        self.assertTrue(any(p.startswith("Assign a fresh") for p in backend.prompts))
        self.assertTrue(any(e.message and '"kind": "reassignment"' in e.message for e in events))

    async def test_journal_resume_reuses_completed_agents(self):
        args = {"statement": "Order preservation", "lean_statement": TARGET}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"SWARM_LEAN_BIN": "nonexistent-lean-for-test"}):
            journal = str(Path(temp) / "journal.json")
            first = FixtureBackend(stop=True)
            await engine.run_workflow(str(WORKFLOW), args=args, backend=first, journal_path=journal, run_id="resume-test")
            second = FixtureBackend(stop=True)
            result = await engine.run_workflow(str(WORKFLOW), args=args, backend=second, resume=journal, run_id="resume-test")
            self.assertEqual(result["status"], "candidate")
            self.assertEqual(second.prompts, [])

    async def test_iteration_limit_and_stagnation_restart(self):
        backend = FixtureBackend()
        original = backend.run
        async def stuck(prompt, opts, schema_json, **kwargs):
            response = await original(prompt, opts, schema_json, **kwargs)
            if "resolution_test" in schema_json["properties"]:
                response.structured.update(made_progress=False, ready_for_proof=False, unresolved=["Unanswered gap"])
            return response
        backend.run = stuck
        result, _ = await self.run_flow(backend, exploration_rounds=3, stagnation_threshold=2)
        self.assertEqual(backend.proofs, 0)
        self.assertEqual(len([p for p in backend.prompts if p.startswith("Investigate your independent")]), 9)
        self.assertEqual([b["generation"] for b in result["branches"]], [1, 1, 1])
        restarted = [json.loads(p.split("Branch context: ")[1]) for p in backend.prompts if p.startswith("Investigate your independent")][-3:]
        self.assertTrue(all(c["own_previous_report"] is None and c["challenge"] is None for c in restarted))
        self.assertTrue(all(c["failure_memory"] for c in restarted))
        self.assertTrue(any(d["kind"] == "finding" and d["status"] == "abandoned" for d in result["discoveries"]))

    async def test_success_stops_early_and_preserves_parallel_findings(self):
        backend = FixtureBackend()
        passed = subprocess.CompletedProcess([], 0, "'triviality_target' does not depend on any axioms", "")
        with patch("subprocess.run", return_value=passed):
            result, _ = await self.run_flow(backend, exploration_rounds=10)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len([p for p in backend.prompts if p.startswith("Investigate your independent")]), 6)
        self.assertEqual(len([d for d in result["discoveries"] if d["kind"] == "finding"]), 6)
        self.assertEqual(result["proof"]["statement"], TARGET)

    async def test_model_generated_target_needs_review_with_successful_checker(self):
        passed = subprocess.CompletedProcess([], 0, "'triviality_target' does not depend on any axioms", "")
        with patch("subprocess.run", return_value=passed):
            result, _ = await self.run_flow(FixtureBackend(), lean_statement="")
        self.assertEqual(result["status"], "formalized")

    async def test_checker_failure_is_feedback_not_foundation_refutation(self):
        failed = subprocess.CompletedProcess([], 1, "error: tactic failed", "")
        with patch("subprocess.run", return_value=failed):
            result, _ = await self.run_flow(FixtureBackend(), exploration_rounds=3, proof_attempts=1)
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(all(b["generation"] == 0 for b in result["branches"]))
        self.assertTrue(any(b["report"] for b in result["branches"]))

    @unittest.skipUnless(REAL_LEAN_AVAILABLE, "Real Lean toolchain not installed")
    async def test_real_lean_failure_repair_and_exact_target(self):
        backend = FixtureBackend(bad_first_proof=True)
        result, _ = await self.run_flow(backend)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(backend.proofs, 2)
        self.assertIn("Lean rejected", backend.prompts[-1])
        self.assertEqual(result["proof"]["statement"], TARGET)

    @unittest.skipUnless(REAL_LEAN_AVAILABLE, "Real Lean toolchain not installed")
    async def test_generated_target_is_not_claimed_as_solved(self):
        result, _ = await self.run_flow(FixtureBackend(), lean_statement="")
        self.assertEqual(result["status"], "formalized")
        self.assertTrue(result["proof"]["verified"])


class VerificationTests(unittest.TestCase):
    @unittest.skipUnless(REAL_LEAN_AVAILABLE, "Real Lean toolchain not installed")
    def test_blank_override_compiles_with_installed_lean(self):
        for value in ["", "   "]:
            with self.subTest(value=value), patch.dict(os.environ, {"SWARM_LEAN_BIN": value}):
                result = checker.check(TARGET, "by omega")
                self.assertTrue(result["verified"], result)
                self.assertIn("Lean checked", result["checker"])

    @unittest.skipUnless(REAL_LEAN_AVAILABLE, "Real Lean toolchain not installed")
    def test_lean_rejects_false_theorem(self):
        result = checker.check(": False", "by trivial")
        self.assertFalse(result["verified"])
        self.assertEqual(result["checker"], "Lean rejected the candidate")
        self.assertIn("error", result["log"])

    def test_invalid_explicit_executable_does_not_fallback(self):
        result = checker.check(TARGET, "by omega", executable="nonexistent-lean-for-test")
        self.assertFalse(result["verified"])
        self.assertIn("Lean unavailable", result["checker"])

    def test_timeout_fails_closed(self):
        with patch.object(checker.subprocess, "run", side_effect=subprocess.TimeoutExpired("lean", 30)):
            result = checker.check(TARGET, "by omega")
        self.assertFalse(result["verified"])
        self.assertIn("timed out", result["checker"])

    def test_rejects_unsafe_and_missing_proofs(self):
        for proof in ["by sorry", "by admit", "by native_decide", "by run_tac pure ()", "by\n exact h\n#eval 1", ""]:
            with self.subTest(proof=proof):
                self.assertFalse(checker.check(TARGET, proof)["verified"])

    def test_missing_audit_fails_closed(self):
        with patch.object(checker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = checker.check(TARGET, "by omega")
        self.assertFalse(result["verified"])
        self.assertIn("no recognizable axiom audit", result["checker"])

    def test_disallowed_transitive_axiom_fails(self):
        output = "'triviality_target' depends on axioms: [sorryAx]"
        with patch.object(checker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.assertFalse(checker.check(TARGET, "by omega")["verified"])


class BridgeTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "apps/research-worker/dist/swarm.js").exists(), "Build research-worker before bridge test")
    def test_node_python_model_transport_and_progress(self):
        fixture = FixtureBackend(bad_first_proof=True)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                output_schema = json.loads(body["messages"][0]["content"].split("exactly: ", 1)[1])
                result = asyncio.run(fixture.run(body["messages"][1]["content"], {}, output_schema))
                payload = json.dumps({"choices": [{"message": {"content": json.dumps(result.structured)}}],
                                      "usage": {"total_tokens": 10}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "OPENAI_API_KEY": "test-fixture-only",
               "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1", "SWARM_PYTHON": sys.executable}
        args = {"episode_id": "fixture-bridge-" + uuid.uuid4().hex[:10], "statement": "Adding a natural number preserves order.",
                "lean_statement": TARGET, "proof_attempts": 2}
        try:
            run = subprocess.run(["node", str(HERE / "tests/bridge-smoke.mjs"), json.dumps(args)],
                                 cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
            self.assertEqual(run.returncode, 0, run.stderr)
            output = json.loads(run.stdout)
            self.assertIn(output["result"]["status"], ["verified", "candidate"])
            if REAL_LEAN_AVAILABLE:
                self.assertEqual(output["result"]["status"], "verified", output["result"])
                self.assertEqual(fixture.proofs, 2)
            self.assertTrue(any(e["kind"] == "usage" for e in output["events"]))
            self.assertTrue(any(e.get("event", {}).get("kind") == "agent_completed" for e in output["events"]))
            self.assertGreater(len(output["searches"]), 0)
            self.assertTrue(any(p["id"] == "fixture-paper" for p in output["result"]["literature"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
