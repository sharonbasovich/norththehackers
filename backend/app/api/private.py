"""Owner/collaborator research controls. Everything here requires X-API-Key."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import hash_key, new_api_key, require_collaborator, require_owner
from ..config import get_settings
from ..db import get_db
from ..deps import get_scheduler
from ..models import (
    Area,
    Attempt,
    Campaign,
    Collaborator,
    Evidence,
    Idea,
    Portfolio,
    Problem,
    ProblemArea,
    Publication,
    Relation,
    Source,
    SourceAssertion,
    utcnow,
)
from ..seed import load_atlas_document, load_seed
from ..services import erdos_import
from ..services.atlas_import import (
    DEFAULT_PAGE,
    fetch_wikitext,
    parse_unsolved_list,
    to_atlas_document,
)
from ..services.devin_client import DEVIN_MODES
from ..services.events import emit
from ..services.publication import withdraw
from ..services.research import memory
from ..services.scheduler import certify_evidence, revive_idea

router = APIRouter(
    prefix="/private", tags=["private"], dependencies=[Depends(require_collaborator)]
)


# -- schemas -------------------------------------------------------------------------------------


class SourceIn(BaseModel):
    title: str
    url: str
    location: str = ""
    asserted_status: str = "open"
    asserted_at: str = ""
    retrieved_date: str = ""
    notes: str = ""


class ProblemIn(BaseModel):
    slug: str
    title: str
    statement: str
    definitions: str = ""
    assumptions: str = ""
    attribution: str = ""
    area_slugs: list[str] = Field(default_factory=list)
    formal_target: str = ""
    sources: list[SourceIn] = Field(default_factory=list)


class PortfolioIn(BaseModel):
    name: str
    max_concurrent_sessions: int = Field(default=2, ge=1, le=32)


class CampaignIn(BaseModel):
    portfolio_id: str
    problem_id: str
    session_budget: int = Field(default=6, ge=1)
    default_mode: str = "ultra"
    policy: dict = Field(default_factory=dict)
    seed: int = 0


class AssignmentIn(BaseModel):
    role: str
    idea_id: str | None = None
    mode: str | None = None
    comparison_group: str = ""


class ResearchPoolIn(BaseModel):
    problem_ids: list[str] = Field(min_length=1, max_length=200)
    session_budget_per_problem: int = Field(default=12, ge=1, le=10000)
    default_mode: str = "ultra"


class WikipediaImportIn(BaseModel):
    page: str = DEFAULT_PAGE
    dry_run: bool = False
    limit: int | None = Field(default=None, ge=1)
    wikitext: str | None = Field(
        default=None, description="pre-fetched page source; skips the network fetch"
    )


class ErdosImportIn(BaseModel):
    dry_run: bool = False
    limit: int | None = Field(default=None, ge=1)
    include_resolved: bool = False
    status_table: str | None = Field(
        default=None, description="pre-fetched problems.yaml; skips the network fetch"
    )
    formal_sources: dict[str, str] | None = Field(
        default=None,
        description="pre-fetched formal-conjectures Lean sources by number; skips the fetch",
    )
    site_statements: dict[str, str] | None = Field(
        default=None,
        description="pre-fetched erdosproblems.com statements by number; skips the fetch",
    )
    fetch_site_statements: bool = Field(
        default=True, description="fetch each problem's statement from its erdosproblems.com page"
    )


class ReviewIn(BaseModel):
    result: str  # confirmed | confirmed_refutation | disputed
    note: str = ""


PROBLEM_REVIEW_STATUSES = {"reported_open", "resolution_claimed", "resolved", "disputed", "unknown"}


class ProblemReviewIn(BaseModel):
    status: str
    note: str = ""
    assertion_ids: list[str] = Field(
        default_factory=list, description="assertions checked; empty means all of the problem's"
    )


class CollaboratorIn(BaseModel):
    name: str


class WithdrawIn(BaseModel):
    reason: str


# -- atlas ---------------------------------------------------------------------------------------


@router.post("/seed")
def seed(db: Session = Depends(get_db)) -> dict:
    counts = load_seed(db)
    counts["published"] = get_scheduler().publisher.process_outbox(db)
    return counts


@router.post("/atlas/import")
def import_atlas_document(body: dict, db: Session = Depends(get_db)) -> dict:
    """Bulk-load a document in the seed format (areas, problems with sources)."""
    if "problems" not in body or "retrieved_date" not in body:
        raise HTTPException(422, "document needs retrieved_date and problems")
    body.setdefault("areas", [])
    body.setdefault("origin", "bulk_import")
    counts = load_atlas_document(db, body)
    counts["published"] = get_scheduler().publisher.process_outbox(db)
    return counts


@router.post("/atlas/import/wikipedia")
def import_wikipedia_list(body: WikipediaImportIn, db: Session = Depends(get_db)) -> dict:
    """Import the reported-open entries of a Wikipedia list page, one unreviewed source
    assertion per listing plus the linked article. Entries whose article is already asserted
    for an existing problem are skipped as duplicates."""
    try:
        wikitext = body.wikitext if body.wikitext is not None else fetch_wikitext(body.page)
    except Exception as exc:
        raise HTTPException(502, f"could not fetch {body.page}: {exc}") from exc
    listed = parse_unsolved_list(wikitext)
    if body.limit:
        listed = listed[: body.limit]
    document = to_atlas_document(listed, page=body.page)
    if body.dry_run:
        return {
            "dry_run": True,
            "parsed": len(listed),
            "sample": document["problems"][:5],
            "areas": document["areas"],
        }
    counts = load_atlas_document(db, document)
    counts["parsed"] = len(listed)
    counts["published"] = get_scheduler().publisher.process_outbox(db)
    return counts


@router.post("/atlas/import/erdos")
def import_erdos_problems(body: ErdosImportIn, db: Session = Depends(get_db)) -> dict:
    """Import the Erdős problems the community status table still reports open, with their
    statements (and reference Lean statements) from formal-conjectures. Both datasets are
    Apache-2.0; every state is stored as a dated, unreviewed assertion."""
    try:
        table = (
            body.status_table
            if body.status_table is not None
            else erdos_import.fetch_status_table()
        )
    except Exception as exc:
        raise HTTPException(502, f"could not fetch the Erdős status table: {exc}") from exc
    entries = erdos_import.parse_status_table(table)
    if not body.include_resolved:
        entries = [e for e in entries if e.is_open]
    if body.limit:
        entries = entries[: body.limit]
    try:
        sources = (
            body.formal_sources
            if body.formal_sources is not None
            else erdos_import.fetch_formal_conjectures([e.number for e in entries])
        )
    except Exception as exc:
        raise HTTPException(502, f"could not fetch formal-conjectures sources: {exc}") from exc
    erdos_import.attach_formalizations(entries, sources)
    if body.site_statements is not None:
        erdos_import.attach_site_statements(entries, body.site_statements)
    elif body.fetch_site_statements:
        try:
            erdos_import.attach_site_statements(
                entries, erdos_import.fetch_site_statements([e.number for e in entries])
            )
        except Exception as exc:
            raise HTTPException(502, f"could not fetch erdosproblems.com pages: {exc}") from exc
    document = erdos_import.to_atlas_document(entries, include_resolved=body.include_resolved)
    if body.dry_run:
        return {
            "dry_run": True,
            "parsed": len(entries),
            "with_statement": sum(1 for e in entries if e.statement),
            "importable": len(document["problems"]),
            "sample": document["problems"][:5],
            "areas": document["areas"],
        }
    counts = load_atlas_document(db, document)
    counts["parsed"] = len(entries)
    counts["without_statement"] = sum(1 for e in entries if not e.statement)
    counts["published"] = get_scheduler().publisher.process_outbox(db)
    return counts


@router.post("/problems")
def create_problem(body: ProblemIn, db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(Problem).where(Problem.slug == body.slug)):
        raise HTTPException(409, "slug exists")
    if not body.sources:
        raise HTTPException(422, "a literature problem needs at least one source")
    problem = Problem(
        slug=body.slug,
        title=body.title,
        statement=body.statement,
        definitions=body.definitions,
        assumptions=body.assumptions,
        attribution=body.attribution,
        status="reported_open",
        formal_target=body.formal_target,
        formal_target_status="proposed" if body.formal_target else "absent",
    )
    db.add(problem)
    db.flush()
    for i, slug in enumerate(body.area_slugs):
        area = db.scalar(select(Area).where(Area.slug == slug))
        if area is None:
            raise HTTPException(422, f"unknown area {slug}")
        db.add(ProblemArea(problem_id=problem.id, area_id=area.id, primary=(i == 0)))
        db.add(
            Relation(
                layer="atlas",
                kind="classified_in",
                source_type="problem",
                source_id=problem.id,
                target_type="area",
                target_id=area.id,
                status="checked",
            )
        )
    for s in body.sources:
        source = db.scalar(select(Source).where(Source.url == s.url)) or Source(
            title=s.title, url=s.url, retrieved_date=s.retrieved_date
        )
        db.add(source)
        db.flush()
        db.add(
            SourceAssertion(
                source_id=source.id,
                problem_id=problem.id,
                location=s.location,
                asserted_status=s.asserted_status,
                asserted_at=s.asserted_at,
                notes=s.notes,
            )
        )
    emit(db, "problem.created", record_type="problem", record_id=problem.id, visibility="public")
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"id": problem.id}


@router.post("/problems/{problem_id}/review")
def review_problem_status(
    problem_id: str,
    body: ProblemReviewIn,
    db: Session = Depends(get_db),
    collaborator: Collaborator = Depends(require_collaborator),
) -> dict:
    """Human review of a problem's reported status. Workers (status_researcher) can only flag
    `resolution_claimed`; moving to `resolved`/`disputed`/back to `reported_open` is a
    collaborator decision recorded here with the assertions that were checked."""
    if body.status not in PROBLEM_REVIEW_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(PROBLEM_REVIEW_STATUSES)}")
    problem = db.get(Problem, problem_id)
    if problem is None:
        raise HTTPException(404, "problem not found")
    checked = [
        a for a in problem.assertions if not body.assertion_ids or a.id in body.assertion_ids
    ]
    for assertion in checked:
        assertion.review_state = "reviewed"
    today = utcnow().date().isoformat()
    previous = problem.status
    problem.status = body.status
    problem.status_checked_at = today
    reviews = list(problem.coverage.get("status_reviews", []))
    reviews.append(
        {
            "reviewer": collaborator.name,
            "date": today,
            "from": previous,
            "to": body.status,
            "note": body.note,
            "assertion_ids": [a.id for a in checked],
        }
    )
    problem.coverage = {**problem.coverage, "status_reviews": reviews}
    emit(
        db,
        "problem.status_reviewed",
        record_type="problem",
        record_id=problem.id,
        payload={"from": previous, "to": body.status, "reviewer": collaborator.name},
        visibility="public",
    )
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"id": problem.id, "status": problem.status, "assertions_reviewed": len(checked)}


# -- portfolios & campaigns ----------------------------------------------------------------------


@router.get("/portfolios")
def list_portfolios(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "paused": p.paused,
            "max_concurrent_sessions": p.max_concurrent_sessions,
        }
        for p in db.scalars(select(Portfolio))
    ]


@router.post("/portfolios")
def create_portfolio(body: PortfolioIn, db: Session = Depends(get_db)) -> dict:
    portfolio = Portfolio(name=body.name, max_concurrent_sessions=body.max_concurrent_sessions)
    db.add(portfolio)
    db.commit()
    return {"id": portfolio.id}


@router.post("/portfolios/{portfolio_id}/research")
def start_research_pool(
    portfolio_id: str, body: ResearchPoolIn, db: Session = Depends(get_db)
) -> dict:
    """Add a set of problems to shared exploration without resetting existing budgets."""
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(404, "portfolio not found")
    if body.default_mode not in DEVIN_MODES:
        raise HTTPException(422, "unknown Devin mode")
    ids = set(body.problem_ids)
    problems = list(db.scalars(select(Problem).where(Problem.id.in_(ids))))
    if len(problems) != len(ids):
        raise HTTPException(404, "one or more problems not found")
    existing = list(db.scalars(select(Campaign).where(Campaign.portfolio_id == portfolio_id)))
    created = []
    for problem in problems:
        if any(c.problem_id == problem.id for c in existing):
            continue
        campaign = Campaign(
            portfolio_id=portfolio_id,
            problem_id=problem.id,
            session_budget=body.session_budget_per_problem,
            policy={"default_mode": body.default_mode, "shared_research": True},
        )
        db.add(campaign)
        db.flush()
        emit(
            db,
            "campaign.created",
            record_type="campaign",
            record_id=campaign.id,
            visibility="public",
        )
        created.append(campaign.id)
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {
        "portfolio_id": portfolio_id,
        "created_campaign_ids": created,
        "note": "Shared tasks use these campaign budgets; existing campaigns are unchanged.",
    }


@router.get("/portfolios/{portfolio_id}/research")
def research_pool(portfolio_id: str, db: Session = Depends(get_db)) -> dict:
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(404, "portfolio not found")
    campaigns = list(db.scalars(select(Campaign).where(Campaign.portfolio_id == portfolio_id)))
    campaign = next((c for c in campaigns if c.policy.get("shared_research", True)), None)
    return (
        memory(db, campaign, limit=200)
        if campaign
        else {"problems": [], "claims": [], "links": [], "failed_directions": []}
    )


@router.post("/portfolios/{portfolio_id}/pause")
def pause_portfolio(portfolio_id: str, paused: bool = True, db: Session = Depends(get_db)) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(404)
    portfolio.paused = paused
    emit(
        db,
        "portfolio.paused" if paused else "portfolio.resumed",
        record_type="portfolio",
        record_id=portfolio.id,
    )
    db.commit()
    return {"paused": portfolio.paused}


@router.post("/portfolios/{portfolio_id}/concurrency")
def set_portfolio_concurrency(
    portfolio_id: str,
    max_concurrent_sessions: int = Query(ge=1, le=32),
    db: Session = Depends(get_db),
) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(404)
    portfolio.max_concurrent_sessions = max_concurrent_sessions
    db.commit()
    return {"max_concurrent_sessions": portfolio.max_concurrent_sessions}


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)) -> list[dict]:
    return [_campaign_view(c) for c in db.scalars(select(Campaign))]


def _campaign_view(c: Campaign) -> dict:
    return {
        "id": c.id,
        "problem_id": c.problem_id,
        "problem_title": c.problem.title,
        "portfolio_id": c.portfolio_id,
        "state": c.state,
        "research_outcome": c.research_outcome or "researching",
        "generation": c.generation,
        "session_budget": c.session_budget,
        "sessions_used": c.sessions_used,
        "policy": c.policy,
        "policy_version": c.policy_version,
        "ideas": len(c.ideas),
        "attempts": len(c.attempts),
    }


@router.post("/campaigns")
def create_campaign(body: CampaignIn, db: Session = Depends(get_db)) -> dict:
    if body.default_mode not in DEVIN_MODES:
        raise HTTPException(422, f"mode must be one of {DEVIN_MODES}")
    if db.get(Portfolio, body.portfolio_id) is None or db.get(Problem, body.problem_id) is None:
        raise HTTPException(404, "portfolio or problem not found")
    campaign = Campaign(
        portfolio_id=body.portfolio_id,
        problem_id=body.problem_id,
        session_budget=body.session_budget,
        seed=body.seed,
        policy={**body.policy, "default_mode": body.default_mode},
    )
    db.add(campaign)
    db.flush()
    emit(db, "campaign.created", record_type="campaign", record_id=campaign.id, visibility="public")
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return _campaign_view(campaign)


@router.post("/campaigns/{campaign_id}/state")
def set_campaign_state(campaign_id: str, state: str, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404)
    if state not in {"active", "paused", "completed"}:
        raise HTTPException(422)
    campaign.state = state
    emit(
        db,
        "campaign.state_changed",
        record_type="campaign",
        record_id=campaign.id,
        payload={"state": state},
        visibility="public",
    )
    db.commit()
    return _campaign_view(campaign)


@router.post("/campaigns/{campaign_id}/assignments")
def create_assignment(campaign_id: str, body: AssignmentIn, db: Session = Depends(get_db)) -> dict:
    """Manual bounded assignment, e.g. for the Fusion/Ultra pilot: mode is frozen at enqueue."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404)
    mode = body.mode or campaign.policy.get("default_mode", "ultra")
    idea = db.get(Idea, body.idea_id) if body.idea_id else None
    if body.idea_id and (idea is None or idea.campaign_id != campaign.id):
        raise HTTPException(404, "idea not in campaign")
    try:
        attempt = get_scheduler().enqueue(
            db,
            campaign,
            role=body.role,
            idea=idea,
            parents=[],
            mode=mode,
            comparison_group=body.comparison_group,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _attempt_view(attempt)


@router.get("/campaigns/{campaign_id}/attempts")
def list_attempts(campaign_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Attempt).where(Attempt.campaign_id == campaign_id).order_by(Attempt.created_at)
    ).all()
    return [_attempt_view(a) for a in rows]


