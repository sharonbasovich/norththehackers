"""Provider adapter for Devin research sessions.

`DevinApiClient` targets the documented v3 cloud API
(https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions).
`MockDevinClient` is a deterministic local stand-in so the lab can run end-to-end without
consuming Devin usage. Both return the same `SessionInfo` shape.

Mode provenance: the requested `devin_mode` is stored on the Attempt; the mode the provider
reports back (`devin_mode` on GET) is stored separately as `reported_mode`. Underlying model
identities are recorded only when the provider exposes them; otherwise they remain "unknown".
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..config import Settings

DEVIN_MODES = ("normal", "fast", "lite", "ultra", "fusion")

# JSON Schema (Draft 7) the research session must satisfy with provide_structured_output.
RESEARCH_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "research_links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": "Existing claim ID or new local_id.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Problem ID for applies_to; claim ID/local_id otherwise.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["applies_to", "depends_on", "contradicts", "equivalent_to"],
                    },
                    "status": {
                        "type": "string",
                        "enum": ["proposed", "adopted", "rejected", "inconclusive"],
                    },
                    "reason": {"type": "string"},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["claim", "target", "kind", "status", "reason"],
            },
        },
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "local_id": {
                        "type": "string",
                        "description": "Short label (e.g. 'I1') used as evidence `target`.",
                    },
                    "title": {"type": "string"},
                    "approach": {"type": "string"},
                    "mechanism": {"type": "string"},
                    "next_experiment": {"type": "string"},
                    "method_tags": {"type": "array", "items": {"type": "string"}},
                    "parent_idea_ids": {"type": "array", "items": {"type": "string"}},
                    "novelty_rationale": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "local_id": {"type": "string"},
                                "statement": {"type": "string"},
                                "scope": {"type": "string"},
                                "lean_declaration": {"type": "string"},
                            },
                            "required": ["statement"],
                        },
                    },
                },
                "required": ["title", "approach"],
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "local_id or title of an idea/claim in this output, an "
                        "existing idea/claim id, 'self' for the assigned idea, or 'problem'.",
                    },
                    "check_type": {
                        "type": "string",
                        "enum": [
                            "counterexample_search",
                            "numerical_experiment",
                            "construction",
                            "proof_sketch",
                            "informal_proof",
                            "critique",
                            "lean_attempt",
                            "literature_check",
                        ],
                    },
                    "result": {"type": "string", "enum": ["supports", "refutes", "inconclusive"]},
                    "summary": {"type": "string"},
                    "coverage": {"type": "string"},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "artifact": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "content": {"type": "string"},
                            "media_type": {"type": "string"},
                        },
                        "required": ["filename", "content"],
                    },
                },
                "required": ["target", "check_type", "result", "summary"],
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "self_reported_models": {
            "type": "string",
            "description": "Optional. Leave empty unless the environment states the model(s).",
        },
    },
    "required": ["ideas", "evidence", "gaps"],
}


@dataclass
class SessionInfo:
    session_id: str
    url: str
    status: str  # new|claimed|running|exit|error|suspended|resuming
    status_detail: str = ""
    devin_mode: str | None = None
    structured_output: dict | None = None
    acus_consumed: float | None = None
    user_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """Devin idles in `running/waiting_for_user` after delivering and is later suspended
        for inactivity; with the deliverable (structured output) present either counts as
        done rather than blocked."""
        if self.status in {"exit", "error"}:
            return True
        if self.status == "suspended":
            return self.structured_output is not None
        if self.status != "running":
            return False
        return self.status_detail == "finished" or (
            self.status_detail == "waiting_for_user" and self.structured_output is not None
        )

    @property
    def is_blocked(self) -> bool:
        return self.status == "suspended" or self.status_detail in {
            "waiting_for_user",
            "waiting_for_approval",
        }


class DevinClient(Protocol):
    provider_name: str

    def create_session(
        self,
        *,
        prompt: str,
        devin_mode: str,
        tags: list[str],
        title: str,
        session_secrets: dict[str, str],
        max_acu_limit: int | None,
    ) -> SessionInfo: ...

    def get_session(self, session_id: str) -> SessionInfo: ...

    def terminate_session(self, session_id: str) -> None: ...


def _parse_session(data: dict) -> SessionInfo:
    return SessionInfo(
        session_id=str(data.get("session_id", "")),
        url=str(data.get("url", "")),
        status=str(data.get("status", "")),
        status_detail=str(data.get("status_detail") or ""),
        devin_mode=data.get("devin_mode"),
        structured_output=data.get("structured_output"),
        acus_consumed=data.get("acus_consumed"),
        user_id=data.get("user_id"),
        raw=data,
    )


class DevinApiClient:
    provider_name = "devin-api"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.devin_api_key or not settings.devin_org_id:
            raise ValueError("MATHLAB_DEVIN_API_KEY and MATHLAB_DEVIN_ORG_ID are required")
        self._org = settings.devin_org_id
        self._create_as = settings.devin_create_as_user_id or None
        self._client = client or httpx.Client(
            base_url=settings.devin_api_base,
            headers={"Authorization": f"Bearer {settings.devin_api_key}"},
            timeout=60,
        )

    def create_session(
        self,
        *,
        prompt: str,
        devin_mode: str,
        tags: list[str],
        title: str,
        session_secrets: dict[str, str],
        max_acu_limit: int | None,
    ) -> SessionInfo:
        if devin_mode not in DEVIN_MODES:
            raise ValueError(f"Unsupported devin_mode {devin_mode!r}")
        body: dict = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "devin_mode": devin_mode,
            "structured_output_schema": RESEARCH_OUTPUT_SCHEMA,
            "structured_output_required": True,
            "resumable": False,
            "session_secrets": [
                {"key": k, "value": v, "sensitive": True} for k, v in session_secrets.items()
            ],
        }
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit
        if self._create_as:
            body["create_as_user_id"] = self._create_as
        response = self._client.post(f"/v3/organizations/{self._org}/sessions", json=body)
        response.raise_for_status()
        return _parse_session(response.json())

    def get_session(self, session_id: str) -> SessionInfo:
        response = self._client.get(f"/v3/organizations/{self._org}/sessions/{session_id}")
        response.raise_for_status()
        return _parse_session(response.json())

    def terminate_session(self, session_id: str) -> None:
        response = self._client.delete(f"/v3/organizations/{self._org}/sessions/{session_id}")
        response.raise_for_status()


_RECORD_ID = re.compile(r"\b[0-9a-f]{32}\b")


class MockDevinClient:
    """Deterministic stand-in. Sessions finish on the second poll with plausible structured
    output derived from the prompt hash, so scheduler/ingestion/publication can be exercised
    without Devin usage. Nothing it produces is treated as verified."""

    provider_name = "mock"

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}

    def create_session(
        self,
        *,
        prompt: str,
        devin_mode: str,
        tags: list[str],
        title: str,
        session_secrets: dict[str, str],
        max_acu_limit: int | None,
    ) -> SessionInfo:
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        session_id = f"mock-{digest[:16]}"
        # Output depends on the assignment, not on the random record ids inside the prompt, so
        # a given campaign replays identically across runs.
        stable = hashlib.sha256(_RECORD_ID.sub("<id>", prompt).encode()).hexdigest()
        self._sessions[session_id] = {
            "polls": 0,
            "mode": devin_mode,
            "prompt": prompt,
            "seed": int(stable[:8], 16),
            "worker_token": session_secrets.get("LAB_WORKER_TOKEN", ""),
        }
        return SessionInfo(
            session_id=session_id, url=f"mock://{session_id}", status="new", devin_mode=devin_mode
        )

    def get_session(self, session_id: str) -> SessionInfo:
        state = self._sessions[session_id]
        state["polls"] += 1
        if state["polls"] < 2:
            return SessionInfo(
                session_id=session_id,
                url=f"mock://{session_id}",
                status="running",
                status_detail="working",
                devin_mode=state["mode"],
            )
        return SessionInfo(
            session_id=session_id,
            url=f"mock://{session_id}",
            status="running",
            status_detail="finished",
            devin_mode=state["mode"],
            structured_output=self._fake_output(state),
            acus_consumed=round(0.4 + (state["seed"] % 100) / 100, 2),
        )

    def terminate_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    @staticmethod
    def _fake_output(state: dict) -> dict:
        rng = random.Random(state["seed"])
        prompt: str = state["prompt"]
        role_name = re.search(r"^ROLE: (\w+)$", prompt, re.M)
        shared_role = role_name.group(1) if role_name else ""
        if shared_role in {
            "synthesizer",
            "connection_reviewer",
            "lemma_architect",
            "lemma_prover",
            "counterexample_hunter",
            "proof_closer",
        }:
            pool = json.loads(
                prompt.split("SHARED RESEARCH POOL\n", 1)[1].split("\n\nASSIGNMENT", 1)[0]
            )
            assignment = json.loads(prompt.split("ASSIGNMENT\n", 1)[1].split("\n\n", 1)[0])
            shared_output: dict = {
                "ideas": [],
                "evidence": [],
                "research_links": [],
                "gaps": ["mock provider: no real research"],
            }
            focus = next(
                (c for c in pool["claims"] if c["id"] == assignment.get("focus_claim_id")), None
            )
            if shared_role in {"synthesizer", "lemma_architect"}:
                shared_output["ideas"] = [
                    {
                        "local_id": "S1",
                        "title": "[mock] Shared finite-case lemma",
                        "approach": "Investigate a reusable finite-case argument across the pool.",
                        "next_experiment": "Test the shared claim's assumptions on each problem.",
                        "method_tags": ["shared-reduction"],
                        "claims": [
                            {
                                "local_id": "SC1",
                                "statement": "[mock] 1 + 1 = 2",
                                "lean_declaration": "theorem shared_mock_lemma : 1 + 1 = 2",
                            }
                        ],
                    }
                ]
                shared_output["research_links"] = [
                    {
                        "claim": "SC1",
                        "target": p["id"],
                        "kind": "applies_to",
                        "status": "proposed",
                        "reason": "[mock] Candidate shared route; untested.",
                    }
                    for p in pool["problems"]
                ]
            elif shared_role == "connection_reviewer" and focus:
                shared_output["research_links"] = [
                    {
                        "claim": focus["id"],
                        "target": assignment["target_problem_id"],
                        "kind": "applies_to",
                        "status": "adopted",
                        "reason": "[mock] Retain as a research route, not a proof.",
                    }
                ]
            elif shared_role == "lemma_prover" and focus:
                declaration = focus["lean_declaration"]
                proof = "rfl" if declaration.endswith(": 1 + 1 = 2") else "by sorry"
                shared_output["evidence"] = [
                    {
                        "target": focus["id"],
                        "check_type": "lean_attempt",
                        "result": "inconclusive",
                        "summary": "[mock] Shared lemma candidate.",
                        "artifact": {
                            "filename": "Shared.lean",
                            "content": f"{declaration} := {proof}\n",
                        },
                    }
                ]
            elif shared_role == "counterexample_hunter" and focus:
                shared_output["evidence"] = [
                    {
                        "target": focus["id"],
                        "check_type": "counterexample_search",
                        "result": "inconclusive",
                        "summary": "[mock] Conflict remains unresolved.",
                    }
                ]
            return shared_output
        role = "hypothesis_generator"
        for candidate in ("experimenter", "critic", "prover_formalizer", "hypothesis_generator"):
            if f"ROLE: {candidate}" in prompt:
                role = candidate
                break
        if role == "hypothesis_generator":
            families = [
                ("Density increment", ["additive-combinatorics", "density-increment"]),
                ("Probabilistic construction", ["probabilistic-method", "construction"]),
                ("Spectral/Fourier bound", ["fourier-analysis", "spectral"]),
                ("Structural reduction", ["reduction", "structure-theory"]),
                ("Computational search for small cases", ["computation", "counterexample-search"]),
            ]
            rng.shuffle(families)
            ideas = [
                {
                    "title": f"{name} approach",
                    "approach": f"Attempt a {name.lower()} argument for the stated problem.",
                    "mechanism": "Identify the key lemma whose failure would falsify the approach.",
                    "next_experiment": "Check the lemma on small cases; record the first failure.",
                    "method_tags": tags,
                    "parent_idea_ids": [],
                    "novelty_rationale": "Mock output; novelty unchecked.",
                    "claims": [{"statement": f"[mock] key lemma for {name.lower()}"}],
                }
                for name, tags in families[:3]
            ]
            return {"ideas": ideas, "evidence": [], "gaps": ["mock provider: no real research"]}
        if role == "prover_formalizer":
            return {
                "ideas": [
                    {
                        "local_id": "L1",
                        "title": "Formalizable sub-lemma of the assigned idea",
                        "approach": "Prove the smallest faithful piece in Lean.",
                        "mechanism": "Mock: restricts the key lemma to a finite case.",
                        "next_experiment": "",
                        "method_tags": ["formalization", "lean"],
                        "parent_idea_ids": [],
                        "novelty_rationale": "Mock output; novelty unchecked.",
                        "claims": [
                            {
                                "local_id": "C1",
                                "statement": "[mock] 1 + 1 = 2",
                                "lean_declaration": "theorem mock_lemma : 1 + 1 = 2",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "target": "C1",
                        "check_type": "lean_attempt",
                        "result": "inconclusive",
                        "summary": "[mock] candidate Lean file; the lab's checker decides",
                        "coverage": "mock coverage",
                        "assumptions": [],
                        "artifact": {
                            "filename": "result.lean",
                            "content": "import MathLab.Basic\n"
                            "theorem mock_lemma : 1 + 1 = 2 := rfl\n",
                        },
                    }
                ],
                "gaps": ["mock provider: no real research"],
            }
        # Verdict follows the idea family so a campaign always ends with a mix of supported,
        # inconclusive and refuted branches regardless of which families were drawn.
        check_type = {
            "experimenter": "numerical_experiment",
            "critic": "critique",
        }[role]
        if "IDEAS UNDER REVIEW" in prompt:
            reviewed = prompt.split("IDEAS UNDER REVIEW", 1)[1].split("\n\nRULES", 1)[0]
            blocks = [b for b in reviewed.split("- id: ")[1:]]
            targets = [(b.split("\n", 1)[0].strip(), b) for b in blocks]
        else:
            targets = [("self", prompt.split("ASSIGNED IDEA", 1)[-1])]
        evidence = []
        for target, text in targets:
            result = "supports"
            if "Probabilistic construction" in text:
                result = "refutes"
            elif "Spectral/Fourier" in text:
                result = "inconclusive"
            content = json.dumps({"checked_range": 1000, "result": result})
            evidence.append(
                {
                    "target": target,
                    "check_type": check_type,
                    "result": result,
                    "summary": f"[mock] {check_type} finished with result {result}",
                    "coverage": "mock coverage",
                    "assumptions": [],
                    "artifact": {"filename": "result.json", "content": content},
                }
            )
        return {"ideas": [], "evidence": evidence, "gaps": ["mock provider: no real research"]}


def build_client(settings: Settings) -> DevinClient:
    if settings.devin_provider == "api":
        return DevinApiClient(settings)
    return MockDevinClient()
