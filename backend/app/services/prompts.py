"""Assignment prompts for Devin research sessions. Every prompt is bounded, states the exact
problem and assumptions, and says what may and may not be claimed."""

from __future__ import annotations

from ..models import Attempt, Campaign, Idea, Problem

ROLE_INSTRUCTIONS: dict[str, str] = {
    "hypothesis_generator": (
        "Produce 3 to 5 genuinely distinct approaches to the problem. For each: the approach, the "
        "mechanism that would make it work, the single lemma or computation whose failure would "
        "falsify it, a concrete next experiment, and method tags. If parent ideas are listed, "
        "refine or recombine them rather than repeating them. Do not claim any result is proved."
    ),
    "experimenter": (
        "Implement and run the bounded experiment described for the assigned idea. Report exactly "
        "what range was checked, every assumption, and the first failure if any. Return the code "
        "and raw results as an artifact. A passing experiment is evidence, not a proof."
    ),
    "critic": (
        "Find gaps, hidden assumptions, circularity, and invalid composition steps in each idea "
        "under review and its claims. Check whether the key lemma is already known and cite the "
        "source if so. Return one `critique` evidence item per idea, `target` = that idea's id; "
        "report `supports` only if you found no gap after a genuine attempt."
    ),
    "prover_formalizer": (
        "Produce a Lean 4 proof the lab's checker accepts. The lab project pins the toolchain in "
        "lean-toolchain and provides Mathlib (`import Mathlib`) plus `import MathLab.Basic`. "
        "If the assigned idea has a claim with an approved Lean target, prove exactly that "
        "declaration (same name, binders and statement). Otherwise pick the strongest "
        "sub-statement of the idea you can genuinely prove (a lemma, a finite case, an "
        "equivalence, a reduction) and return it as a new idea refining the assigned one with a "
        "claim carrying `lean_declaration` = `theorem <name> <binders> : <statement>`; faithfully "
        "formalizing a piece of the problem matters more than reaching the conjecture. Do not use "
        "sorry, axiom, unsafe, native_decide, implemented_by, or set_option; proofs may depend "
        "only on propext, Classical.choice and Quot.sound. Iterate against the lab's checker "
        "(POST .../lean-check below) until it returns status `verified`, then attach the whole "
        "file as a `lean_attempt` evidence artifact whose `target` is that claim's local_id. If "
        "you cannot finish, return the partial file plus a precise list of remaining "
        "obligations. The lab's checker decides verification; do not report it yourself."
    ),
    "status_researcher": (
        "Check whether the problem's reported open status still holds against later literature. "
        "Return sources with exact locations and retrieval dates. "
        "Do not change any status yourself."
    ),
}

# Canonical role names accepted for both automatic and manually-created assignments.
# Keep this derived from the prompt map so every accepted role is guaranteed to have
# substantive instructions.
ASSIGNMENT_ROLES = tuple(ROLE_INSTRUCTIONS)


def describe_idea(idea: Idea) -> str:
    lines = [
        f"- id: {idea.id}",
        f"  title: {idea.title}",
        f"  approach: {idea.approach}",
        f"  evidence_status: {idea.evidence_status}; review: {idea.review_status}; "
        f"formalization: {idea.formalization_status}",
        f"  method_tags: {', '.join(idea.method_tags) or 'none'}",
    ]
    if idea.next_experiment:
        lines.append(f"  next_experiment: {idea.next_experiment}")
    for claim in idea.claims:
        lines.append(f"  claim {claim.id} (v{claim.version}): {claim.statement}")
        if claim.lean_declaration:
            lines.append(f"    approved Lean target: {claim.lean_declaration}")
    return "\n".join(lines)


def build_prompt(
    *,
    attempt: Attempt,
    campaign: Campaign,
    problem: Problem,
    active_ideas: list[Idea],
    parents: list[Idea],
    worker_api_base: str,
    idea: Idea | None = None,
    review: list[Idea] | None = None,
) -> str:
    sources = (
        "\n".join(
            f"- {a.source.title} — {a.source.url} ({a.location or 'n/a'}; "
            f"asserted {a.asserted_status} on {a.asserted_at or 'unknown date'})"
            for a in problem.assertions
        )
        or "- none recorded"
    )
    areas = ", ".join(sorted(area.name for area in problem.areas)) or "unclassified"
    sections = [
        "You are a research worker in a mathematics lab. Work only on the bounded assignment "
        "below and stop when the deliverable is complete.",
        f"ROLE: {attempt.role}",
        ROLE_INSTRUCTIONS.get(attempt.role, ""),
        "PROBLEM",
        f"title: {problem.title}",
        f"areas: {areas}",
        f"statement: {problem.statement}",
        f"definitions: {problem.definitions or 'as standard'}",
        f"assumptions: {problem.assumptions or 'none beyond the statement'}",
        f"reported status: {problem.status} (checked {problem.status_checked_at or 'never'})",
        "sources:\n" + sources,
    ]
    if problem.formal_target:
        sections.append(f"approved formal target:\n{problem.formal_target}")
    reference = problem.coverage.get("reference_formalization")
    if reference:
        sections.append(
            "reference formalization (external library "
            f"{reference.get('library', '')}, {reference.get('url', '')}; not compilable in the "
            "lab project verbatim, use it to fix the intended meaning):\n"
            f"{reference.get('statement', '')}"
        )
    if parents:
        sections.append(
            "PARENT IDEAS TO REFINE OR RECOMBINE\n" + "\n".join(map(describe_idea, parents))
        )
    if active_ideas:
        sections.append(
            "CURRENT ACTIVE IDEAS (avoid duplicates)\n"
            + "\n".join(map(describe_idea, active_ideas))
        )
    if idea is not None:
        sections.append(
            f"ASSIGNED IDEA (id {idea.id}; use target 'self' for its evidence)\n"
            + describe_idea(idea)
        )
    if review:
        sections.append(
            "IDEAS UNDER REVIEW (one critique each, target = the idea id)\n"
            + "\n".join(map(describe_idea, review))
        )
    sections += [
        "RULES",
        "- State every assumption. Never describe anything as verified, proved, or solved; the "
        "lab's independent checker assigns those labels.",
        "- Distinguish known results (cite them) from your own reasoning.",
        "- Return reproducible artifacts (code, data, Lean files) inline in the structured output.",
        "- Fill `gaps` with what remains unresolved. Empty gaps on an open problem is a red flag.",
        "- Leave `self_reported_models` empty unless your environment explicitly states the model.",
        "- Give every new idea and claim a `local_id` (I1, I2, C1, ...) and point each evidence "
        "item's `target` at one of those, at an existing idea/claim id, or at 'self'/'problem'. "
        "Evidence that names nothing attachable is kept but cannot be published against an idea.",
        "DELIVERABLE",
        "Call provide_structured_output with the required schema (ideas, evidence, gaps).",
        f"Lab worker API (header X-Worker-Token = the LAB_WORKER_TOKEN session secret): "
        f"GET {worker_api_base}/worker/attempts/{attempt.id}/context for the assignment; "
        f"POST {worker_api_base}/worker/attempts/{attempt.id}/lean-check with JSON "
        '{"source": <full .lean file>, "declaration": <theorem line>} to dry-run the checker; '
        f"POST {worker_api_base}/worker/attempts/{attempt.id}/submit with the deliverable JSON "
        "shape for optional partial progress (merged idempotently).",
        f"campaign: {campaign.id}; generation: {campaign.generation}; attempt: {attempt.id}",
    ]
    return "\n\n".join(section for section in sections if section)
