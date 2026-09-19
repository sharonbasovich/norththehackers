"""Portfolio/campaign scheduler (docs/plan.md §6–7).

One tick:
  1. reconcile running attempts with the provider and ingest finished output,
  2. plan new bounded assignments for each active campaign,
  3. dispatch queued attempts within portfolio concurrency and campaign session budgets,
  4. run generation selection when a generation's work is complete,
  5. project eligible records to the public read model.

Every state change goes through the outbox so the UI and publication projector can replay it.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import hash_key
from ..config import Settings
from ..models import (
    Attempt,
    Campaign,
    Evidence,
    Idea,
    Portfolio,
    SelectionDecision,
    utcnow,
)
from .devin_client import DEVIN_MODES, DevinClient
from .events import emit
from .ingest import Ingestor
from .prompts import ROLE_INSTRUCTIONS, build_prompt
from .publication import Publisher
from .research import claim_state, frontier, memory, pool_campaigns, pool_claims, pool_relations
from .selection import Candidate, select_generation

RUNNING = {"queued", "dispatching", "running", "blocked"}
NOT_FORMALIZABLE = {"refuted", "unresolved_conflict", "lean_verified"}
DEFAULT_POLICY = {
    "default_mode": "ultra",
    # per-role overrides; see docs/pilot-fusion-vs-ultra.md for why tool-heavy roles get fusion
    "role_modes": {"experimenter": "fusion", "status_researcher": "fusion"},
    # Lean iteration is slow; formalizers get more headroom than the deployment default cap
    "role_acu_limits": {"prover_formalizer": 15},
    "ideas_per_generation": 3,
    "keep_total": 6,
    "keep_per_cluster": 2,
    "promote_top": 2,
    "auto_plan": True,
    "max_retries": 2,
    "parallel_branches": 2,
    "shared_research": True,
}


def policy_of(campaign: Campaign) -> dict:
    return {**DEFAULT_POLICY, **(campaign.policy or {})}


def mode_for_role(policy: dict, role: str) -> str:
    mode = (policy.get("role_modes") or {}).get(role, policy["default_mode"])
    return str(mode) if mode in DEVIN_MODES else str(policy["default_mode"])


class Scheduler:
    def __init__(
        self, settings: Settings, client: DevinClient, ingestor: Ingestor, publisher: Publisher
    ):
        self.settings = settings
        self.client = client
        self.ingestor = ingestor
        self.publisher = publisher
        self._tick_lock = threading.Lock()

    # -- public entry point --------------------------------------------------------------------

    def tick(self, db: Session) -> dict:
        # The manual endpoint and background loop share this scheduler. Do not dispatch
        # the same queued work twice when both tick at once in the supported single process.
        with self._tick_lock:
            return self._tick(db)

    def _tick(self, db: Session) -> dict:
        summary = {"reconciled": 0, "planned": 0, "dispatched": 0, "selected": 0, "published": 0}
        summary["reconciled"] = self.reconcile(db)
        for portfolio in db.scalars(select(Portfolio).where(Portfolio.paused.is_(False))):
            campaigns = list(
                db.scalars(select(Campaign).where(Campaign.portfolio_id == portfolio.id))
            )
            shared = [c for c in campaigns if policy_of(c)["shared_research"]]
            for task in frontier(db, shared):
                if not self._has_slot(db, task.campaign):
                    continue
                self.enqueue(
                    db,
                    task.campaign,
                    role=task.role,
                    idea=None,
                    parents=[],
                    mode=mode_for_role(policy_of(task.campaign), task.role),
                    metadata={
                        **task.metadata,
                        "research_task_key": task.key,
                        "priority": task.priority,
                    },
                )
                summary["planned"] += 1
                break  # retain capacity for branch exploration on every tick
        for campaign in db.scalars(select(Campaign).where(Campaign.state == "active")):
            owning_portfolio = db.get(Portfolio, campaign.portfolio_id)
            if owning_portfolio is None or owning_portfolio.paused:
                continue
            summary["planned"] += self.plan(db, campaign)
        summary["dispatched"] = self.dispatch(db)
        for campaign in db.scalars(select(Campaign).where(Campaign.state == "active")):
            summary["selected"] += self.maybe_select(db, campaign)
        db.commit()
        summary["published"] = self.publisher.process_outbox(db)
        return summary

    # -- reconcile -----------------------------------------------------------------------------

    def reconcile(self, db: Session) -> int:
        count = 0
        attempts = db.scalars(
            select(Attempt).where(Attempt.status.in_(["running", "blocked"]))
        ).all()
        for attempt in attempts:
            if not attempt.provider_session_id:
                continue
            try:
                info = self.client.get_session(attempt.provider_session_id)
            except Exception as exc:  # provider hiccup: keep lease, record, retry next tick
                attempt.error = f"poll failed: {exc}"[:2000]
                continue
            attempt.reported_mode = info.devin_mode
            attempt.status_detail = info.status_detail or info.status
            if info.acus_consumed is not None:
                attempt.usage = {**attempt.usage, "acus_consumed": info.acus_consumed}
            if info.user_id:
                attempt.usage = {**attempt.usage, "attributed_user_id": info.user_id}
            if info.is_terminal:
                self._finish(db, attempt, info.structured_output, info.status)
                count += 1
            elif info.is_blocked:
                attempt.status = "blocked"
                emit(
                    db,
                    "attempt.blocked",
                    record_type="attempt",
                    record_id=attempt.id,
                    payload={"detail": attempt.status_detail},
                )
            elif attempt.lease_expires_at and attempt.lease_expires_at < utcnow():
                attempt.status = "timed_out"
                attempt.finished_at = utcnow()
                emit(db, "attempt.timed_out", record_type="attempt", record_id=attempt.id)
                count += 1
        db.commit()
        return count

    def _finish(
        self, db: Session, attempt: Attempt, output: dict | None, provider_status: str
    ) -> None:
        attempt.finished_at = utcnow()
        if output:
            self.ingestor.ingest(db, attempt, output)
            attempt.status = "completed"
            if provider_status == "running" and attempt.provider_session_id:
                try:
                    self.client.terminate_session(attempt.provider_session_id)
                except Exception as exc:  # already gone or provider hiccup; output is saved
                    attempt.error = f"terminate after completion failed: {exc}"[:2000]
        elif provider_status == "error":
            attempt.status = "failed"
            attempt.error = attempt.error or "provider reported error"
        else:
            attempt.status = "completed_without_output"
        emit(
            db,
            "attempt.finished",
            record_type="attempt",
            record_id=attempt.id,
            payload={
                "status": attempt.status,
                "requested_mode": attempt.requested_mode,
                "reported_mode": attempt.reported_mode,
                "usage": attempt.usage,
            },
        )

    # -- plan ----------------------------------------------------------------------------------

    def plan(self, db: Session, campaign: Campaign) -> int:
        policy = policy_of(campaign)
        if not policy["auto_plan"]:
            return 0
        if campaign.research_outcome == "proven":
            for pending in self._open_attempts(db, campaign):
                if pending.status == "queued":
                    pending.status = "cancelled"
            if not self._open_attempts(db, campaign):
                campaign.state = "completed"
            return 0
        if campaign.problem.status in {"resolution_claimed", "resolved"}:
            if not self._open_attempts(db, campaign):
                campaign.state = "paused"
                emit(
                    db,
                    "campaign.paused",
                    record_type="campaign",
                    record_id=campaign.id,
                    payload={"reason": f"problem status {campaign.problem.status}"},
                    visibility="public",
                )
            return 0
        if campaign.sessions_used >= campaign.session_budget:
            for pending in self._open_attempts(db, campaign):
                if pending.status == "queued":
                    pending.status = "cancelled"
                    pending.error = "Campaign session budget exhausted before dispatch"
            open_attempts = self._open_attempts(db, campaign)
            if not open_attempts:
                campaign.state = "completed"
                shared_claims = pool_claims(db, pool_campaigns(db, campaign))
                useful = {
                    r.source_id
                    for r in pool_relations(db, shared_claims, pool_campaigns(db, campaign))
                    if r.kind == "applies_to"
                    and r.status == "adopted"
                    and r.target_id == campaign.problem_id
                }
                campaign.research_outcome = (
                    "unresolved_with_progress"
                    if any(
                        claim_state(c) == "lean_verified"
                        and (c.campaign_id == campaign.id or c.id in useful)
                        for c in shared_claims
                    )
                    else "unresolved"
                )
                emit(
                    db,
                    "campaign.completed",
                    record_type="campaign",
                    record_id=campaign.id,
                    payload={"reason": "session budget exhausted"},
                    visibility="public",
                )
            return 0
        open_attempts = self._open_attempts(db, campaign)
        if not self._has_slot(db, campaign):
            return 0
        if any(a.role in {"hypothesis_generator", "synthesizer"} for a in open_attempts):
            return 0
        active = [
            i
            for i in campaign.ideas
            if i.scheduling_status in {"active", "promoted"} and i.generation == campaign.generation
        ]
        shared_ids = [c.id for c in pool_campaigns(db, campaign)]
        busy_shared_claims = {
            (a.model_metadata or {}).get("focus_claim_id")
            for a in db.scalars(
                select(Attempt).where(
                    Attempt.campaign_id.in_(shared_ids), Attempt.status.in_(RUNNING)
                )
            )
        } - {None}
        if not active:
            if open_attempts:
                return 0
            parents = [i for i in campaign.ideas if i.scheduling_status == "promoted"]
            if not parents:
                # Nothing earned promotion: refine the best surviving (unrefuted) ideas rather
                # than restarting from scratch, so lineage never breaks.
                survivors = [
                    i
                    for i in campaign.ideas
                    if i.scheduling_status == "active" and i.evidence_status != "refuted"
                ]
                parents = sorted(survivors, key=lambda i: (i.pinned is False, -i.score))[
                    : policy["promote_top"]
                ]
            # Survivors of a cull are deepened first: a formalizer tries to land a Lean-verified
            # piece of each promoted idea before the next generation branches from them.
            for idea in sorted(parents, key=lambda i: (i.pinned is False, -i.score)):
                if self._wants_formalizer(idea) and not any(
                    c.id in busy_shared_claims for c in idea.claims
                ):
                    self.enqueue(
                        db,
                        campaign,
                        role="prover_formalizer",
                        idea=idea,
                        parents=[],
                        mode=mode_for_role(policy, "prover_formalizer"),
                    )
                    return 1
            self.enqueue(
                db,
                campaign,
                role="hypothesis_generator",
                idea=None,
                parents=parents,
                mode=policy["default_mode"],
            )
            return 1
        ranked = sorted(
            (i for i in active if not any(c.id in busy_shared_claims for c in i.claims)),
            key=lambda i: (i.pinned is False, -i.score),
        )
        busy = {a.idea_id for a in open_attempts}
        busy.update(
            i for a in open_attempts for i in (a.model_metadata or {}).get("review_idea_ids", [])
        )
        next_roles = [(idea, self._next_role(idea)) for idea in ranked if idea.id not in busy]
        # Critiques wait until the generation's other work is done and then run as one
        # session over every idea; culling needs all of them critiqued, and per-idea critic
        # sessions would spend the budget before that point.
        for idea, role in next_roles:
            if role and role != "critic":
                self.enqueue(
                    db,
                    campaign,
                    role=role,
                    idea=idea,
                    parents=[],
                    mode=mode_for_role(policy, role),
                )
                return 1
        needing_critique = [idea for idea, role in next_roles if role == "critic"]
        if open_attempts:
            return 0  # critique the generation after its concurrent experiments finish
        if len(needing_critique) > 1:
            self.enqueue(
                db,
                campaign,
                role="critic",
                idea=None,
                parents=[],
                mode=mode_for_role(policy, "critic"),
                review=needing_critique,
            )
            return 1
        if needing_critique:
            self.enqueue(
                db,
                campaign,
                role="critic",
                idea=needing_critique[0],
                parents=[],
                mode=mode_for_role(policy, "critic"),
            )
            return 1
        return 0

    @staticmethod
    def _next_role(idea: Idea) -> str | None:
        """Cheapest informative next step for an idea in the current generation."""
        types = {e.check_type for e in idea.evidence}
        if (
            idea.evidence_status == "untested"
            and "critique" not in types
            and "numerical_experiment" not in types
            and "counterexample_search" not in types
        ):
            return "experimenter" if idea.next_experiment else "critic"
        if "critique" not in types:
            return "critic"
        formal_claims = [
            c
            for c in idea.claims
            if c.lean_declaration and c.formalization_status not in {"complete", "blocked"}
        ]
        if (
            idea.evidence_status
            in {"informal_proof_candidate", "proof_sketch", "lean_formalization_in_progress"}
            and formal_claims
            and "lean_attempt" not in types
        ):
            return "prover_formalizer"
        return None

    @staticmethod
    def _wants_formalizer(idea: Idea) -> bool:
        """Promoted ideas without a Lean attempt whose critique did not refute them get one
        formalization pass; the worker picks the strongest provable sub-statement."""
        if not idea.claims or any(e.check_type == "lean_attempt" for e in idea.evidence):
            return False
        if any(e.check_type == "critique" and e.result == "refutes" for e in idea.evidence):
            return False
        return idea.evidence_status not in NOT_FORMALIZABLE

    def _open_attempts(self, db: Session, campaign: Campaign) -> list[Attempt]:
        return list(
            db.scalars(
                select(Attempt).where(
                    Attempt.campaign_id == campaign.id, Attempt.status.in_(RUNNING)
                )
            ).all()
        )

    def _has_slot(self, db: Session, campaign: Campaign) -> bool:
        pending = self._open_attempts(db, campaign)
        return (
            campaign.state == "active"
            and campaign.research_outcome != "proven"
            and len(pending) < max(1, int(policy_of(campaign)["parallel_branches"]))
            and campaign.sessions_used + sum(a.status == "queued" for a in pending)
            < campaign.session_budget
        )

    def enqueue(
        self,
        db: Session,
        campaign: Campaign,
        *,
        role: str,
        idea: Idea | None,
        parents: list[Idea],
        mode: str,
        comparison_group: str = "",
        review: list[Idea] | None = None,
        metadata: dict | None = None,
    ) -> Attempt:
        if mode not in DEVIN_MODES:
            raise ValueError(f"unknown devin mode {mode!r}")
        aliases = {
            "hypothesis_generation": "hypothesis_generator",
            "experiment": "experimenter",
            "critique": "critic",
            "formalization": "prover_formalizer",
            "status_research": "status_researcher",
        }
        role = aliases.get(role, role)
        if role not in ROLE_INSTRUCTIONS:
            raise ValueError(f"unknown research role {role!r}")
        review = review or []
        attempt = Attempt(
            campaign_id=campaign.id,
            idea_id=idea.id if idea else None,
            role=role,
            requested_mode=mode,
            provider=self.client.provider_name,
            status="queued",
            comparison_group=comparison_group,
            model_metadata={
                "parent_idea_ids": [p.id for p in parents[:6]],
                "review_idea_ids": [r.id for r in review],
                **(metadata or {}),
            },
        )
        db.add(attempt)
        db.flush()
        problem = campaign.problem
        active = [i for i in campaign.ideas if i.scheduling_status in {"active", "promoted"}]
        attempt.prompt = build_prompt(
            attempt=attempt,
            campaign=campaign,
            problem=problem,
            active_ideas=active[:12],
            parents=parents[:6],
            worker_api_base=self.settings.public_base_url,
            idea=idea,
            review=review,
            research_context=memory(
                db,
                campaign,
                isolated=bool(comparison_group),
                focus_claim_id=(metadata or {}).get("focus_claim_id"),
            ),
        )
        attempt.prompt_hash = hashlib.sha256(attempt.prompt.encode()).hexdigest()
        emit(
            db,
            "attempt.queued",
            record_type="attempt",
            record_id=attempt.id,
            payload={"campaign_id": campaign.id, "role": role, "requested_mode": mode},
        )
        return attempt

    # -- dispatch ------------------------------------------------------------------------------

    def dispatch(self, db: Session) -> int:
        dispatched = 0
        for portfolio in db.scalars(select(Portfolio).where(Portfolio.paused.is_(False))):
            campaign_ids = [
                c.id
                for c in db.scalars(select(Campaign).where(Campaign.portfolio_id == portfolio.id))
            ]
            if not campaign_ids:
                continue
            in_flight = db.scalars(
                select(Attempt).where(
                    Attempt.campaign_id.in_(campaign_ids),
                    Attempt.status.in_(["dispatching", "running", "blocked"]),
                )
            ).all()
            capacity = portfolio.max_concurrent_sessions - len(in_flight)
            if capacity <= 0:
                continue
            queued = db.scalars(
                select(Attempt)
                .where(Attempt.campaign_id.in_(campaign_ids), Attempt.status == "queued")
                .order_by(Attempt.created_at)
            ).all()
            eligible = [
                a
                for a in queued
                if a.campaign.state == "active"
                and a.campaign.research_outcome != "proven"
                and a.campaign.sessions_used < a.campaign.session_budget
                and a.campaign.problem.status not in {"resolution_claimed", "resolved"}
            ]
            for _ in range(capacity):
                if not eligible:
                    break
                # Every third allocation protects exploration. Remaining slots favor
                # contradictions, proof closure and lemmas with multiple beneficiaries.
                allocations = sum(
                    c.sessions_used
                    for c in db.scalars(select(Campaign).where(Campaign.id.in_(campaign_ids)))
                )
                exploration = [
                    a for a in eligible if a.role in {"hypothesis_generator", "synthesizer"}
                ]
                choices = exploration if allocations % 3 == 2 and exploration else eligible
                attempt = min(
                    choices,
                    key=lambda a: (
                        -float((a.model_metadata or {}).get("priority", 40))
                        - min(60, (utcnow() - a.created_at).total_seconds() / 30),
                        a.campaign.sessions_used,
                        a.created_at,
                    ),
                )
                eligible.remove(attempt)
                campaign = db.get(Campaign, attempt.campaign_id)
                if campaign is None or campaign.state != "active":
                    continue
                if campaign.sessions_used >= campaign.session_budget:
                    continue
                if self._dispatch_one(db, attempt, campaign):
                    dispatched += 1
        db.commit()
        return dispatched

    def _acu_limit(self, campaign: Campaign, role: str) -> int | None:
        """Per-session ACU cap: policy `role_acu_limits[role]`, else policy `max_acu_limit`,
        else the deployment default. `None` means the provider's own limit applies."""
        policy = policy_of(campaign)
        for value in ((policy.get("role_acu_limits") or {}).get(role), policy.get("max_acu_limit")):
            if isinstance(value, int) and value > 0:
                return value
        return self.settings.devin_max_acu_limit

    def _dispatch_one(self, db: Session, attempt: Attempt, campaign: Campaign) -> bool:
        # Reserve before the network call so a crash cannot double-create a session.
        attempt.status = "dispatching"
        campaign.sessions_used += 1
        worker_token = secrets.token_urlsafe(32)
        attempt.worker_token_hash = hash_key(worker_token)
        db.commit()
        try:
            info = self.client.create_session(
                prompt=attempt.prompt,
                devin_mode=attempt.requested_mode,
                tags=[
                    f"campaign:{campaign.id}",
                    f"attempt:{attempt.id}",
                    f"role:{attempt.role}",
                    f"mode:{attempt.requested_mode}",
                    "mathlab",
                ],
                title=f"[mathlab] {attempt.role} — {campaign.problem.title[:60]}",
                session_secrets={"LAB_WORKER_TOKEN": worker_token},
                max_acu_limit=self._acu_limit(campaign, attempt.role),
            )
        except Exception as exc:
            attempt.retries += 1
            attempt.error = f"create_session failed: {exc}"[:2000]
            campaign.sessions_used -= 1
            if attempt.retries > policy_of(campaign)["max_retries"]:
                attempt.status = "failed"
                emit(
                    db,
                    "attempt.failed",
                    record_type="attempt",
                    record_id=attempt.id,
                    payload={"error": attempt.error},
                )
            else:
                attempt.status = "queued"
            db.commit()
            return False
        attempt.provider_session_id = info.session_id
        attempt.provider_session_url = info.url
        attempt.reported_mode = info.devin_mode
        attempt.status = "running"
        attempt.started_at = utcnow()
        attempt.lease_expires_at = utcnow() + timedelta(hours=6)
        if info.devin_mode and info.devin_mode != attempt.requested_mode:
            # Never silently accept a substitute mode (docs/plan.md §7.4).
            attempt.error = f"provider reported mode {info.devin_mode} != requested"
            emit(
                db,
                "attempt.mode_mismatch",
                record_type="attempt",
                record_id=attempt.id,
                payload={"requested": attempt.requested_mode, "reported": info.devin_mode},
            )
        emit(
            db,
            "attempt.dispatched",
            record_type="attempt",
            record_id=attempt.id,
            payload={
                "campaign_id": campaign.id,
                "requested_mode": attempt.requested_mode,
                "reported_mode": info.devin_mode,
                "role": attempt.role,
            },
            visibility="public",
        )
        db.commit()
        return True

    # -- selection -----------------------------------------------------------------------------

    def maybe_select(self, db: Session, campaign: Campaign) -> int:
        if self._open_attempts(db, campaign):
            return 0
        current = [
            i
            for i in campaign.ideas
            if i.generation == campaign.generation and i.scheduling_status in {"active", "promoted"}
        ]
        if not current:
            return 0
        if any(self._next_role(i) for i in current):
            return 0  # generation still has cheap informative work
        policy = policy_of(campaign)
        candidates = []
        campaigns = pool_campaigns(db, campaign)
        claims = pool_claims(db, campaigns)
        links = pool_relations(db, claims, campaigns)
        for idea in current:
            critiques = [e for e in idea.evidence if e.check_type == "critique"]
            cost = sum(
                float((a.usage or {}).get("acus_consumed", 0.0))
                for a in campaign.attempts
                if a.idea_id == idea.id
            )
            idea_claim_ids = {c.id for c in idea.claims}
            transfer_value = len(
                {
                    r.target_id
                    for r in links
                    if r.kind == "applies_to"
                    and r.status == "adopted"
                    and r.source_id in idea_claim_ids
                    and r.target_id != campaign.problem_id
                }
            )
            candidates.append(
                Candidate(
                    idea_id=idea.id,
                    method_tags=list(idea.method_tags),
                    evidence_status=idea.evidence_status,
                    review_status=idea.review_status,
                    formalization_status=idea.formalization_status,
                    depth=idea.depth,
                    pinned=idea.pinned,
                    evidence_count=len(idea.evidence),
                    critique_supports=sum(1 for e in critiques if e.result == "supports"),
                    critique_refutes=sum(1 for e in critiques if e.result == "refutes"),
                    cost=cost,
                    duplicate_of=_duplicate_of(idea, current),
                    transfer_value=transfer_value,
                )
            )
        decisions = select_generation(
            candidates,
            keep_total=policy["keep_total"],
            keep_per_cluster=policy["keep_per_cluster"],
            promote_top=policy["promote_top"],
        )
        by_id = {i.id: i for i in current}
        for decision in decisions:
            idea = by_id[decision.idea_id]
            idea.score = decision.score
            idea.scheduling_status = decision.decision if decision.decision != "kept" else "active"
            db.add(
                SelectionDecision(
                    campaign_id=campaign.id,
                    generation=campaign.generation,
                    idea_id=idea.id,
                    decision=decision.decision,
                    reason=decision.reason,
                    score_components=decision.components,
                    cluster=decision.cluster,
                    policy_version=campaign.policy_version,
                )
            )
            emit(
                db,
                "selection.decided",
                record_type="idea",
                record_id=idea.id,
                payload={
                    "campaign_id": campaign.id,
                    "generation": campaign.generation,
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "score": decision.score,
                },
                visibility="public",
            )
        campaign.generation += 1
        emit(
            db,
            "campaign.generation_advanced",
            record_type="campaign",
            record_id=campaign.id,
            payload={"generation": campaign.generation},
            visibility="public",
        )
        return 1


