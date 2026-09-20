"""JSON-lines bridge from the Node worker to the real SwarmFlow runtime."""
import asyncio
import hashlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

from runtime import ROOT, load_engine

engine = load_engine()


def emit(kind, **payload):
    print(json.dumps({"kind": kind, **payload}, ensure_ascii=False), flush=True)


class ModelBackend(engine.AgentBackend):
    """Each framework agent has its own prompt, role, and validated output.

    The framework owns scheduling, retries, handoffs and the journal. This
    adapter owns provider transport and usage accounting, per AgentBackend.
    """
    def __init__(self, max_calls=200, directory=None):
        super().__init__()
        self.directory = directory
        self.calls = 0
        self.max_calls = max_calls
        self.catalog = json.loads((ROOT / "config/research-models.json").read_text(encoding="utf-8"))
        self.model = self.catalog["defaultModel"]

    def route(self, selection):
        entry = next((item for item in self.catalog["models"] if item["id"] == selection), None)
        if not entry or entry.get("disabled"):
            raise RuntimeError("Unknown or unavailable model selection")
        key_name = entry["keyEnv"]
        key = os.environ.get(key_name, "")
        base = os.environ.get(entry["provider"].upper() + "_BASE_URL") or entry["baseUrl"]
        model = entry["model"]
        if not key or not model:
            raise RuntimeError(f"Configure {key_name} and the selected model in the worker environment")
        if entry and entry["provider"] == "devin" and not os.environ.get("DEVIN_ORG_ID"):
            raise RuntimeError("Configure DEVIN_ORG_ID for Devin role assignments")
        return key, base.rstrip("/"), model, entry["provider"]

    def request(self, payload, route):
        key, base, _, _ = route
        request = urllib.request.Request(base + "/chat/completions",
            data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key,
                                                       "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=110) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Model endpoint returned HTTP {error.code}") from None
        except urllib.error.URLError:
            raise RuntimeError("Model endpoint could not be reached") from None

    async def run(self, prompt, opts, schema_json, *, call_key=None):
        if self.calls >= self.max_calls or self.budget.exhausted:
            raise RuntimeError("Model-call/token budget exhausted")
        self.calls += 1  # No await before reservation; safe across parallel tasks.
        if len(prompt) > 100000:
            raise RuntimeError("Agent context exceeds the configured input limit")
        route = self.route(opts.get("model") or self.model)
        if route[3] == "devin":
            from devin_backend import run_session
            if self.directory is None:
                raise RuntimeError("Devin requires a persistent episode directory")
            output = await run_session(prompt, schema_json, route, self.directory, call_key, emit)
            # Devin reports ACUs, not tokens; never fabricate a token estimate.
            return engine.AgentResult(structured=output, tokens=0)
        payload = {
            "model": route[2],
            "messages": [{"role": "system", "content": "You are a specialized mathematical research agent. "
                          "Return one JSON object matching this schema exactly: " + json.dumps(schema_json)},
                         {"role": "user", "content": prompt}],
            "max_completion_tokens": int(os.environ.get("SWARM_MAX_OUTPUT_TOKENS", "4096")),
        }
        if route[3] in {"gemini", "qwen", "deepseek"}:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        result = await asyncio.to_thread(self.request, payload, route)
        usage = result.get("usage", {}).get("total_tokens")
        if not isinstance(usage, int) or usage < 0:
            raise RuntimeError("Provider omitted token usage; cannot enforce the token budget")
        self.budget.add(usage)
        if self.workflow_budget is not None:
            self.workflow_budget.add(usage)
        emit("usage", tokens=usage, calls=self.calls, model=payload["model"])
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise RuntimeError("Provider returned no text output")
        content = content.strip()
        if content.startswith("```json") and content.endswith("```"):
            content = content[7:-3].strip()
        return engine.AgentResult(structured=json.loads(content), tokens=usage)


