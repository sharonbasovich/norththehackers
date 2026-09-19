"""The pool must change actual scheduling and evidence reuse, not just worker wording."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_key
from app.db import SessionLocal
from app.deps import get_scheduler
from app.models import Attempt, Campaign, Claim, Evidence, Relation, SelectionDecision
from app.services.lean_checker import LeanChecker, LeanCheckResult
from app.services.research import (
    claim_state,
    dependency_gaps,
    frontier,
    idea_status,
    memory,
    pool_campaigns,
    record_link,
)
from tests.conftest import OWNER
from tests.test_research_loop import _run_ticks


def _pool(client: TestClient, suffix: str = "", budget: int = 30) -> tuple[str, list[str]]:
    portfolio = client.post(
        "/private/portfolios",
        headers=OWNER,
        json={"name": "shared" + suffix, "max_concurrent_sessions": 3},
    ).json()["id"]
    problems = []
    for name in ("alpha", "beta"):
        response = client.post(
            "/private/problems",
            headers=OWNER,
            json={
                "slug": name + suffix,
                "title": name + suffix,
                "statement": "Finite integer sums have a structural decomposition.",
                "sources": [{"title": "test fixture", "url": "https://example.com/fixture"}],
            },
        )
        assert response.status_code == 200, response.text
        problems.append(response.json()["id"])
    response = client.post(
        f"/private/portfolios/{portfolio}/research",
        headers=OWNER,
        json={"problem_ids": problems, "session_budget_per_problem": budget},
    )
    assert response.status_code == 200, response.text
    return portfolio, response.json()["created_campaign_ids"]


def _submit(db: Session, campaign: Campaign, output: dict, role: str = "synthesizer") -> Attempt:
    scheduler = get_scheduler()
    attempt = scheduler.enqueue(db, campaign, role=role, idea=None, parents=[], mode="ultra")
    attempt.status = "completed"
    scheduler.ingestor.ingest(db, attempt, output)
    db.flush()
    return attempt


def _claim(db: Session, campaign: Campaign, statement: str = "Shared integer lemma") -> Claim:
    _submit(
        db,
        campaign,
        {
            "ideas": [
                {
                    "title": statement,
                    "approach": "shared reduction",
                    "claims": [
                        {"statement": statement, "lean_declaration": "theorem shared : 1 + 1 = 2"}
                    ],
                }
            ],
            "evidence": [],
            "gaps": [],
        },
        role="hypothesis_generator",
    )
    claim = db.scalar(
        select(Claim).where(Claim.campaign_id == campaign.id, Claim.statement == statement)
    )
    assert claim is not None
    return claim


def test_bulk_pool_is_atomic_and_does_not_reset_budgets(client: TestClient) -> None:
    portfolio, ids = _pool(client)
    with SessionLocal() as db:
        campaigns = [db.get(Campaign, cid) for cid in ids]
        problems = [c.problem_id for c in campaigns if c is not None]
    again = client.post(
        f"/private/portfolios/{portfolio}/research",
        headers=OWNER,
        json={"problem_ids": problems, "session_budget_per_problem": 999},
    ).json()
    assert again["created_campaign_ids"] == []
    assert all(
        c["session_budget"] == 30 for c in client.get("/private/campaigns", headers=OWNER).json()
    )
    bad = client.post(
        f"/private/portfolios/{portfolio}/research",
        headers=OWNER,
        json={"problem_ids": [problems[0], "missing"]},
    )
    assert bad.status_code == 404
    assert len(client.get("/private/campaigns", headers=OWNER).json()) == 2


def test_pool_loop_explores_transfers_and_proves_shared_claims(client: TestClient) -> None:
    portfolio, ids = _pool(client)
    _run_ticks(client, 50)
    attempts = [
        a
        for cid in ids
        for a in client.get(f"/private/campaigns/{cid}/attempts", headers=OWNER).json()
    ]
    assert {"synthesizer", "connection_reviewer", "lemma_prover"} <= {a["role"] for a in attempts}
    pool = client.get(f"/private/portfolios/{portfolio}/research", headers=OWNER).json()
    assert len(pool["problems"]) == 2
    adopted = [r for r in pool["links"] if r["kind"] == "applies_to" and r["status"] == "adopted"]
    assert len({r["target"] for r in adopted}) == 2
    assert all(c["state"] != "lean_verified" for c in pool["claims"])
    assert any(e["check_type"] == "lean_check" for c in pool["claims"] for e in c["evidence"])
    assert all(
        c["sessions_used"] <= c["session_budget"]
        for c in client.get("/private/campaigns", headers=OWNER).json()
    )
    # Shared links are visible in the public graph, with their research status.
    assert any(
        r["kind"] == "applies_to" and r["status"] == "adopted"
        for r in client.get("/public/graph").json()["links"]
    )


def test_adoption_and_rejection_are_persistent_decisions_not_proofs(client: TestClient) -> None:
    _, ids = _pool(client)
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        assert a and b
        claim = _claim(db, a)
        assert claim.idea
        claim.idea.scheduling_status = "archived"
        output = {
            "ideas": [],
            "evidence": [],
            "gaps": [],
            "research_links": [
                {
                    "claim": claim.id,
                    "target": b.problem_id,
                    "kind": "applies_to",
                    "status": "adopted",
                    "reason": "The same lemma reduces beta's bottleneck.",
                    "assumptions": ["finite domain"],
                }
            ],
        }
        _submit(db, b, output, "connection_reviewer")
        assert claim_state(claim) == "untested"
        assert claim.idea.scheduling_status == "active"
        output["research_links"][0].update(status="rejected", reason="Quantifiers do not match.")
        _submit(db, b, output, "connection_reviewer")
        link = db.scalar(select(Relation).where(Relation.kind == "applies_to"))
        assert link and link.status == "rejected" and len(link.provenance["history"]) == 2
        assert not any(
            t.role == "connection_reviewer" and t.metadata.get("relation_id") == link.id
            for t in frontier(db, [a, b])
        )
        context = memory(db, b)
        assert any(c["id"] == claim.id for c in context["claims"])
        assert any(r["status"] == "rejected" for r in context["links"])


def test_claim_and_artifact_access_stays_inside_pool(client: TestClient) -> None:
    _, ids = _pool(client)
    _, foreign_ids = _pool(client, "-foreign")
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        foreign = db.get(Campaign, foreign_ids[0])
        assert a and b and foreign
        shared = _claim(db, a)
        secret = _claim(db, foreign, "Foreign claim")
        output = {
            "ideas": [],
            "gaps": [],
            "evidence": [
                {
                    "target": shared.id,
                    "check_type": "numerical_experiment",
                    "result": "supports",
                    "summary": "Finite enumeration",
                    "artifact": {"filename": "test.py", "content": "print(2)"},
                },
                {
                    "target": secret.id,
                    "check_type": "numerical_experiment",
                    "result": "supports",
                    "summary": "Unauthorized foreign write",
                },
            ],
            "research_links": [
                {
                    "claim": secret.id,
                    "target": b.problem_id,
                    "kind": "applies_to",
                    "status": "adopted",
                    "reason": "should be refused",
                }
            ],
        }
        attempt = _submit(db, b, output)
        assert attempt.result and len(attempt.result["unattached_evidence"]) == 1
        assert len(attempt.result["rejected_research_links"]) == 1
        assert not secret.evidence
        evidence = db.scalar(select(Evidence).where(Evidence.claim_id == shared.id))
        assert evidence and evidence.artifact_id
        artifact_id = evidence.artifact_id
        attempt.worker_token_hash = hash_key("pool-token")
        attempt.status = "running"
        outsider = _submit(db, foreign, {"ideas": [], "evidence": [], "gaps": []})
        outsider.worker_token_hash = hash_key("outsider-token")
        outsider.status = "running"
        db.commit()
        shared_url = f"/worker/attempts/{attempt.id}/artifacts/{artifact_id}"
        foreign_url = f"/worker/attempts/{outsider.id}/artifacts/{artifact_id}"
    assert (
        client.get(shared_url, headers={"X-Worker-Token": "pool-token"}).json()["content"]
        == "print(2)"
    )
    assert client.get(foreign_url, headers={"X-Worker-Token": "outsider-token"}).status_code == 404


def test_dependency_gaps_prioritize_shared_leaf_and_detect_cycles(client: TestClient) -> None:
    _, ids = _pool(client)
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        assert a and b
        parent, leaf = _claim(db, a, "Parent theorem"), _claim(db, a, "Leaf lemma")
        for c in (a, b):
            record_link(
                db,
                source=parent,
                target_id=c.problem_id,
                kind="applies_to",
                status="adopted",
                reason="needed",
            )
        dependency = record_link(
            db, source=parent, target_id=leaf.id, kind="depends_on", status="proposed", reason="gap"
        )
        tasks = frontier(db, [a, b])
        assert any(
            t.role == "lemma_prover" and t.metadata.get("focus_claim_id") == leaf.id for t in tasks
        )
        assert not any(
            t.role == "lemma_prover" and t.metadata.get("focus_claim_id") == parent.id
            for t in tasks
        )
        reverse = record_link(
            db,
            source=leaf,
            target_id=parent.id,
            kind="depends_on",
            status="proposed",
            reason="circular",
        )
        gaps = dependency_gaps(parent.id, {parent.id: parent, leaf.id: leaf}, [dependency, reverse])
        assert gaps["cycles"]
        assert any(t.role == "lemma_architect" for t in frontier(db, [a, b]))


def test_counterevidence_survives_prior_support_and_is_prioritized(client: TestClient) -> None:
    _, ids = _pool(client)
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        assert a and b
        claim = _claim(db, a)
        for result in ("supports", "refutes"):
            _submit(
                db,
                b,
                {
                    "ideas": [],
                    "gaps": [],
                    "evidence": [
                        {
                            "target": claim.id,
                            "check_type": "counterexample_search",
                            "result": result,
                            "summary": result,
                        }
                    ],
                },
            )
        assert claim_state(claim) == "unresolved_conflict"
        assert claim.idea and claim.idea.evidence_status == "unresolved_conflict"
        assert frontier(db, [a, b])[0].role == "counterexample_hunter"


def test_shared_proof_closes_only_exact_target_and_not_sibling_claims(
    client: TestClient, monkeypatch
) -> None:
    _, ids = _pool(client)
    # This verifies propagation of a checker verdict, not Lean itself (separate real-Lean tests).
    monkeypatch.setattr(
        LeanChecker, "check", lambda *args, **kwargs: LeanCheckResult(status="verified")
    )
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        assert a and b
        b.problem.formal_target = "theorem shared : 1 + 1 = 2"
        _submit(
            db,
            a,
            {
                "ideas": [
                    {
                        "title": "Two claims",
                        "approach": "mixed",
                        "claims": [
                            {"statement": "One lemma", "lean_declaration": b.problem.formal_target},
                            {"statement": "Unproved second claim"},
                        ],
                    }
                ],
                "evidence": [],
                "gaps": [],
            },
        )
        claim = db.scalar(select(Claim).where(Claim.statement == "One lemma"))
        assert claim
        _submit(
            db,
            b,
            {
                "ideas": [],
                "gaps": [],
                "evidence": [
                    {
                        "target": claim.id,
                        "check_type": "lean_attempt",
                        "result": "inconclusive",
                        "summary": "proof",
                        "artifact": {
                            "filename": "proof.lean",
                            "content": b.problem.formal_target + " := rfl",
                        },
                    }
                ],
            },
        )
        assert claim_state(claim) == "lean_verified"
        assert claim.idea and claim.idea.evidence_status != "lean_verified"
        assert claim.idea.formalization_status != "complete"
        assert get_scheduler()._wants_formalizer(claim.idea)
        assert b.research_outcome == "proven"
        assert a.research_outcome != "proven"


def test_late_claim_removes_verification_and_verified_score(
    client: TestClient, monkeypatch
) -> None:
    _, ids = _pool(client)
    monkeypatch.setattr(
        LeanChecker, "check", lambda *args, **kwargs: LeanCheckResult(status="verified")
    )
    scheduler = get_scheduler()
    with SessionLocal() as db:
        campaign = db.get(Campaign, ids[0])
        assert campaign
        first = _claim(db, campaign)
        idea = first.idea
        assert idea

        def prove(claim: Claim) -> None:
            _submit(
                db,
                campaign,
                {
                    "evidence": [
                        {
                            "target": claim.id,
                            "check_type": "lean_attempt",
                            "result": "inconclusive",
                            "summary": "fixture proof",
                            "artifact": {
                                "filename": "proof.lean",
                                "content": claim.lean_declaration + " := rfl",
                            },
                        }
                    ],
                },
            )

        prove(first)
        assert idea_status(idea) == ("lean_verified", "complete")
        assert not scheduler._wants_formalizer(idea)
        producer = db.get(Attempt, idea.produced_by_attempt_id)
        assert producer
        # Streaming output can append a claim after the first one has been certified.
        scheduler.ingestor.ingest(
            db,
            producer,
            {
                "ideas": [
                    {
                        "title": idea.title,
                        "approach": idea.approach,
                        "claims": [{"statement": "Later unproved claim"}],
                    }
                ]
            },
            partial=True,
        )
        sibling = next(c for c in idea.claims if c.id != first.id)
        assert claim_state(first) == "lean_verified"
        assert claim_state(sibling) == "untested"
        assert (idea.evidence_status, idea.formalization_status) == (
            "lean_formalization_in_progress",
            "in_progress",
        )
        assert scheduler._wants_formalizer(idea)
        projection = scheduler.publisher._idea(db, idea.id)
        assert projection and projection[0]["evidence_status"] != "lean_verified"
        assert projection[0]["formalization_status"] != "complete"

        # A legacy stored label must not give this branch the full-verification score.
        idea.evidence_status, idea.formalization_status = "lean_verified", "complete"
        _submit(
            db,
            campaign,
            {
                "evidence": [
                    {
                        "target": idea.id,
                        "check_type": "critique",
                        "result": "supports",
                        "summary": "No refutation in fixture",
                    }
                ]
            },
        )
        idea.evidence_status, idea.formalization_status = "lean_verified", "complete"
        assert scheduler._wants_formalizer(idea)
        assert scheduler.maybe_select(db, campaign)
        decision = db.scalar(select(SelectionDecision).where(SelectionDecision.idea_id == idea.id))
        assert decision and decision.score_components["evidence"] == 3.0
        assert scheduler.publisher._idea(db, idea.id)[0]["evidence_status"] != "lean_verified"

        sibling.lean_declaration = "theorem sibling : 2 + 2 = 4"
        prove(sibling)
        assert idea_status(idea) == ("lean_verified", "complete")
        assert not scheduler._wants_formalizer(idea)


def test_formalizer_eligibility_uses_current_claim_version(client: TestClient) -> None:
    _, ids = _pool(client)
    scheduler = get_scheduler()
    with SessionLocal() as db:
        campaign = db.get(Campaign, ids[0])
        assert campaign
        claim = _claim(db, campaign)
        idea = claim.idea
        assert idea
        db.add_all(
            [
                Evidence(
                    idea_id=idea.id,
                    claim_id=claim.id,
                    claim_version=1,
                    check_type="lean_attempt",
                    result="supports",
                    summary="worker assertion",
                ),
                Evidence(
                    idea_id=idea.id,
                    claim_id=claim.id,
                    claim_version=1,
                    check_type="lean_check",
                    result="verified",
                    certified=True,
                    summary="old-version fixture",
                ),
                Evidence(
                    idea_id=idea.id, check_type="critique", result="supports", summary="review"
                ),
            ]
        )
        db.flush()
        db.expire(claim, ["evidence"])
        db.expire(idea, ["evidence"])
        idea.evidence_status, idea.formalization_status = "lean_verified", "complete"
        assert not scheduler._wants_formalizer(idea)
        claim.version = 2
        assert idea_status(idea) == ("lean_formalization_in_progress", "in_progress")
        assert scheduler._wants_formalizer(idea)
        assert scheduler._next_role(idea) == "prover_formalizer"
        # A new worker claim of success is not independent verification and is not retried.
        db.add(
            Evidence(
                idea_id=idea.id,
                claim_id=claim.id,
                claim_version=2,
                check_type="lean_attempt",
                result="supports",
                summary="uncertified",
            )
        )
        db.flush()
        db.expire(claim, ["evidence"])
        assert idea_status(idea)[0] != "lean_verified"
        assert not scheduler._wants_formalizer(idea)
        assert scheduler._next_role(idea) is None


def test_pause_limits_duplicate_work_and_comparison_context(client: TestClient) -> None:
    portfolio, ids = _pool(client, budget=2)
    client.post(f"/private/portfolios/{portfolio}/pause", headers=OWNER)
    _run_ticks(client, 3)
    assert all(
        not client.get(f"/private/campaigns/{cid}/attempts", headers=OWNER).json() for cid in ids
    )
    client.post(f"/private/portfolios/{portfolio}/pause?paused=false", headers=OWNER)
    _run_ticks(client, 20)
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        assert a and b
        assert a.sessions_used <= 2 and b.sessions_used <= 2
        assert not db.scalars(select(Attempt).where(Attempt.status == "queued")).all()
        assert len(pool_campaigns(db, a, isolated=True)) == 1
        keys = [at.model_metadata.get("research_task_key") for at in db.scalars(select(Attempt))]
        keys = [k for k in keys if k]
        assert len(keys) == len(set(keys))


def test_manual_role_aliases_and_unknown_roles(client: TestClient) -> None:
    _, ids = _pool(client)
    response = client.post(
        f"/private/campaigns/{ids[0]}/assignments", headers=OWNER, json={"role": "formalization"}
    )
    assert response.json()["role"] == "prover_formalizer"
    prompt = client.get(f"/private/attempts/{response.json()['id']}/prompt", headers=OWNER).json()[
        "prompt"
    ]
    assert "Produce a Lean 4 proof" in prompt
    assert (
        client.post(
            f"/private/campaigns/{ids[0]}/assignments", headers=OWNER, json={"role": "unknown"}
        ).status_code
        == 422
    )


def test_closure_waits_for_verified_inputs_and_retains_failed_gaps(client: TestClient) -> None:
    _, ids = _pool(client)
    with SessionLocal() as db:
        a, b = (db.get(Campaign, cid) for cid in ids)
        assert a and b
        b.problem.formal_target = "theorem target : 2 + 2 = 4"
        claim = _claim(db, a)
        record_link(
            db,
            source=claim,
            target_id=b.problem_id,
            kind="applies_to",
            status="adopted",
            reason="A useful sublemma, still unverified.",
        )
        assert not any(t.role == "proof_closer" for t in frontier(db, [a, b]))
        # Supply a verifier verdict as a fixture, without calling a real Lean process.
        db.add(
            Evidence(
                claim_id=claim.id,
                claim_version=claim.version,
                check_type="lean_check",
                result="verified",
                certified=True,
                summary="test fixture",
            )
        )
        db.flush()
        db.expire(claim, ["evidence"])
        task = next(t for t in frontier(db, [a, b]) if t.role == "proof_closer")
        attempt = get_scheduler().enqueue(
            db,
            b,
            role=task.role,
            idea=None,
            parents=[],
            mode="ultra",
            metadata={"research_task_key": task.key},
        )
        attempt.status = "completed"
        get_scheduler().ingestor.ingest(
            db,
            attempt,
            {"ideas": [], "evidence": [], "gaps": ["Need a bound for the remaining case."]},
        )
        assert not any(t.key == task.key for t in frontier(db, [a, b]))
        assert b.research_outcome != "proven"
        assert any(
            "Need a bound" in gap for item in memory(db, a)["bottlenecks"] for gap in item["gaps"]
        )
