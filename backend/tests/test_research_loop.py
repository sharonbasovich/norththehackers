"""End-to-end: seed atlas -> campaign -> mock Devin sessions -> ingest -> select -> publish."""

from fastapi.testclient import TestClient

from app.services.prompts import ASSIGNMENT_ROLES, ROLE_INSTRUCTIONS
from tests.conftest import OWNER

PRIVATE_FIELDS = {"prompt", "worker_token_hash", "prompt_hash", "error", "api_key", "usage"}


def _setup_campaign(client: TestClient, mode: str = "ultra") -> str:
    r = client.post("/private/seed", headers=OWNER)
    assert r.status_code == 200, r.text
    assert r.json()["problems"] >= 20
    portfolio = client.post("/private/portfolios", json={"name": "pilot"}, headers=OWNER).json()
    problem = client.get("/public/problems/goldbach-conjecture").json()["problem"]
    r = client.post(
        "/private/campaigns",
        json={
            "portfolio_id": portfolio["id"],
            "problem_id": problem["record_id"],
            "session_budget": 8,
            "default_mode": mode,
        },
        headers=OWNER,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _run_ticks(client: TestClient, n: int) -> list[dict]:
    return [client.post("/private/scheduler/tick", headers=OWNER).json() for _ in range(n)]


def test_public_requires_no_auth_and_private_requires_key(client: TestClient) -> None:
    assert client.get("/public/areas").status_code == 200
    assert client.get("/private/campaigns").status_code == 401
    assert client.get("/private/campaigns", headers={"X-API-Key": "wrong"}).status_code == 403
    assert client.get("/private/campaigns", headers=OWNER).status_code == 200


def test_seed_is_idempotent_and_published(client: TestClient) -> None:
    first = client.post("/private/seed", headers=OWNER).json()
    second = client.post("/private/seed", headers=OWNER).json()
    assert first["problems"] > 0 and second["problems"] == 0
    problems = client.get("/public/problems").json()
    assert len(problems) == first["problems"]
    goldbach = client.get("/public/problems/goldbach-conjecture").json()["problem"]
    assert goldbach["status"] == "reported_open"
    assert goldbach["evidence_label"].startswith("Reported open")
    assert {a["slug"] for a in goldbach["areas"]} >= {"number-theory", "additive-number-theory"}
    assert all(s["url"].startswith("http") for s in goldbach["sources"])
    # a cross-area problem is reachable from both of its top-level areas
    for area in ("number-theory", "analysis"):
        assert any(
            p["slug"] == "riemann-hypothesis"
            for p in client.get("/public/problems", params={"area": area}).json()
        )


def test_full_generation_cycle_with_mock_provider(client: TestClient) -> None:
    campaign_id = _setup_campaign(client, mode="fusion")
    summaries = _run_ticks(client, 12)
    assert sum(s["dispatched"] for s in summaries) >= 1

    attempts = client.get(f"/private/campaigns/{campaign_id}/attempts", headers=OWNER).json()
    assert attempts, "scheduler should have planned attempts"
    assert all(a["requested_mode"] == "fusion" for a in attempts)
    finished = [a for a in attempts if a["status"] == "completed"]
    assert finished, [a["status"] for a in attempts]
    assert all(a["reported_mode"] == "fusion" for a in finished)
    assert all("acus_consumed" in a["usage"] for a in finished)

    campaign = next(
        c for c in client.get("/private/campaigns", headers=OWNER).json() if c["id"] == campaign_id
    )
    assert campaign["ideas"] >= 3
    assert campaign["sessions_used"] <= campaign["session_budget"]

    # the generation's critique is one batched session, and each reviewed idea got its verdict
    batch = [a for a in attempts if a["role"] == "critic" and a["idea_id"] is None]
    assert len(batch) == 1 and len(batch[0]["review_idea_ids"]) >= 2
    reviewed = set(batch[0]["review_idea_ids"])
    critiqued = {
        ev["idea_id"]
        for ev in client.get("/public/problems/goldbach-conjecture").json()["evidence"]
        if ev["check_type"] == "critique"
    }
    assert reviewed <= critiqued

    problem = client.get("/public/problems/goldbach-conjecture").json()
    ideas = problem["ideas"]
    assert len(ideas) >= 3
    assert all(i["evidence_label"] for i in ideas)
    # worker-reported evidence is never certified and never "Lean verified"
    for ev in problem["evidence"]:
        if ev["check_type"] != "lean_check":
            assert ev["certified"] is False
            assert ev["evidence_label"] != "Lean verified"
    for claim in problem["claims"]:
        assert claim["formalization_status"] != "complete"
        assert claim["evidence_label"] != "Lean verified"


def test_public_payloads_never_leak_private_fields(client: TestClient) -> None:
    _setup_campaign(client)
    _run_ticks(client, 12)

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert k not in PRIVATE_FIELDS, k
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for path in (
        "/public/problems",
        "/public/problems/goldbach-conjecture",
        "/public/graph",
        "/public/events",
        "/public/areas",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        walk(r.json())
    graph = client.get("/public/graph").json()
    types = {n["type"] for n in graph["nodes"]}
    assert {"area", "problem", "idea"} <= types
    layers = {link["layer"] for link in graph["links"]}
    assert "atlas" in layers and "lineage" in layers


def test_selection_archives_and_revival_is_private(client: TestClient) -> None:
    campaign_id = _setup_campaign(client)
    _run_ticks(client, 30)
    events = client.get("/private/events", headers=OWNER).json()
    types = {e["type"] for e in events}
    assert "selection.completed" in types or "campaign.completed" in types
    problem = client.get("/public/problems/goldbach-conjecture").json()
    later = [i for i in problem["ideas"] if i["generation"] >= 1]
    assert later and all(i["parent_ids"] and i["depth"] >= 1 for i in later)
    kinds = {link["kind"] for link in client.get("/public/graph").json()["links"]}
    assert {"proposed_for", "refined_from"} <= kinds or "combined_from" in kinds
    archived = [i for i in problem["ideas"] if i["scheduling_status"] == "archived"]
    if archived:
        idea_id = archived[0]["record_id"]
        assert client.post(f"/private/ideas/{idea_id}/revive").status_code == 401
        r = client.post(f"/private/ideas/{idea_id}/revive", headers=OWNER)
        assert r.status_code == 200 and r.json()["scheduling_status"] == "active"
    attempts = client.get(f"/private/campaigns/{campaign_id}/attempts", headers=OWNER).json()
    assert len(attempts) <= 8


def test_manual_assignment_validates_roles_and_freezes_mode(client: TestClient) -> None:
    campaign_id = _setup_campaign(client, mode="ultra")
    status = client.get("/private/scheduler/status", headers=OWNER)
    assert status.status_code == 200
    assert status.json()["roles"] == list(ASSIGNMENT_ROLES)

    for role in ASSIGNMENT_ROLES:
        r = client.post(
            f"/private/campaigns/{campaign_id}/assignments",
            json={"role": role, "mode": "fusion", "comparison_group": "pilot-A"},
            headers=OWNER,
        )
        assert r.status_code == 200, (role, r.text)
        assert r.json()["role"] == role
        assert r.json()["requested_mode"] == "fusion"
        assert r.json()["comparison_group"] == "pilot-A"
        prompt = client.get(f"/private/attempts/{r.json()['id']}/prompt", headers=OWNER).json()
        assert f"ROLE: {role}" in prompt["prompt"]
        assert ROLE_INSTRUCTIONS[role] in prompt["prompt"]

    r = client.post(
        f"/private/campaigns/{campaign_id}/assignments",
        json={"role": "formalization", "mode": "fusion"},
        headers=OWNER,
    )
    assert r.status_code == 422
    assert "unknown assignment role" in r.json()["detail"]

    r = client.post(
        f"/private/campaigns/{campaign_id}/assignments",
        json={"role": "hypothesis_generator", "mode": "gpt-99"},
        headers=OWNER,
    )
    assert r.status_code == 422


def test_worker_token_scoping(client: TestClient) -> None:
    campaign_id = _setup_campaign(client)
    _run_ticks(client, 1)
    attempts = client.get(f"/private/campaigns/{campaign_id}/attempts", headers=OWNER).json()
    running = next(a for a in attempts if a["status"] == "running")
    r = client.get(f"/worker/attempts/{running['id']}/context")
    assert r.status_code == 401
    r = client.get(f"/worker/attempts/{running['id']}/context", headers={"X-Worker-Token": "nope"})
    assert r.status_code == 403
