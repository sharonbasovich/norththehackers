"""Attempt-scoped endpoints Devin sessions can call mid-run (also exposed through MCP later).
Workers can read their assignment context and submit partial output; they cannot certify."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_worker_attempt
from ..db import get_db
from ..deps import get_scheduler
from ..models import Attempt
from ..services.devin_client import RESEARCH_OUTPUT_SCHEMA
from ..services.ingest import declaration_name, declaration_signature
from ..services.prompts import describe_idea

router = APIRouter(prefix="/worker", tags=["worker"])


@router.get("/attempts/{attempt_id}/context")
def context(attempt: Attempt = Depends(require_worker_attempt)) -> dict:
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
        "lean": _lean_environment(),
    }


def _lean_environment() -> dict:
    return get_scheduler().ingestor.lean_checker.environment()


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