def _duplicate_of(idea: Idea, siblings: list[Idea]) -> str | None:
    """Deterministic duplicate detection: identical normalized title or approach within a
    generation. Semantic matching is a review aid, not a merge rule (docs/plan.md §3.4)."""
    key = (idea.title.strip().lower(), idea.approach.strip().lower())
    for other in siblings:
        if other.id == idea.id or other.created_at > idea.created_at:
            continue
        if (other.title.strip().lower(), other.approach.strip().lower()) == key:
            return other.id
    return None


def revive_idea(db: Session, idea: Idea, reason: str) -> None:
    idea.scheduling_status = "active"
    idea.generation = idea.campaign.generation
    db.add(
        SelectionDecision(
            campaign_id=idea.campaign_id,
            generation=idea.campaign.generation,
            idea_id=idea.id,
            decision="revived",
            reason=reason,
            policy_version=idea.campaign.policy_version,
        )
    )
    emit(
        db,
        "selection.decided",
        record_type="idea",
        record_id=idea.id,
        payload={"campaign_id": idea.campaign_id, "decision": "revived", "reason": reason},
        visibility="public",
    )


def certify_evidence(
    db: Session, evidence: Evidence, *, reviewer: str, result: str, note: str
) -> None:
    """Collaborator review of worker evidence. Sets review status; can confirm a refutation."""
    evidence.details = {
        **evidence.details,
        "review": {"by": reviewer, "result": result, "note": note},
    }
    idea = evidence.idea
    if idea is None:
        return
    idea.review_status = "expert_reviewed" if result != "disputed" else "disputed"
    if result == "confirmed_refutation":
        idea.evidence_status = "refuted"
        idea.scheduling_status = "archived"
    emit(
        db,
        "evidence.reviewed",
        record_type="evidence",
        record_id=evidence.id,
        payload={"result": result},
        visibility="public",
    )
