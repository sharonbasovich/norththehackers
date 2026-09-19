"""Automatic publication of eligible records to the public read model (docs/plan.md §7.6–7.7).

Rules:
  * Publication is a projection, never a verification step. The evidence label is derived from
    the record's independent status dimensions and copied verbatim onto the public payload.
  * Public payloads are allow-listed field by field; prompts, tokens, usage details, private
    artifacts, and internal errors never appear.
  * A changed record version gets a new Publication row; old versions remain for replay.
  * Withdrawal is explicit and keeps the row (with reason) so the public timeline stays honest.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Campaign, Claim, Event, Evidence, Idea, Problem, Publication, utcnow
from .research import claim_state, idea_status

POLICY_VERSION = "v1"

EVIDENCE_LABELS: dict[str, str] = {
    "lean_verified": "Lean verified",
    "lean_formalization_in_progress": "Lean formalization in progress",
    "informal_proof_candidate": "Informal proof candidate — not formally verified",
    "proof_sketch": "Proof sketch — not verified",
    "counterexample_checked": "Computationally checked on a finite range — not a proof",
    "empirically_supported": "Empirical support only — not a proof",
    "unresolved_conflict": "Reported refutation — awaiting independent check",
    "refuted": "Refuted",
    "untested": "Untested hypothesis",
}

PROBLEM_STATUS_LABELS: dict[str, str] = {
    "unreviewed": "Status unreviewed",
    "reported_open": "Reported open by cited sources",
    "resolution_claimed": "Resolution claimed — under review",
    "resolved": "Resolved (reviewed)",
    "disputed": "Status disputed",
    "unknown": "Status unknown",
}


def evidence_label(status: str) -> str:
    return EVIDENCE_LABELS.get(status, "Untested hypothesis")


def _version(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[
        :32
    ]


class Publisher:
    PUBLISHABLE = {"problem", "idea", "claim", "evidence", "campaign"}

    def process_outbox(self, db: Session) -> int:
        published = 0
        events = db.scalars(
            select(Event).where(Event.processed.is_(False)).order_by(Event.id)
        ).all()
        for event in events:
            if event.record_type in self.PUBLISHABLE and event.record_id:
                if self.project(db, event.record_type, event.record_id, event.id):
                    published += 1
            event.processed = True
        db.commit()
        return published

    def project(
        self, db: Session, record_type: str, record_id: str, event_id: int | None = None
    ) -> bool:
        builders = {
            "problem": self._problem,
            "idea": self._idea,
            "claim": self._claim,
            "evidence": self._evidence,
            "campaign": self._campaign,
        }
        built = builders[record_type](db, record_id)
        if built is None:
            return False
        payload, label = built
        version = _version(payload)
        existing = db.scalar(
            select(Publication).where(
                Publication.record_type == record_type,
                Publication.record_id == record_id,
                Publication.record_version == version,
            )
        )
        if existing is not None:
            return False
        withdrawn = db.scalar(
            select(Publication).where(
                Publication.record_type == record_type,
                Publication.record_id == record_id,
                Publication.withdrawn_at.is_not(None),
            )
        )
        if withdrawn is not None and withdrawn.withdrawal_reason.startswith("permanent:"):
            return False
        db.add(
            Publication(
                record_type=record_type,
                record_id=record_id,
                record_version=version,
                public_payload=payload,
                evidence_label=label,
                policy_version=POLICY_VERSION,
                event_id=event_id,
            )
        )
        db.add(
            Event(
                type="publication.created",
                record_type=record_type,
                record_id=record_id,
                payload={"version": version, "label": label},
                visibility="public",
                processed=True,
            )
        )
        return True

    # -- projections (allow-list only) ---------------------------------------------------------

    def _problem(self, db: Session, problem_id: str) -> tuple[dict, str] | None:
        problem = db.get(Problem, problem_id)
        if problem is None:
            return None
        if problem.origin != "generated" and not problem.assertions:
            return None  # never publish an unsourced literature problem (docs/plan.md §3.2)
        payload = {
            "id": problem.id,
            "slug": problem.slug,
            "title": problem.title,
            "statement": problem.statement,
            "definitions": problem.definitions,
            "assumptions": problem.assumptions,
            "origin": problem.origin,
            "attribution": problem.attribution,
            "status": problem.status,
            "status_checked_at": problem.status_checked_at,
            "formal_target": problem.formal_target,
            "formal_target_status": problem.formal_target_status,
            "areas": [{"slug": a.slug, "name": a.name} for a in problem.areas],
            "sources": [
                {
                    "title": a.source.title,
                    "url": a.source.url,
                    "location": a.location,
                    "asserted_status": a.asserted_status,
                    "asserted_at": a.asserted_at,
                    "retrieved_date": a.source.retrieved_date,
                    "review_state": a.review_state,
                }
                for a in problem.assertions
            ],
            "status_reviews": [
                {k: r.get(k) for k in ("reviewer", "date", "from", "to", "note")}
                for r in problem.coverage.get("status_reviews", [])
            ],
            "reference_formalization": problem.coverage.get("reference_formalization"),
        }
        return payload, PROBLEM_STATUS_LABELS.get(problem.status, "Status unknown")

    def _idea(self, db: Session, idea_id: str) -> tuple[dict, str] | None:
        idea = db.get(Idea, idea_id)
        if idea is None:
            return None
        status, formal = idea_status(idea)
        payload = {
            "id": idea.id,
            "campaign_id": idea.campaign_id,
            "problem_id": idea.campaign.problem_id,
            "title": idea.title,
            "approach": idea.approach,
            "mechanism": idea.mechanism,
            "next_experiment": idea.next_experiment,
            "method_tags": idea.method_tags,
            "generation": idea.generation,
            "depth": idea.depth,
            "parent_ids": [p.id for p in idea.parents],
            "scheduling_status": idea.scheduling_status,
            "evidence_status": status,
            "review_status": idea.review_status,
            "novelty_status": idea.novelty_status,
            "formalization_status": formal,
            "score": round(idea.score, 3),
            "pinned": idea.pinned,
            "claim_ids": [c.id for c in idea.claims],
            "created_at": idea.created_at.isoformat(),
        }
        return payload, evidence_label(status)

    def _claim(self, db: Session, claim_id: str) -> tuple[dict, str] | None:
        claim = db.get(Claim, claim_id)
        if claim is None:
            return None
        status = claim_state(claim)
        payload = {
            "id": claim.id,
            "idea_id": claim.idea_id,
            "campaign_id": claim.campaign_id,
            "statement": claim.statement,
            "scope": claim.scope,
            "lean_declaration": claim.lean_declaration,
            "claim_version": claim.version,
            "content_hash": claim.content_hash,
            "formalization_status": claim.formalization_status,
            "epistemic_status": status,
            "previous_version_id": claim.previous_version_id,
        }
        return payload, evidence_label(status)

    def _evidence(self, db: Session, evidence_id: str) -> tuple[dict, str] | None:
        evidence = db.get(Evidence, evidence_id)
        if evidence is None:
            return None
        details = evidence.details or {}
        payload = {
            "id": evidence.id,
            "idea_id": evidence.idea_id,
            "claim_id": evidence.claim_id,
            "problem_id": evidence.problem_id,
            "claim_version": evidence.claim_version,
            "check_type": evidence.check_type,
            "result": evidence.result,
            "summary": evidence.summary,
            "coverage": evidence.coverage,
            "verifier": evidence.verifier,
            "verifier_version": evidence.verifier_version,
            "certified": evidence.certified,
            "assumptions": details.get("assumptions", []),
            "axioms": details.get("axioms", []),
            "reasons": details.get("reasons", []),
            "check_status": details.get("status"),
            "approved_target": details.get("approved_target"),
            "target_origin": details.get("target_origin"),
            "toolchain": details.get("toolchain"),
            "artifact_id": evidence.artifact_id,
            "created_at": evidence.created_at.isoformat(),
        }
        if evidence.certified and evidence.result == "verified":
            label = "Lean verified"
        elif evidence.check_type == "lean_check":
            label = (
                "Lean check rejected" if evidence.result == "rejected" else "Lean check unavailable"
            )
        else:
            label = f"Worker-reported {evidence.check_type} — not independently certified"
        return payload, label

    def _campaign(self, db: Session, campaign_id: str) -> tuple[dict, str] | None:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return None
        payload = {
            "id": campaign.id,
            "problem_id": campaign.problem_id,
            "state": campaign.state,
            "research_outcome": campaign.research_outcome or "researching",
            "generation": campaign.generation,
            "session_budget": campaign.session_budget,
            "sessions_used": campaign.sessions_used,
            "policy_version": campaign.policy_version,
            "default_mode": (campaign.policy or {}).get("default_mode", "ultra"),
            "created_at": campaign.created_at.isoformat(),
        }
        return payload, f"Campaign {campaign.state}"


def withdraw(db: Session, publication: Publication, reason: str) -> None:
    publication.withdrawn_at = utcnow()
    publication.withdrawal_reason = reason
    db.add(
        Event(
            type="publication.withdrawn",
            record_type=publication.record_type,
            record_id=publication.record_id,
            payload={"reason": reason},
            visibility="public",
            processed=True,
        )
    )
