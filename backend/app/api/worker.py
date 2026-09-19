"""Attempt-scoped endpoints Devin sessions can call mid-run (also exposed through MCP later).
Workers can read their assignment context and submit partial output; they cannot certify."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_worker_attempt
from ..db import get_db
from ..deps import get_scheduler
from ..models import Artifact, Attempt, Claim, Evidence, Idea
from ..services.artifacts import read_artifact
from ..services.devin_client import RESEARCH_OUTPUT_SCHEMA
from ..services.ingest import declaration_name, declaration_signature
from ..services.prompts import describe_idea
from ..services.research import memory, scope_ids

router = APIRouter(prefix="/worker", tags=["worker"])


@router.get("/attempts/{attempt_id}/context")
def context(
    attempt: Attempt = Depends(require_worker_attempt), db: Session = Depends(get_db)
) -> dict:
    campaign = attempt.campaign
    return {
        "attempt_id": attempt.id,
        "role": attempt.role,
        "requested_mode": attempt.requested_mode,
        "problem": {
            "title": campaign.problem.title,
            "statement": campaign.problem.statement,
            "definitions": campaign.problem.definitions,
            "assumptions": campaign.problem.assumptions,
            "formal_target": campaign.problem.formal_target,
        },
        "active_ideas": [
            describe_idea(i)
            for i in campaign.ideas
            if i.scheduling_status in {"active", "promoted"}
        ],
        "output_schema": RESEARCH_OUTPUT_SCHEMA,
        "assignment": attempt.model_metadata,
        "research_pool": memory(
            db,
            campaign,
            isolated=bool(attempt.comparison_group),
            focus_claim_id=(attempt.model_metadata or {}).get("focus_claim_id"),
        ),
        "lean": _lean_environment(),
    }


@router.get("/attempts/{attempt_id}/artifacts/{artifact_id}")
def shared_artifact(
    artifact_id: str,
    attempt: Attempt = Depends(require_worker_attempt),
    db: Session = Depends(get_db),
) -> dict:
    allowed = scope_ids(db, attempt)
    artifact = db.get(Artifact, artifact_id)
    authorized = False
    for evidence in db.scalars(select(Evidence).where(Evidence.artifact_id == artifact_id)):
        claim = db.get(Claim, evidence.claim_id) if evidence.claim_id else None
        idea = db.get(Idea, evidence.idea_id) if evidence.idea_id else None
        if (claim and claim.campaign_id in allowed) or (idea and idea.campaign_id in allowed):
            authorized = True
    if artifact is None or not authorized:
        raise HTTPException(404, "artifact not in this research pool")
    return {
        "id": artifact.id,
        "filename": artifact.filename,
        "content_hash": artifact.content_hash,
        "content": read_artifact(artifact),
    }


@router.get("/attempts/{attempt_id}/claims/{claim_id}")
def shared_claim(
    claim_id: str, attempt: Attempt = Depends(require_worker_attempt), db: Session = Depends(get_db)
) -> dict:
    claim = db.get(Claim, claim_id)
    if claim is None or claim.campaign_id not in scope_ids(db, attempt):
        raise HTTPException(404, "claim not in this research pool")
    context = memory(
        db, attempt.campaign, isolated=bool(attempt.comparison_group), focus_claim_id=claim.id
    )
    return {
        "claim": next(c for c in context["claims"] if c["id"] == claim.id),
        "links": context["links"],
    }


def _lean_environment() -> dict:
    checker = get_scheduler().ingestor.lean_checker
    root = checker.project_dir
    if root is None or not checker.available():
        return {"available": False}

    def read(path: Path) -> str:
        return path.read_text() if path.exists() else ""

    return {
        "available": True,
        "toolchain": read(root / "lean-toolchain").strip(),
        "lakefile": read(root / "lakefile.toml"),
        "MathLab/Basic.lean": read(root / "MathLab" / "Basic.lean"),
        "allowed_axioms": sorted(checker.allowed_axioms),
    }


class LeanCheckRequest(BaseModel):
    source: str = Field(max_length=200_000)
    declaration: str = Field(max_length=4000, description="approved `theorem name binders : stmt`")


@router.post("/attempts/{attempt_id}/lean-check")
def lean_check(body: LeanCheckRequest, attempt: Attempt = Depends(require_worker_attempt)) -> dict:
    """Dry-run the lab's Lean checker on a candidate file so a formalization session can
    iterate. Nothing is recorded; certification only happens when the final structured output
    carries the file as a `lean_attempt` artifact targeting a claim with that declaration."""
    name = declaration_name(body.declaration)
    if not name:
        raise HTTPException(422, "declaration must start with `theorem <name>` or `lemma <name>`")
    outcome = get_scheduler().ingestor.lean_checker.check(
        body.source, name, declaration_signature(body.declaration)
    )
    return {"attempt_id": attempt.id, **outcome.as_details()}


@router.post("/attempts/{attempt_id}/submit")
def submit(
    payload: dict, attempt: Attempt = Depends(require_worker_attempt), db: Session = Depends(get_db)
) -> dict:
    """Idempotent partial submission. Final results still arrive via structured output."""
    counts = get_scheduler().ingestor.ingest(db, attempt, payload, partial=True)
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"accepted": counts, "note": "recorded as uncertified worker evidence"}
