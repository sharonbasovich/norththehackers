"""Idempotent ingestion of worker output into the research graph.

Workers propose; the lab records. Worker-submitted evidence is stored uncertified. Only the lab's
Lean checker (and, later, collaborator review) can certify. A worker-reported refutation becomes
`unresolved_conflict` until independently checked.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Attempt, Campaign, Claim, Evidence, Idea, IdeaParent, Relation, utcnow
from .artifacts import store_artifact
from .events import emit
from .lean_checker import LeanChecker, normalize
from .research import idea_status, ingest_links, scope_ids

WORKER_STATUS_MAP: dict[tuple[str, str], str] = {
    ("counterexample_search", "supports"): "counterexample_checked",
    ("counterexample_search", "refutes"): "unresolved_conflict",
    ("numerical_experiment", "supports"): "empirically_supported",
    ("numerical_experiment", "refutes"): "unresolved_conflict",
    ("construction", "supports"): "empirically_supported",
    ("construction", "refutes"): "unresolved_conflict",
    ("proof_sketch", "supports"): "proof_sketch",
    ("informal_proof", "supports"): "informal_proof_candidate",
    ("informal_proof", "refutes"): "unresolved_conflict",
    ("lean_attempt", "supports"): "lean_formalization_in_progress",
    ("lean_attempt", "inconclusive"): "lean_formalization_in_progress",
    ("lean_attempt", "refutes"): "lean_formalization_in_progress",
}

# `I1. Title`, `H2: title`, `(C3) title` -> I1 / H2 / C3
_LEADING_LABEL = re.compile(r"^\(?([A-Za-z]{1,2}\d{1,3})\)?\s*[.:\-–)]\s*")

# Evidence statuses ordered by strength; ingestion never downgrades certified statuses.
STRENGTH = [
    "refuted",
    "untested",
    "unresolved_conflict",
    "empirically_supported",
    "counterexample_checked",
    "proof_sketch",
    "informal_proof_candidate",
    "lean_formalization_in_progress",
    "lean_verified",
]


def content_hash(*parts: str) -> str:
    return hashlib.sha256("\u241f".join(parts).encode()).hexdigest()


def stronger(current: str, proposed: str) -> str:
    if current == "lean_verified" or current == "refuted":
        return current
    if proposed in {"refuted", "lean_verified"}:
        return proposed
    if proposed == "unresolved_conflict" or current == "unresolved_conflict":
        return "unresolved_conflict"
    return proposed if STRENGTH.index(proposed) > STRENGTH.index(current) else current


class Ingestor:
    def __init__(self, artifact_root: Path, lean_checker: LeanChecker):
        self.artifact_root = artifact_root
        self.lean_checker = lean_checker

    def ingest(self, db: Session, attempt: Attempt, output: dict, *, partial: bool = False) -> dict:
        """Merge one structured-output payload. Safe to call repeatedly: ideas are keyed by
        (attempt, title) and evidence by (attempt, target, check_type, summary)."""
        campaign = db.get(Campaign, attempt.campaign_id)
        assert campaign is not None
        created = {"ideas": 0, "claims": 0, "evidence": 0, "lean_checks": 0}
        id_map: dict[str, str] = {}
        unattached: list[dict] = []

        for raw in output.get("ideas", []) or []:
            idea, is_new = self._upsert_idea(db, attempt, campaign, raw)
            if idea is None:
                continue
            created["ideas"] += int(is_new)
            self._register_keys(id_map, raw, idea.id)
            for raw_claim in raw.get("claims", []) or []:
                claim, claim_new = self._upsert_claim(db, campaign, idea, raw_claim)
                if claim is not None:
                    created["claims"] += int(claim_new)
                    self._register_keys(id_map, raw_claim, claim.id, title_key="statement")

        for raw in output.get("evidence", []) or []:
            outcome = self._upsert_evidence(db, attempt, campaign, raw, id_map)
            if outcome is None:
                unattached.append(
                    {k: raw.get(k) for k in ("target", "check_type", "result", "summary")}
                )
                continue
            created["evidence"] += outcome[0]
            created["lean_checks"] += outcome[1]

        rejected_links = ingest_links(db, attempt, output, id_map)

        if not partial:
            attempt.result = {
                "gaps": output.get("gaps", []),
                "self_reported_models": output.get("self_reported_models", ""),
                "counts": created,
                "unattached_evidence": unattached,
                "rejected_research_links": rejected_links,
                "raw": output,
            }
            attempt.result_ingested = True
        emit(
            db,
            "attempt.ingested",
            record_type="attempt",
            record_id=attempt.id,
            payload={"campaign_id": campaign.id, "counts": created, "partial": partial},
        )
        db.expire(campaign, ["ideas"])
        return created

    @staticmethod
    def _register_keys(
        id_map: dict[str, str], raw: dict, record_id: str, *, title_key: str = "title"
    ) -> None:
        """Index a record under every handle a worker plausibly uses as an evidence target:
        its local_id, its full title, and a leading label such as `I1` in `I1. Sum lemma`."""
        for key in (raw.get("local_id"), raw.get(title_key)):
            if isinstance(key, str) and key.strip():
                id_map.setdefault(key.strip().lower(), record_id)
        title = raw.get(title_key)
        if isinstance(title, str):
            label = _LEADING_LABEL.match(title)
            if label:
                id_map.setdefault(label.group(1).lower(), record_id)

    # -- ideas -------------------------------------------------------------------------------

    def _upsert_idea(
        self, db: Session, attempt: Attempt, campaign: Campaign, raw: dict
    ) -> tuple[Idea | None, bool]:
        title = (raw.get("title") or "").strip()
        approach = (raw.get("approach") or "").strip()
        if not title or not approach:
            return None, False
        existing = (
            db.query(Idea)
            .filter(Idea.produced_by_attempt_id == attempt.id, Idea.title == title)
            .one_or_none()
        )
        if existing:
            return existing, False
        parent_ids = [p for p in raw.get("parent_idea_ids", []) or [] if isinstance(p, str)]
        parents = [
            p
            for p in (db.get(Idea, pid) for pid in parent_ids)
            if p is not None and p.campaign_id in scope_ids(db, attempt)
        ]
        if not parents and attempt.idea_id:
            assigned = db.get(Idea, attempt.idea_id)
            if assigned is not None:
                parents = [assigned]
        if not parents:
            # worker omitted lineage: fall back to the parents frozen into the assignment
            assigned_ids = attempt.model_metadata.get("parent_idea_ids", [])
            parents = [
                p
                for p in (db.get(Idea, pid) for pid in assigned_ids)
                if p is not None and p.campaign_id in scope_ids(db, attempt)
            ]
        idea = Idea(
            campaign_id=campaign.id,
            title=title,
            approach=approach,
            mechanism=raw.get("mechanism", "") or "",
            next_experiment=raw.get("next_experiment", "") or "",
            novelty_rationale=raw.get("novelty_rationale", "") or "",
            method_tags=[t for t in raw.get("method_tags", []) or [] if isinstance(t, str)][:8],
            generation=campaign.generation,
            depth=(1 + max(p.depth for p in parents)) if parents else 0,
            produced_by_attempt_id=attempt.id,
        )
        db.add(idea)
        db.flush()
        for parent in parents:
            kind = "combined_from" if len(parents) > 1 else "refined_from"
            db.add(IdeaParent(child_id=idea.id, parent_id=parent.id, kind=kind))
            db.add(
                Relation(
                    layer="lineage",
                    kind=kind,
                    source_type="idea",
                    source_id=idea.id,
                    target_type="idea",
                    target_id=parent.id,
                    status="checked",
                    provenance={"attempt_id": attempt.id},
                )
            )
        if not parents:
            db.add(
                Relation(
                    layer="lineage",
                    kind="proposed_for",
                    source_type="idea",
                    source_id=idea.id,
                    target_type="problem",
                    target_id=campaign.problem_id,
                    status="checked",
                    provenance={"attempt_id": attempt.id},
                )
            )
        emit(
            db,
            "idea.created",
            record_type="idea",
            record_id=idea.id,
            payload={"campaign_id": campaign.id, "generation": idea.generation},
        )
        return idea, True

    def _upsert_claim(
        self, db: Session, campaign: Campaign, idea: Idea, raw: dict
    ) -> tuple[Claim | None, bool]:
        statement = (raw.get("statement") or "").strip()
        if not statement:
            return None, False
        lean_decl = (raw.get("lean_declaration") or "").strip()
        digest = content_hash(statement, raw.get("scope", "") or "", lean_decl)
        existing = (
            db.query(Claim).filter(Claim.idea_id == idea.id, Claim.content_hash == digest).first()
        )
        if existing:
            return existing, False
        claim = Claim(
            campaign_id=campaign.id,
            idea_id=idea.id,
            statement=statement,
            scope=raw.get("scope", "") or "",
            lean_declaration=lean_decl,
            content_hash=digest,
            formalization_status="target_proposed" if lean_decl else "absent",
        )
        db.add(claim)
        db.flush()
        db.add(
            Relation(
                layer="dependency",
                kind="addresses",
                source_type="idea",
                source_id=idea.id,
                target_type="claim",
                target_id=claim.id,
                status="checked",
            )
        )
        emit(
            db,
            "claim.created",
            record_type="claim",
            record_id=claim.id,
            payload={"campaign_id": campaign.id},
        )
        db.expire(idea, ["claims"])
        idea.evidence_status, idea.formalization_status = idea_status(idea)
        emit(db, "idea.evidence_updated", record_type="idea", record_id=idea.id)
        return claim, True

    # -- evidence ----------------------------------------------------------------------------

    def _resolve_target(
        self, db: Session, attempt: Attempt, target: str, id_map: dict[str, str]
    ) -> tuple[Idea | None, Claim | None]:
        if target == "self" and attempt.idea_id:
            return db.get(Idea, attempt.idea_id), None
        key = target.strip().lower()
        if key in id_map:
            idea = db.get(Idea, id_map[key])
            if idea is not None:
                return idea, None
            claim = db.get(Claim, id_map[key])
            if claim is not None:
                return claim.idea, claim
        idea = db.get(Idea, target)
        if idea is not None and idea.campaign_id in scope_ids(db, attempt):
            return idea, None
        claim = db.get(Claim, target)
        if claim is not None and claim.campaign_id in scope_ids(db, attempt):
            return claim.idea, claim
        return None, None

    def _upsert_evidence(
        self, db: Session, attempt: Attempt, campaign: Campaign, raw: dict, id_map: dict[str, str]
    ) -> tuple[int, int] | None:
        """Returns (evidence_created, lean_checks_created), or None when the evidence names a
        target this lab cannot attach it to; the caller keeps it. `problem`, and `self` on an
        attempt without an assigned idea, attach to the campaign's problem."""
        check_type = raw.get("check_type", "")
        result = raw.get("result", "")
        summary = (raw.get("summary") or "").strip()
        if check_type not in {k for k, _ in WORKER_STATUS_MAP} | {"critique", "literature_check"}:
            return 0, 0
        if result not in {"supports", "refutes", "inconclusive"} or not summary:
            return 0, 0
        target = str(raw.get("target") or "self")
        idea, claim = self._resolve_target(db, attempt, target, id_map)
        problem_id: str | None = None
        if idea is None and claim is None:
            if target.strip().lower() == "problem" or (target == "self" and not attempt.idea_id):
                problem_id = campaign.problem_id
            else:
                return None
        existing = (
            db.query(Evidence)
            .filter(
                Evidence.produced_by_attempt_id == attempt.id,
                Evidence.idea_id == (idea.id if idea else None),
                Evidence.claim_id == (claim.id if claim else None),
                Evidence.check_type == check_type,
                Evidence.summary == summary,
            )
            .first()
        )
        if existing:
            return 0, 0

        artifact = None
        artifact_raw = raw.get("artifact")
        if isinstance(artifact_raw, dict) and artifact_raw.get("content"):
            artifact = store_artifact(
                db,
                self.artifact_root,
                filename=str(artifact_raw.get("filename", "artifact.txt"))[:300],
                content=str(artifact_raw["content"]),
                media_type=str(artifact_raw.get("media_type", "text/plain")),
                producer_attempt_id=attempt.id,
                manifest={"requested_mode": attempt.requested_mode, "role": attempt.role},
            )
        evidence = Evidence(
            idea_id=idea.id if idea else None,
            claim_id=claim.id if claim else None,
            problem_id=problem_id,
            claim_version=claim.version if claim else 0,
            check_type=check_type,
            result=result,
            summary=summary,
            coverage=raw.get("coverage", "") or "",
            verifier=f"worker:{attempt.provider}:{attempt.requested_mode}",
            verifier_version=attempt.provider_session_id or "",
            certified=False,
            details={"assumptions": raw.get("assumptions", []) or []},
            artifact_id=artifact.id if artifact else None,
            produced_by_attempt_id=attempt.id,
        )
        db.add(evidence)
        db.flush()
        emit(
            db,
            "evidence.created",
            record_type="evidence",
            record_id=evidence.id,
            payload={"campaign_id": campaign.id, "certified": False},
        )

        if problem_id is not None and check_type == "literature_check" and result == "refutes":
            problem = campaign.problem
            if problem.status in {"reported_open", "unreviewed", "unknown"}:
                problem.status = "resolution_claimed"
                problem.status_checked_at = utcnow().date().isoformat()
                emit(
                    db,
                    "problem.status_flagged",
                    record_type="problem",
                    record_id=problem.id,
                    payload={"attempt_id": attempt.id, "evidence_id": evidence.id},
                    visibility="public",
                )

        if idea is not None:
            proposed = WORKER_STATUS_MAP.get((check_type, result))
            if proposed:
                idea.evidence_status = stronger(idea.evidence_status, proposed)
            if check_type == "critique" and idea.review_status == "unreviewed":
                idea.review_status = "ai_critiqued"
            if check_type == "lean_attempt" and idea.formalization_status in {
                "absent",
                "target_proposed",
            }:
                idea.formalization_status = "in_progress"

        lean_checks = 0
        if (
            check_type == "lean_attempt"
            and artifact is not None
            and artifact_raw is not None
            and claim is not None
        ):
            lean_checks = self._run_lean_check(
                db, attempt, claim, idea, str(artifact_raw["content"]), artifact.id
            )
        if claim is not None:
            db.expire(claim, ["evidence"])
            emit(db, "claim.evidence_updated", record_type="claim", record_id=claim.id)
        if idea is not None:
            db.expire(idea, ["claims", "evidence"])
            idea.evidence_status, idea.formalization_status = idea_status(idea)
            emit(db, "idea.evidence_updated", record_type="idea", record_id=idea.id)
        return 1, lean_checks

    def _run_lean_check(
        self,
        db: Session,
        attempt: Attempt,
        claim: Claim,
        idea: Idea | None,
        source: str,
        artifact_id: str,
    ) -> int:
        target_decl = declaration_name(claim.lean_declaration)
        if not target_decl:
            return 0
        approved = declaration_signature(claim.lean_declaration)
        outcome = self.lean_checker.check(source, target_decl, approved)
        verified = outcome.status == "verified"
        result = (
            "verified"
            if verified
            else ("rejected" if outcome.status == "rejected" else "inconclusive")
        )
        check = Evidence(
            idea_id=idea.id if idea else None,
            claim_id=claim.id,
            claim_version=claim.version,
            check_type="lean_check",
            result=result,
            summary="; ".join(outcome.reasons)
            if outcome.reasons
            else "Lean checked the approved target",
            verifier="lab-lean-checker",
            verifier_version=outcome.toolchain,
            certified=verified,
            details={
                **outcome.as_details(),
                "approved_target": claim.lean_declaration,
                "target_origin": (
                    "problem_formal_target"
                    if normalize(claim.lean_declaration)
                    == normalize(attempt.campaign.problem.formal_target or "")
                    else "worker_proposed"
                ),
            },
            artifact_id=artifact_id,
            produced_by_attempt_id=attempt.id,
        )
        db.add(check)
        db.flush()
        if verified:
            claim.formalization_status = "complete"
            if idea is not None:
                existing_proof = db.scalar(
                    select(Relation).where(
                        Relation.kind == "proves",
                        Relation.source_id == idea.id,
                        Relation.target_id == claim.id,
                    )
                )
                if existing_proof is None:
                    db.add(
                        Relation(
                            layer="dependency",
                            kind="proves",
                            source_type="idea",
                            source_id=idea.id,
                            target_type="claim",
                            target_id=claim.id,
                            status="checked",
                            provenance={"evidence_id": check.id},
                        )
                    )
            # A proof of a sublemma never completes a problem. Match the immutable target
            # declaration for each beneficiary; research outcome is separate from source status.
            for beneficiary in db.scalars(
                select(Campaign).where(Campaign.id.in_(scope_ids(db, attempt)))
            ):
                if beneficiary.problem.formal_target and normalize(
                    claim.lean_declaration
                ) == normalize(beneficiary.problem.formal_target):
                    beneficiary.research_outcome = "proven"
                    emit(
                        db,
                        "research.target_proven",
                        record_type="campaign",
                        record_id=beneficiary.id,
                        payload={"claim_id": claim.id, "evidence_id": check.id},
                        visibility="public",
                    )
        elif outcome.status == "rejected":
            claim.formalization_status = "blocked"
            if idea is not None and idea.formalization_status != "complete":
                idea.formalization_status = "blocked"
        emit(
            db,
            "evidence.created",
            record_type="evidence",
            record_id=check.id,
            payload={
                "campaign_id": attempt.campaign_id,
                "certified": verified,
                "lean_status": outcome.status,
            },
        )
        return 1


def declaration_name(lean_declaration: str) -> str:
    match = re.match(r"\s*(?:theorem|lemma)\s+([A-Za-z_][\w.']*)", lean_declaration)
    return match.group(1) if match else ""


def declaration_signature(lean_declaration: str) -> str:
    """Return everything between the declaration name and `:=` (binders and statement)."""
    match = re.match(
        r"\s*(?:theorem|lemma)\s+[A-Za-z_][\w.']*\s*(.*?)\s*(?::=.*)?$", lean_declaration, re.S
    )
    return match.group(1) if match else ""