async def execute(args, backend=None, progress=None):
    import re
    catalog = json.loads((ROOT / "config/research-models.json").read_text(encoding="utf-8"))
    if args.get("role_models") is None:
        args = {**args, "role_models": {role["id"]: catalog["defaultModel"] for role in catalog["roles"]}}
    legacy = args.get("role_models")
    if isinstance(legacy, dict) and set(legacy) == {"coordinator", "researcher", "challenger", "critic", "proof_writer"}:
        args = {**args, "role_models": {"coordinator": legacy["coordinator"], "researcher_1": legacy["researcher"],
                "researcher_2": legacy["challenger"], "researcher_3": legacy["researcher"],
                "challenger": legacy["critic"], "proof_writer": legacy["proof_writer"]}}
    episode_id = args.get("episode_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", episode_id):
        raise ValueError("Invalid episode_id")
    directory = ROOT / ".data" / "swarm-runs" / episode_id
    directory.mkdir(parents=True, exist_ok=True)
    args_path = directory / "input.json"
    # Episode IDs bind a journal to immutable inputs; a changed goal needs a new run.
    if args_path.exists() and json.loads(args_path.read_text(encoding="utf-8")) != args:
        raise ValueError("Episode inputs changed; create a new episode")
    args_path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
    journal = directory / "journal.json"
    workflow = ROOT / "swarm-skills" / "math-research" / "scripts" / "workflow.py"
    fingerprint = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(workflow.parent.glob("*.py")))).hexdigest()
    version_path = directory / "workflow.sha256"
    if journal.exists() and (not version_path.exists() or version_path.read_text() != fingerprint):
        raise ValueError("Workflow changed; create a new episode instead of replaying an incompatible journal")
    version_path.write_text(fingerprint)
    if backend is None:
        backend = ModelBackend(directory=directory)
        selections = args.get("role_models")
        if selections is not None:
            expected = {role["id"] for role in backend.catalog["roles"]}
            if not isinstance(selections, dict) or set(selections) != expected:
                raise ValueError("Choose a model for every research role")
            allowed = {model["id"] for model in backend.catalog["models"] if not model.get("disabled")}
            for selection in selections.values():
                if selection not in allowed:
                    raise ValueError("Unknown or unavailable role model")
                backend.route(selection)  # Preflight every role before spending tokens.
    if args.get("retrieval_bridge"):
        from retrieval import Retrieval
        sys.modules["triviality_retrieval"] = Retrieval(directory, emit)
    # Snapshot domain events before forwarding them. A budget stop is terminal,
    # but must not erase the completed prefix of the exploration.
    snapshot = dict(status="candidate", reports=[], branches=[], discoveries=[],
                    literature=args.get("literature", []), plan=None, proof=None,
                    target_origin="user" if args.get("lean_statement", "").strip() else "model")

    def on_progress(event):
        message = event.message or ""
        if message.startswith("TRIVIALITY_EVENT "):
            domain = json.loads(message.removeprefix("TRIVIALITY_EVENT "))
            kind = domain["kind"]
            if kind in {"branch", "discovery"}:
                key, item = ("branches", domain["branch"]) if kind == "branch" else ("discoveries", domain["entry"])
                items = {value["id"]: value for value in snapshot[key]}
                items[item["id"]] = item
                snapshot[key] = list(items.values())
            elif kind == "plan":
                snapshot["plan"] = domain["plan"]
            elif kind == "verification":
                snapshot["proof"] = domain["proof"]
            elif kind == "literature":
                papers = {p["id"]: p for p in snapshot["literature"]}
                papers.update({p["id"]: p for p in domain.get("papers", [])})
                snapshot["literature"] = list(papers.values())
        if progress:
            progress(event)
        else:
            emit("progress", event=asdict(event))

    try:
        result = await engine.run_workflow(str(workflow), args=args, backend=backend,
            resume=str(journal), journal_path=str(journal), run_id=episode_id, cap=3,
            budget=engine.BudgetLedger(total=int(os.environ.get("SWARM_TOKEN_BUDGET", "60000"))),
            progress_sink=on_progress,
            log_sink=lambda message: emit("log", message=message))
    except engine.BudgetExhausted as error:
        result = {**snapshot, "reports": [b.get("report") for b in snapshot["branches"]],
                  "stop_reason": "token_budget", "token_usage": {"spent": error.spent, "limit": error.total},
                  "summary": f"Token budget reached ({error.spent}/{error.total}). Research stopped without a verified solution. "
                             "Completed findings, challenges and proof attempts are preserved."}
    finally:
        sys.modules.pop("triviality_retrieval", None)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("proof") and result["proof"].get("lean"):
        (directory / "Proof.lean").write_text(result["proof"]["lean"], encoding="utf-8")
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    try:
        args = json.loads(sys.stdin.readline())
        emit("result", result=asyncio.run(execute(args)))
    except Exception as error:
        emit("error", message=str(error))
        sys.exit(1)