def _attempt_view(a: Attempt) -> dict:
    return {
        "id": a.id,
        "campaign_id": a.campaign_id,
        "idea_id": a.idea_id,
        "review_idea_ids": (a.model_metadata or {}).get("review_idea_ids", []),
        "research_task": {
            k: v
            for k, v in (a.model_metadata or {}).items()
            if k in {"focus_claim_id", "target_problem_id", "reason", "research_task_key"}
        },
        "role": a.role,
        "requested_mode": a.requested_mode,
        "reported_mode": a.reported_mode,
        "provider": a.provider,
        "provider_session_id": a.provider_session_id,
        "provider_session_url": a.provider_session_url,
        "status": a.status,
        "status_detail": a.status_detail,
        "usage": a.usage,
        "result": a.result,
        "error": a.error,
        "retries": a.retries,
        "comparison_group": a.comparison_group,
        "created_at": a.created_at.isoformat(),
        "started_at": a.started_at.isoformat() if a.started_at else None,
        "finished_at": a.finished_at.isoformat() if a.finished_at else None,
    }


@router.get("/attempts/{attempt_id}/prompt")
def attempt_prompt(attempt_id: str, db: Session = Depends(get_db)) -> dict:
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(404)
    return {"id": attempt.id, "prompt": attempt.prompt, "prompt_hash": attempt.prompt_hash}


