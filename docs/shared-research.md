# Shared research across a problem set

Each portfolio is a shared research pool. Its campaigns retain their problem targets and
session allowances, while workers explore a common graph of claims, proposed applications,
dependencies, evidence, and failed directions. A shared assignment is charged to one eligible
campaign; discoveries remain available to every campaign in the pool after that allowance ends.

## Start a pool

In **Research controls**, create a portfolio, then use **Explore a problem set** to select
several problems and add them together. Set a session allowance for each new problem and run
the scheduler (or enable the existing background scheduler). Existing campaigns in the same
portfolio also share research by default. Adding a problem again does not reset its budget or
resume a paused/completed campaign. Different portfolios remain separate research pools.

The equivalent API is `POST /private/portfolios/{id}/research`:

```json
{
  "problem_ids": ["problem-a-id", "problem-b-id"],
  "session_budget_per_problem": 12,
  "default_mode": "ultra"
}
```

`GET /private/portfolios/{id}/research` returns shared claims, evidence, applications,
dependencies and failed directions. The full public graph includes cross-problem links;
the private pool panel shows adoption/rejection decisions and their reasons.

## The loop

1. **Explore the set.** A synthesizer considers the pool together, proposes reusable lemmas,
   combines approaches and proposes applications to more than one problem. Individual branch
   explorers also receive shared memory and previous failures.
2. **Find candidate connections.** Cheap retrieval uses shared subject areas and statement
   terms to nominate up to three other problems per claim. These are untested suggestions.
   Synthesis can propose connections beyond this shortlist, including across subject areas.
3. **Test applicability.** A connection reviewer checks the claim's scope, quantifiers and
   assumptions against a target problem, recording an adopted, rejected or inconclusive route.
   Every decision retains its reason, assumptions, originating attempt and prior history.
4. **Work on common bottlenecks.** Claims adopted by multiple problems, and prerequisites of
   other claims, become shared lemma tasks. Unresolved prerequisites receive attention before
   their dependents; cycles or refuted prerequisites trigger attempts to repair the formulation.
   Contradictory claims/evidence get counterexample tasks with higher priority.
5. **Verify and reuse.** Workers can retrieve shared proof and experiment source through their
   scoped API. The independent Lean checker certifies submitted proofs. A useful new application
   can revive an archived source idea; failed connections remain available to later workers.
6. **Try closure.** When a formal target has adopted, verified shared lemmas, a proof closer
   attempts a self-contained proof of that exact target. Failure retains its gaps and returns
   capacity to other frontier work. A completed attempt on an unchanged closure frontier is
   not retried indefinitely; new verified inputs create another opportunity.
7. **Continue exploration.** Synthesis runs initially and after six additional claim/verified/
   refuted discoveries. Every third dispatch prefers available exploration work. Other dispatches
   favor contradictions, closure and shared lemmas, with queue aging to avoid starvation.

Within a campaign, independent branch experiments can run concurrently (default limit two),
subject to the portfolio concurrency cap. Local assignments and shared lemma tasks avoid
simultaneously working on the same focused claim/idea. Task identities persist in attempt
metadata, so completed unchanged tasks are not recreated on every scheduler tick.

## What the graph means

Workers submit optional `research_links` alongside ideas and evidence:

```json
{
  "claim": "existing-claim-id-or-new-local-id",
  "target": "problem-id",
  "kind": "applies_to",
  "status": "adopted",
  "reason": "This lemma removes the finite-domain bottleneck after this substitution.",
  "assumptions": ["The target must satisfy the lemma's finiteness hypothesis."]
}
```

`applies_to` connects a claim to a problem. `depends_on`, `contradicts` and `equivalent_to`
connect two claims. These mathematical relationships are proposed structure. In particular,
**adopted means a useful research route; it does not mean the claim or target is proven**.
The checker never accepts a dependency just because an agent proposed or adopted it.

Claim evidence is version-scoped. Worker counterevidence remains explicitly disputed until
independently checked. One verified sublemma does not certify other claims in the same idea.
Research outcomes are separate from the literature's reported problem status: `proven` requires
a checker-certified claim matching the problem's exact formal declaration; budget exhaustion
produces `unresolved_with_progress` when relevant verified claims exist, otherwise `unresolved`.
Automatic formal disproof classification is not implemented.

## Memory and budgets

Worker context is bounded to 40 claims, prioritizing the focused claim, relevant applications,
dependencies and verified results, plus up to 20 failed/disputed directions. Omitted claims
are counted. Workers can request an individual claim and its dependencies using
`GET /worker/attempts/{attempt_id}/claims/{claim_id}`, and source files using
`GET /worker/attempts/{attempt_id}/artifacts/{artifact_id}`. Both enforce pool scope.

Session allowances and portfolio concurrency remain hard limits. This policy reallocates
available slots; it does not increase or silently move the user's campaign allowances. Repeated
inconclusive challenges are bounded. Campaign policy `shared_research: false` excludes that
campaign from the shared pool. A comparison-tagged assignment gets only its own campaign context;
use a separate portfolio or an excluded campaign for strict benchmark isolation.

Run one scheduler process per database. Its lock serializes manual and background ticks within
that process; this is not a distributed scheduler.

## Validation and scope

`backend/tests/test_shared_research.py` exercises a two-problem mock loop through synthesis,
application review and shared lemma proving, as well as negative decisions, dependency cycles,
counterevidence, exact-target outcome propagation, artifact access, pauses and budgets.
The mock's mathematical statements and adoption decisions are fixtures, not real discoveries.
Positive certification propagation tests substitute a checker verdict; real Lean integration
remains separately tested when the toolchain is installed.

The policy is heuristic. It adds an executable shared research loop, not learned scheduling,
semantic theorem equivalence, interactive Lean proof-state search or evidence that this
architecture outperforms equal-budget baselines. Those remain future evaluation and runtime work.
