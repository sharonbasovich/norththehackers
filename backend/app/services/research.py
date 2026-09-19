"""Shared mathematical memory and a portfolio-wide research frontier.

Campaigns remain budget accounts. Claims and proposed applications are shared within a
portfolio; an adopted application is a research decision, never a certificate of truth.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Attempt, Campaign, Claim, Idea, Relation, SelectionDecision
from .events import emit

OPEN_ATTEMPTS = {"queued", "dispatching", "running", "blocked"}
LINK_KINDS = {"applies_to", "depends_on", "contradicts", "equivalent_to"}


def pool_campaigns(db: Session, campaign: Campaign, *, isolated: bool = False) -> list[Campaign]:
    if isolated or not (campaign.policy or {}).get("shared_research", True):
        return [campaign]
    return [
        c
        for c in db.scalars(select(Campaign).where(Campaign.portfolio_id == campaign.portfolio_id))
        if (c.policy or {}).get("shared_research", True)
    ]


def scope_ids(db: Session, attempt: Attempt) -> set[str]:
    return {
        c.id for c in pool_campaigns(db, attempt.campaign, isolated=bool(attempt.comparison_group))
    }


def claim_state(claim: Claim) -> str:
    evidence = [e for e in claim.evidence if e.claim_version == claim.version]
    proven = any(e.certified and e.result == "verified" for e in evidence)
    refuted = any(
        (e.certified and e.result == "refutes")
        or (e.details or {}).get("review", {}).get("result") == "confirmed_refutation"
        for e in evidence
    )
    disputed = any(e.result == "refutes" and e.check_type != "lean_attempt" for e in evidence)
    if proven and disputed:
        return "unresolved_conflict"
    if refuted:
        return "refuted"
    if disputed:
        return "unresolved_conflict"
    if proven:
        return "lean_verified"
    if any(e.result == "supports" for e in evidence):
        return "empirically_supported"
    return "untested"


def idea_status(idea: Idea) -> tuple[str, str]:
    """Derive verification from all current claims, including for stale stored labels."""
    states = [claim_state(c) for c in idea.claims]
    if states and all(state == "lean_verified" for state in states):
        return "lean_verified", "complete"
    status, formal = idea.evidence_status, idea.formalization_status
    if "unresolved_conflict" in states:
        status = "unresolved_conflict"
    elif status == "lean_verified":
        status = "lean_formalization_in_progress" if states else "untested"
    elif "lean_verified" in states and status not in {"refuted", "unresolved_conflict"}:
        status = "lean_formalization_in_progress"
    if formal == "complete" or "lean_verified" in states:
        formal = "in_progress" if states else "absent"
    return status, formal


def claim_revision(claim: Claim) -> str:
    data = [
        claim.content_hash,
        claim.version,
        claim_state(claim),
        sorted(e.id for e in claim.evidence),
    ]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:20]


def dependency_gaps(claim_id: str, claims: dict[str, Claim], links: list[Relation]) -> dict:
    missing: set[str] = set()
    cycles: set[str] = set()
    visited: set[str] = set()

    def visit(current: str, path: set[str]) -> None:
        if current in path:
            cycles.add(current)
            return
        if current in visited:
            return
        visited.add(current)
        for link in links:
            if link.kind != "depends_on" or link.source_id != current:
                continue
            dependency = claims.get(link.target_id)
            if dependency is not None and claim_state(dependency) != "lean_verified":
                missing.add(dependency.id)
                visit(dependency.id, path | {current})

    visit(claim_id, set())
    return {
        "unresolved": sorted(missing),
        "cycles": sorted(cycles),
        "refuted": sorted(cid for cid in missing if claim_state(claims[cid]) == "refuted"),
    }


def pool_claims(db: Session, campaigns: list[Campaign]) -> list[Claim]:
    return list(db.scalars(select(Claim).where(Claim.campaign_id.in_([c.id for c in campaigns]))))


def pool_relations(db: Session, claims: list[Claim], campaigns: list[Campaign]) -> list[Relation]:
    claim_ids = {c.id for c in claims}
    problem_ids = {c.problem_id for c in campaigns}
    return [
        r
        for r in db.scalars(select(Relation).where(Relation.kind.in_(LINK_KINDS)))
        if r.source_type == "claim"
        and r.source_id in claim_ids
        and (
            (r.target_type == "claim" and r.target_id in claim_ids)
            or (r.target_type == "problem" and r.target_id in problem_ids)
        )
    ]


def record_link(
    db: Session,
    *,
    source: Claim,
    target_id: str,
    kind: str,
    status: str,
    reason: str,
    attempt_id: str | None = None,
    assumptions: list | None = None,
) -> Relation:
    target_type = "problem" if kind == "applies_to" else "claim"
    relation = db.scalar(
        select(Relation).where(
            Relation.source_id == source.id,
            Relation.source_type == "claim",
            Relation.target_id == target_id,
            Relation.target_type == target_type,
            Relation.kind == kind,
        )
    )
    revision = claim_revision(source)
    entry = {
        "attempt_id": attempt_id,
        "status": status,
        "reason": reason,
        "assumptions": assumptions or [],
        "claim_revision": revision,
    }
    if relation is None:
        relation = Relation(
            layer="association" if kind == "applies_to" else "dependency",
            kind=kind,
            source_type="claim",
            source_id=source.id,
            target_type=target_type,
            target_id=target_id,
            status=status,
            provenance={"history": [entry], **entry},
        )
        db.add(relation)
    else:
        history = list((relation.provenance or {}).get("history", []))
        if entry in history:
            return relation
        # An automatic suggestion cannot erase a worker's reviewed application decision.
        if attempt_id is None:
            return relation
        relation.status = status
        relation.provenance = {"history": [*history, entry], **entry}
    db.flush()
    if (
        kind == "applies_to"
        and status == "adopted"
        and source.idea is not None
        and source.idea.scheduling_status == "archived"
        and claim_state(source) != "refuted"
    ):
        source.idea.scheduling_status = "active"
        source.idea.generation = source.idea.campaign.generation
        db.add(
            SelectionDecision(
                campaign_id=source.campaign_id,
                generation=source.idea.generation,
                idea_id=source.idea.id,
                decision="revived",
                reason="New cross-problem application: " + reason,
                policy_version=source.idea.campaign.policy_version,
            )
        )
        emit(
            db,
            "research.branch_revived",
            record_type="idea",
            record_id=source.idea.id,
            payload={"claim_id": source.id, "target_problem_id": target_id},
            visibility="public",
        )
    emit(
        db,
        "research.link_recorded",
        record_type="claim",
        record_id=source.id,
        payload={
            "relation_id": relation.id,
            "kind": kind,
            "status": status,
            "target_id": target_id,
        },
        visibility="public",
    )
    return relation


def ingest_links(db: Session, attempt: Attempt, output: dict, id_map: dict[str, str]) -> list[dict]:
    allowed = scope_ids(db, attempt)
    problem_ids = {
        c.problem_id for c in db.scalars(select(Campaign).where(Campaign.id.in_(allowed)))
    }
    rejected = []
    for raw in output.get("research_links", []) or []:
        source_id = id_map.get(str(raw.get("claim", "")).lower(), raw.get("claim"))
        source = db.get(Claim, source_id) if source_id else None
        kind, status = raw.get("kind"), raw.get("status", "proposed")
        target_id = id_map.get(str(raw.get("target", "")).lower(), raw.get("target"))
        valid_target = target_id in problem_ids if kind == "applies_to" else False
        if kind != "applies_to" and target_id:
            target = db.get(Claim, target_id)
            valid_target = target is not None and target.campaign_id in allowed
        if (
            source is None
            or source.campaign_id not in allowed
            or not valid_target
            or kind not in LINK_KINDS
            or status not in {"proposed", "adopted", "rejected", "inconclusive"}
            or source_id == target_id
            or not raw.get("reason")
        ):
            rejected.append(raw)
            continue
        # Dependencies/equivalences/conflicts are proposed mathematical structure, not certified.
        if kind != "applies_to":
            status = "proposed"
        record_link(
            db,
            source=source,
            target_id=target_id,
            kind=kind,
            status=status,
            reason=str(raw["reason"]),
            attempt_id=attempt.id,
            assumptions=raw.get("assumptions", []),
        )
    return rejected


def discover_connections(db: Session, campaigns: list[Campaign], claims: list[Claim]) -> None:
    """Cheap retrieval proposes candidates only. Workers must test the actual assumptions."""
    by_campaign = {c.id: c for c in campaigns}
    existing = {
        (r.source_id, r.target_id)
        for r in pool_relations(db, claims, campaigns)
        if r.kind == "applies_to"
    }
    for claim in claims:
        if claim_state(claim) == "refuted":
            continue
        origin = by_campaign[claim.campaign_id]
        words = set(re.findall(r"[a-z]{4,}", claim.statement.lower()))
        areas = {a.id for a in origin.problem.areas}
        candidates = []
        for campaign in campaigns:
            if campaign.problem_id == origin.problem_id:
                continue
            overlap = words & set(re.findall(r"[a-z]{4,}", campaign.problem.statement.lower()))
            if areas & {a.id for a in campaign.problem.areas} or len(overlap) >= 2:
                candidates.append((len(overlap), campaign))
        # Keep broad subject matches from producing an all-to-all review queue. Synthesis
        # workers can propose additional applications beyond this cheap retrieval shortlist.
        for _, campaign in sorted(candidates, key=lambda item: (-item[0], item[1].problem_id))[:3]:
            if (claim.id, campaign.problem_id) not in existing:
                record_link(
                    db,
                    source=claim,
                    target_id=campaign.problem_id,
                    kind="applies_to",
                    status="proposed",
                    reason="Shared subject or statement terms; applicability untested.",
                )
                existing.add((claim.id, campaign.problem_id))


def memory(
    db: Session,
    campaign: Campaign,
    *,
    isolated: bool = False,
    focus_claim_id: str | None = None,
    limit: int = 40,
) -> dict:
    campaigns = pool_campaigns(db, campaign, isolated=isolated)
    claims = pool_claims(db, campaigns)
    relations = pool_relations(db, claims, campaigns)
    relevant = {r.source_id for r in relations if r.target_id == campaign.problem_id}
    if focus_claim_id:
        relevant.add(focus_claim_id)
    # Include the complete dependency neighborhood of focused/relevant claims before other work.
    for _ in range(len(claims)):
        expanded = relevant | {
            r.target_id for r in relations if r.kind == "depends_on" and r.source_id in relevant
        }
        if expanded == relevant:
            break
        relevant = expanded
    claims.sort(
        key=lambda c: (
            c.id != focus_claim_id,
            c.id not in relevant,
            claim_state(c) != "lean_verified",
            c.created_at,
        ),
        reverse=False,
    )
    selected = claims[:limit]
    selected_ids = {c.id for c in selected}
    recent_attempts = list(
        db.scalars(
            select(Attempt)
            .where(Attempt.campaign_id.in_([c.id for c in campaigns]), Attempt.result.is_not(None))
            .order_by(Attempt.created_at.desc())
            .limit(12)
        )
    )

    def evidence(c: Claim) -> list[dict]:
        return [
            {
                "id": e.id,
                "check_type": e.check_type,
                "result": e.result,
                "certified": e.certified,
                "summary": e.summary[:2000],
                "coverage": e.coverage,
                "artifact_id": e.artifact_id,
                "assumptions": (e.details or {}).get("assumptions", []),
            }
            for e in c.evidence[-8:]
        ]

    return {
        "portfolio_id": campaign.portfolio_id,
        "problems": [
            {
                "id": c.problem_id,
                "title": c.problem.title,
                "statement": c.problem.statement[:5000],
                "assumptions": c.problem.assumptions,
                "formal_target": c.problem.formal_target,
                "campaign_id": c.id,
                "state": c.state,
            }
            for c in campaigns
        ],
        "claims": [
            {
                "id": c.id,
                "origin_campaign_id": c.campaign_id,
                "statement": c.statement,
                "scope": c.scope,
                "lean_declaration": c.lean_declaration,
                "state": claim_state(c),
                "version": c.version,
                "evidence": evidence(c),
                "dependency_gaps": dependency_gaps(
                    c.id, {item.id: item for item in claims}, relations
                ),
            }
            for c in selected
        ],
        "links": [
            {
                "id": r.id,
                "claim": r.source_id,
                "target": r.target_id,
                "kind": r.kind,
                "status": r.status,
                "reason": (r.provenance or {}).get("reason", ""),
                "assumptions": (r.provenance or {}).get("assumptions", []),
            }
            for r in relations
            if r.source_id in selected_ids
        ],
        "failed_directions": [
            {
                "id": i.id,
                "problem_id": c.problem_id,
                "title": i.title,
                "approach": i.approach[:1500],
                "state": i.evidence_status,
                "evidence": [
                    {"summary": e.summary[:1000], "result": e.result, "certified": e.certified}
                    for e in i.evidence[-3:]
                ],
            }
            for c in campaigns
            for i in c.ideas
            if i.scheduling_status == "archived"
            or i.evidence_status in {"refuted", "unresolved_conflict"}
        ][-20:],
        "total_claims": len(claims),
        "bottlenecks": [
            {
                "attempt_id": a.id,
                "campaign_id": a.campaign_id,
                "role": a.role,
                "focus_claim_id": (a.model_metadata or {}).get("focus_claim_id"),
                "gaps": (a.result or {}).get("gaps", [])[:8],
            }
            for a in recent_attempts
            if (a.result or {}).get("gaps")
        ],
        "omitted_claims": max(0, len(claims) - len(selected)),
        "rule": "Adopted means useful to investigate. Only certified claim evidence is proof.",
    }


@dataclass
class ResearchTask:
    campaign: Campaign
    role: str
    key: str
    priority: float
    metadata: dict = field(default_factory=dict)


def frontier(db: Session, campaigns: list[Campaign]) -> list[ResearchTask]:
    """Build tasks from mathematical changes, not the number of scheduler ticks."""
    if len({c.problem_id for c in campaigns}) < 2:
        return []
    claims = pool_claims(db, campaigns)
    discover_connections(db, campaigns, claims)
    links = pool_relations(db, claims, campaigns)
    attempts = list(
        db.scalars(select(Attempt).where(Attempt.campaign_id.in_([c.id for c in campaigns])))
    )
    eligible = [
        c
        for c in campaigns
        if c.state == "active"
        and c.research_outcome != "proven"
        and (c.policy or {}).get("auto_plan", True)
        and c.problem.status not in {"resolution_claimed", "resolved"}
        and c.sessions_used + sum(a.status == "queued" for a in attempts if a.campaign_id == c.id)
        < c.session_budget
    ]
    eligible.sort(key=lambda c: (c.sessions_used, c.created_at))
    if not eligible:
        return []
    tasks: list[ResearchTask] = []

    def add(campaign: Campaign, role: str, key: str, priority: float, **metadata: object) -> None:
        matching = [a for a in attempts if (a.model_metadata or {}).get("research_task_key") == key]
        if (
            any(a.status in OPEN_ATTEMPTS or a.status == "completed" for a in matching)
            or len(matching) >= 2
        ):
            return
        focus = metadata.get("focus_claim_id")
        # Repeated inconclusive work cannot monopolize a shared claim's budget. A changed
        # statement creates a new claim and therefore a new opportunity.
        if focus and sum(
            a.role == role and (a.model_metadata or {}).get("focus_claim_id") == focus
            for a in attempts
        ) >= (2 if role == "counterexample_hunter" else 8):
            return
        if focus and any(
            a.status in OPEN_ATTEMPTS
            and (
                (a.model_metadata or {}).get("focus_claim_id") == focus
                or (
                    by_id.get(str(focus)) is not None
                    and a.idea_id is not None
                    and by_id[str(focus)].idea_id == a.idea_id
                )
            )
            for a in attempts
        ):
            return
        tasks.append(ResearchTask(campaign, role, key, priority, dict(metadata)))

    by_id = {c.id: c for c in claims}
    for claim in claims:
        state = claim_state(claim)
        applications = [r for r in links if r.source_id == claim.id and r.kind == "applies_to"]
        consumers = {r.target_id for r in applications if r.status == "adopted"}
        dependents = [r for r in links if r.kind == "depends_on" and r.target_id == claim.id]
        interested = [c for c in eligible if c.problem_id in consumers or c.id == claim.campaign_id]
        sponsor = (interested or eligible)[0]
        revision = claim_revision(claim)
        conflicts = [
            r for r in links if r.kind == "contradicts" and claim.id in {r.source_id, r.target_id}
        ]
        if state == "unresolved_conflict" or conflicts:
            add(
                sponsor,
                "counterexample_hunter",
                f"challenge:{claim.id}:{revision}",
                100,
                focus_claim_id=claim.id,
                reason="Resolve conflicting evidence before reuse.",
            )
        if state not in {"lean_verified", "refuted", "unresolved_conflict"} and (
            len(consumers) >= 2 or dependents
        ):
            gaps = dependency_gaps(claim.id, by_id, links)
            role = "lemma_prover" if claim.lean_declaration else "lemma_architect"
            if gaps["cycles"] or gaps["refuted"]:
                role = "lemma_architect"
            elif gaps["unresolved"]:
                role = ""
            # One attempt per statement version; a revised lemma opens a fresh research direction.
            if role:
                add(
                    sponsor,
                    role,
                    f"{role}:{claim.id}:{claim.content_hash}",
                    70 + 5 * len(consumers),
                    focus_claim_id=claim.id,
                    reason="Resolve a shared lemma or repair its blocked dependency skeleton.",
                )
        for link in applications:
            target = next((c for c in eligible if c.problem_id == link.target_id), None)
            if (
                target
                and state != "refuted"
                and (
                    link.status == "proposed"
                    or (link.provenance or {}).get("claim_revision") != revision
                )
            ):
                add(
                    target,
                    "connection_reviewer",
                    f"transfer:{link.id}:{revision}",
                    55,
                    focus_claim_id=claim.id,
                    relation_id=link.id,
                    target_problem_id=target.problem_id,
                    reason="Check whether this claim's scope and assumptions help the target.",
                )

    for campaign in eligible:
        adopted = [
            r
            for r in links
            if r.kind == "applies_to"
            and r.status == "adopted"
            and r.target_id == campaign.problem_id
            and claim_state(by_id[r.source_id]) == "lean_verified"
        ]
        if campaign.problem.formal_target and adopted:
            signature = hashlib.sha256(
                json.dumps(
                    sorted((r.source_id, claim_revision(by_id[r.source_id])) for r in adopted)
                ).encode()
            ).hexdigest()[:20]
            add(
                campaign,
                "proof_closer",
                f"closure:{campaign.id}:{signature}",
                85,
                reason="Assemble verified shared lemmas into the target; expose remaining gaps.",
            )

    # Reserve regular synthesis even when cheap retrieval found no vocabulary overlap.
    discoveries = len(claims) + sum(claim_state(c) in {"lean_verified", "refuted"} for c in claims)
    previous = [a for a in attempts if a.role == "synthesizer" and not a.comparison_group]
    last_size = max(
        (int((a.model_metadata or {}).get("discovery_count", 0)) for a in previous), default=0
    )
    if not previous or discoveries - last_size >= 6:
        if not any(a.status in OPEN_ATTEMPTS for a in previous):
            add(
                eligible[0],
                "synthesizer",
                f"synthesis:{eligible[0].portfolio_id}:{discoveries}",
                60,
                discovery_count=discoveries,
                reason="Explore the pool; propose shared lemmas and combine or reject ideas.",
            )
    return sorted(tasks, key=lambda t: (-t.priority, t.campaign.sessions_used, t.key))