@router.post("/attempts/{attempt_id}/cancel")
def cancel_attempt(attempt_id: str, db: Session = Depends(get_db)) -> dict:
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(404)
    if attempt.provider_session_id and attempt.status in {"running", "blocked"}:
        get_scheduler().client.terminate_session(attempt.provider_session_id)
    attempt.status = "cancelled"
    emit(db, "attempt.cancelled", record_type="attempt", record_id=attempt.id)
    db.commit()
    return _attempt_view(attempt)


@router.post("/attempts/{attempt_id}/reingest")
def reingest_attempt(attempt_id: str, db: Session = Depends(get_db)) -> dict:
    """Re-run ingestion of a completed attempt's structured output (from the stored raw
    payload, else fetched from the provider). Idempotent: only records missed earlier are added."""
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(404)
    output = (attempt.result or {}).get("raw")
    if not output and attempt.provider_session_id:
        output = get_scheduler().client.get_session(attempt.provider_session_id).structured_output
    if not output:
        raise HTTPException(409, "no structured output available for this attempt")
    counts = get_scheduler().ingestor.ingest(db, attempt, output)
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"added": counts, "result": attempt.result}


# -- ideas, evidence, review ---------------------------------------------------------------------


@router.post("/ideas/{idea_id}/pin")
def pin_idea(idea_id: str, pinned: bool = True, db: Session = Depends(get_db)) -> dict:
    idea = db.get(Idea, idea_id)
    if idea is None:
        raise HTTPException(404)
    idea.pinned = pinned
    emit(db, "idea.pinned", record_type="idea", record_id=idea.id, payload={"pinned": pinned})
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"pinned": idea.pinned}


