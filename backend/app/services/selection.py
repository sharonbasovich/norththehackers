"""Diversity-preserving selection over a campaign generation (docs/plan.md §6.3).

Pure functions over plain data so they can be unit-tested and replayed from recorded state.
Scores come from independently recorded evidence, not from a model's self-assessment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EVIDENCE_WEIGHTS: dict[str, float] = {
    "untested": 0.0,
    "empirically_supported": 1.0,
    "counterexample_checked": 1.0,
    "proof_sketch": 1.5,
    "informal_proof_candidate": 2.5,
    "lean_formalization_in_progress": 3.0,
    "lean_verified": 6.0,
    "refuted": -5.0,
    "unresolved_conflict": -1.0,
}

REVIEW_WEIGHTS: dict[str, float] = {
    "unreviewed": 0.0,
    "ai_critiqued": 0.25,
    "expert_reviewed": 1.5,
    "disputed": -1.0,
}


@dataclass
class Candidate:
    idea_id: str
    method_tags: list[str]
    evidence_status: str
    review_status: str
    formalization_status: str
    depth: int
    pinned: bool = False
    evidence_count: int = 0
    critique_supports: int = 0
    critique_refutes: int = 0
    cost: float = 0.0
    duplicate_of: str | None = None
    transfer_value: int = 0


@dataclass
class Decision:
    idea_id: str
    decision: str  # promoted | kept | archived
    cluster: str
    score: float
    components: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def cluster_key(tags: list[str]) -> str:
    return sorted(tags)[0] if tags else "untagged"


def score(candidate: Candidate) -> tuple[float, dict[str, float]]:
    components = {
        "evidence": EVIDENCE_WEIGHTS.get(candidate.evidence_status, 0.0),
        "review": REVIEW_WEIGHTS.get(candidate.review_status, 0.0),
        "critique": 0.5 * candidate.critique_supports - 1.0 * candidate.critique_refutes,
        "formalization": 1.0
        if candidate.formalization_status in {"in_progress", "complete"}
        else 0.0,
        "cost_penalty": -0.1 * candidate.cost,
        "duplicate_penalty": -3.0 if candidate.duplicate_of else 0.0,
        "transfer_value": min(3.0, candidate.transfer_value * 0.75),
    }
    return sum(components.values()), components


def select_generation(
    candidates: list[Candidate],
    *,
    keep_total: int = 6,
    keep_per_cluster: int = 2,
    promote_top: int = 2,
) -> list[Decision]:
    """Keep the best `keep_per_cluster` from every method cluster (diversity), then fill up to
    `keep_total` by global score. Pinned ideas are always kept. Refuted ideas are archived
    regardless of score, but remain revivable through the private API."""
    scored: list[tuple[Candidate, float, dict[str, float]]] = []
    for candidate in candidates:
        total, components = score(candidate)
        scored.append((candidate, total, components))
    scored.sort(key=lambda item: item[1], reverse=True)

    kept: set[str] = set()
    per_cluster: dict[str, int] = {}
    for candidate, _total, _ in scored:
        key = cluster_key(candidate.method_tags)
        if candidate.pinned:
            kept.add(candidate.idea_id)
            per_cluster[key] = per_cluster.get(key, 0) + 1
            continue
        if candidate.evidence_status == "refuted" or candidate.duplicate_of:
            continue
        if len(kept) < keep_total and per_cluster.get(key, 0) < keep_per_cluster:
            kept.add(candidate.idea_id)
            per_cluster[key] = per_cluster.get(key, 0) + 1
    for candidate, _total, _ in scored:
        if len(kept) >= keep_total:
            break
        if candidate.evidence_status == "refuted" or candidate.duplicate_of:
            continue
        kept.add(candidate.idea_id)

    decisions: list[Decision] = []
    promoted = 0
    for candidate, total, components in scored:
        key = cluster_key(candidate.method_tags)
        if candidate.idea_id in kept:
            if promoted < promote_top and total > 0:
                decision, reason = "promoted", "top score with positive evidence"
                promoted += 1
            else:
                decision, reason = "kept", "diversity or pin"
        else:
            decision = "archived"
            if candidate.evidence_status == "refuted":
                reason = "refuted by checked evidence"
            elif candidate.duplicate_of:
                reason = f"duplicate of {candidate.duplicate_of}"
            else:
                reason = "below selection threshold"
        decisions.append(Decision(candidate.idea_id, decision, key, total, components, reason))
    return decisions