@router.post("/ideas/{idea_id}/revive")
def revive(
    idea_id: str, reason: str = "collaborator revival", db: Session = Depends(get_db)
) -> dict:
    idea = db.get(Idea, idea_id)
    if idea is None:
        raise HTTPException(404)
    revive_idea(db, idea, reason)
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"scheduling_status": idea.scheduling_status, "generation": idea.generation}


@router.post("/evidence/{evidence_id}/review")
def review_evidence(
    evidence_id: str,
    body: ReviewIn,
    collaborator: Collaborator = Depends(require_collaborator),
    db: Session = Depends(get_db),
) -> dict:
    evidence = db.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(404)
    if body.result not in {"confirmed", "confirmed_refutation", "disputed"}:
        raise HTTPException(422)
    certify_evidence(db, evidence, reviewer=collaborator.name, result=body.result, note=body.note)
    db.commit()
    get_scheduler().publisher.process_outbox(db)
    return {"ok": True}


@router.get("/events")
def private_events(
    since_id: int = 0, limit: int = 500, db: Session = Depends(get_db)
) -> list[dict]:
    from ..models import Event

    rows = db.scalars(
        select(Event).where(Event.id > since_id).order_by(Event.id).limit(limit)
    ).all()
    return [
        {
            "id": e.id,
            "type": e.type,
            "record_type": e.record_type,
            "record_id": e.record_id,
            "payload": e.payload,
            "visibility": e.visibility,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


# -- scheduler & publication ----------------------------------------------------------------------


@router.post("/scheduler/tick")
def scheduler_tick(db: Session = Depends(get_db)) -> dict:
    return get_scheduler().tick(db)


@router.get("/scheduler/status")
def scheduler_status(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    running = db.scalars(select(Attempt).where(Attempt.status.in_(["running", "blocked"]))).all()
    return {
        "provider": get_scheduler().client.provider_name,
        "background_enabled": settings.scheduler_enabled,
        "interval_seconds": settings.scheduler_interval_seconds,
        "lean_available": get_scheduler().ingestor.lean_checker.available(),
        "running_attempts": len(running),
        "modes": list(DEVIN_MODES),
    }


@router.post("/publications/{publication_id}/withdraw", dependencies=[Depends(require_owner)])
def withdraw_publication(
    publication_id: str, body: WithdrawIn, db: Session = Depends(get_db)
) -> dict:
    publication = db.get(Publication, publication_id)
    if publication is None:
        raise HTTPException(404)
    withdraw(db, publication, body.reason)
    db.commit()
    return {"withdrawn_at": publication.withdrawn_at}


# -- collaborators (owner only) --------------------------------------------------------------------


@router.post("/collaborators", dependencies=[Depends(require_owner)])
def add_collaborator(body: CollaboratorIn, db: Session = Depends(get_db)) -> dict:
    key = new_api_key("mlc")
    db.add(Collaborator(name=body.name, role="collaborator", api_key_hash=hash_key(key)))
    db.commit()
    return {"name": body.name, "api_key": key, "note": "shown once; store it securely"}


@router.post("/collaborators/{collaborator_id}/revoke", dependencies=[Depends(require_owner)])
def revoke_collaborator(collaborator_id: str, db: Session = Depends(get_db)) -> dict:
    c = db.get(Collaborator, collaborator_id)
    if c is None or c.role == "owner":
        raise HTTPException(404)
    c.revoked = True
    db.commit()
    return {"revoked": True}


@router.get("/collaborators", dependencies=[Depends(require_owner)])
def list_collaborators(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {"id": c.id, "name": c.name, "role": c.role, "revoked": c.revoked}
        for c in db.scalars(select(Collaborator))
    ]
