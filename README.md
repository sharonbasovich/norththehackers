# MathLab — a Devin-driven laboratory for open mathematical problems

MathLab maintains a source-backed atlas of reported open problems across mathematics, runs
bounded Devin research assignments against them, keeps the resulting hypotheses / experiments /
proof attempts as a versioned lineage graph, culls and refines branches generation by generation,
independently checks Lean proofs, and publishes everything automatically with explicit evidence
labels. The full design is in [`docs/plan.md`](docs/plan.md).

```
frontend/   React + react-force-graph-3d explorer (public browsing + private research controls)
backend/    FastAPI system of record: atlas, campaigns, scheduler, Devin adapter, Lean checker,
            publication projection, public/private/worker APIs
lean/       Lean 4 project the independent checker compiles submissions against
docs/       plan.md — the approved research & implementation plan
```

## Principles the code enforces

- **The app is the system of record; Devin does bounded work.** A graph node is not a Devin
  session. Sessions are `Attempt`s with a frozen prompt, a requested mode and the mode the
  provider actually reported; the app owns scheduling, ingestion, selection and publication.
- **Nothing a worker says is trusted.** Worker-reported evidence is published as
  *"Worker-reported … — not independently certified"*. Only the independent Lean checker (or a
  human reviewer via the private API) can certify evidence; only the checker can grant
  **Lean verified**, and only for the exact approved theorem target with no `sorry`/`sorryAx`
  and no axioms outside the allow-list. A changed statement is a new claim version and never
  inherits verification.
- **Informal proofs are welcome and labelled.** *"Informal proof candidate — not formally
  verified"* is a first-class public label; Lean remains the end goal.
- **Public reads never spend money.** `/public/*` serves only the `Publication` projection
  (allow-listed payloads) and can't create sessions. `/private/*` needs an owner/collaborator
  `X-API-Key`; `/worker/*` needs an attempt-scoped `X-Worker-Token`.
- **"Open" is a dated claim by a source, not a fact.** Every problem carries
  `SourceAssertion`s (URL, location, asserted status, retrieval date, review state). Seed
  records are marked `unreviewed` until a person confirms them.
- **Four relation layers stay separate:** `atlas` (classification), `lineage` (research
  parent/child), `dependency` (idea→claim, proves), `association` (thematic). 3D geometry is
  presentational, never mathematical distance.

## Quick start (mock provider — consumes no Devin usage)

Backend (Python 3.10+):

```bash
cd backend
pip install -e ".[dev]"
MATHLAB_OWNER_API_KEY=dev-owner-key uvicorn app.main:app --reload --port 8000
```

Frontend (Node 22+):

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /public and /private to :8000
```

Then in the UI click **Research controls**, enter `dev-owner-key`, **Load seed atlas**, create
a portfolio and a campaign, and press **Run scheduler tick** a few times. The mock provider
returns deterministic role-dependent structured output, so you can watch generations branch,
get culled and promoted without spending anything. `curl` equivalent:

```bash
K='X-API-Key: dev-owner-key'
curl -X POST -H "$K" localhost:8000/private/seed
PF=$(curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
     -d '{"name":"pilot","max_concurrent_sessions":2}' localhost:8000/private/portfolios | jq -r .id)
PR=$(curl -s localhost:8000/public/problems/goldbach-conjecture | jq -r .problem.record_id)
curl -X POST -H "$K" -H 'Content-Type: application/json' \
     -d "{\"portfolio_id\":\"$PF\",\"problem_id\":\"$PR\",\"session_budget\":10,\"default_mode\":\"fusion\"}" \
     localhost:8000/private/campaigns
for i in $(seq 30); do curl -s -X POST -H "$K" localhost:8000/private/scheduler/tick >/dev/null; done
curl -s localhost:8000/public/graph | jq '.links | group_by(.layer) | map({(.[0].layer): length})'
```

Or with Docker: `docker compose up --build` (backend on :8000, frontend on :5173).

### Growing the atlas

The seed is 27 hand-checked problems. `POST /private/atlas/import/wikipedia` bulk-imports the
`== Unsolved problems ==` sections of Wikipedia's *List of unsolved problems in mathematics*
(~500 entries; pass `{"dry_run": true}` to preview, `limit` to cap). Each entry becomes a
`reported_open` problem with `origin="bulk_import"` and two *unreviewed* source assertions:
the list page (with the section path it was listed under) and the linked article. Being
listed is a dated claim, not a verified status; the `status_researcher` role and
`POST /private/problems/{id}/review` exist to check it. Entries whose article URL is already
asserted for an existing problem are skipped as duplicates, so re-running is idempotent and
the hand-checked seed records win. `POST /private/atlas/import` accepts any document in the
seed JSON format for other sources (Open Problem Garden, …).

`POST /private/atlas/import/erdos` imports the Erdős problems from the status table in
[teorth/erdosproblems](https://github.com/teorth/erdosproblems) (`data/problems.yaml`,
Apache-2.0): by default the ~640 open-like entries (`open`, `falsifiable`, `verifiable`,
`decidable`, …; `include_resolved` adds the rest as `resolved`). Every source state string is
preserved verbatim as an assertion, dated with the table's `last_update`. The statement is the
problem's own erdosproblems.com page text; where google-deepmind/formal-conjectures has a file,
its main declaration (research-open part preferred over textbook/solved variants) is stored as
`reference_formalization` — an *external* reference, never MathLab's approved `formal_target`.
Re-running refreshes the text/reference of bulk-imported records without touching status,
assertions or reviews.

Status review is two-tier. A `status_researcher` session that returns a problem-level
`literature_check` with `result: "refutes"` only *flags* the problem as `resolution_claimed`;
the scheduler then pauses auto-planned campaigns on it so no more sessions are spent. Moving a
problem to `resolved`, `disputed`, or back to `reported_open` is a collaborator decision via
`POST /private/problems/{id}/review` (`status`, `note`, optional `assertion_ids`), which marks
the checked source assertions `reviewed` and appends a dated, attributed entry to the
publicly visible `status_reviews` history.

## Turning on real Devin sessions

Set these (environment or `backend/.env`, see `backend/.env.example`; never commit the key):

| Variable | Meaning |
|---|---|
| `MATHLAB_DEVIN_PROVIDER=api` | switch from `mock` to the Devin v3 cloud API |
| `MATHLAB_DEVIN_API_KEY` | organization service-user API key |
| `MATHLAB_DEVIN_ORG_ID` | organization id used in `POST /v3/organizations/{org}/sessions` |
| `MATHLAB_DEVIN_CREATE_AS_USER_ID` | optional; attributes sessions to that user's plan |
| `MATHLAB_DEVIN_MAX_ACU_LIMIT` | default per-session ACU cap; campaign policy `max_acu_limit` / `role_acu_limits` override it (formalizers default to 15) |
| `MATHLAB_PUBLIC_BASE_URL` | URL Devin sessions can reach to call `/worker/*` back. `localhost` only works for the mock provider; for cloud sessions expose the backend (e.g. `cloudflared tunnel --url http://localhost:8000` gives a temporary URL — fine for pilots, not production hosting). Without reachability sessions still work via structured output only. |
| `MATHLAB_SCHEDULER_ENABLED=true` | run the scheduler loop in-process every `MATHLAB_SCHEDULER_INTERVAL_SECONDS` |
| `MATHLAB_OWNER_API_KEY` | **change from the default before exposing the API** |

Modes (`normal`, `fast`, `lite`, `ultra`, `fusion`) are passed through verbatim as `devin_mode`
and stored as `requested_mode`; whatever the API reports back is stored separately as
`reported_mode`. Nothing is silently downgraded. Per-assignment mode choice and a
`comparison_group` tag support the Fusion-vs-Ultra pilot from the plan (§6.4); the first
matched pair is recorded in `docs/pilot-fusion-vs-ultra.md` and motivates the default
`role_modes` policy (Ultra for generation/critique/formalization, Fusion for tool-heavy
experimenter and status-research roles).

Worker output is accepted with worker-local ids (`I1`, `C1`, …) and resolved to lab records;
evidence that resolves to nothing is kept in `attempt.result["unattached_evidence"]` along
with the raw output, and `target: "problem"` attaches literature/status evidence to the
problem itself. `POST /private/attempts/{id}/reingest` replays a stored result idempotently
after a resolver fix.

With a live provider every scheduler tick may create sessions billed to the attributed account;
the UI shows a warning when the provider is not `mock`.

## Lean checker

`lean/` is a Lake project pinned to `leanprover/lean4:v4.24.0` with Mathlib `v4.24.0` as a
dependency. Install [elan](https://github.com/leanprover/elan), then
`cd lean && lake exe cache get && lake build` (the Mathlib cache is ~6 GB). The checker:

1. statically rejects `sorry`, `sorryAx`, `axiom`, `unsafe`, `implemented_by`, `extern`,
   `native_decide`, and any declaration whose name/signature differs from the approved target;
2. compiles the submission inside the project and runs `#print axioms` on the target;
3. records toolchain, project hash, elapsed time and full log as `Evidence.details`.

Docker Compose runs Lean in a separate, non-root checker container. The service has no host
port or public-internet route, uses a read-only filesystem apart from a temporary proof
directory, drops Linux capabilities, and enforces CPU, memory and process limits. The backend
authenticates to it with `MATHLAB_LEAN_CHECKER_TOKEN`; replace the development default before
deployment. For non-Docker development, the backend can still run a local checker from
`MATHLAB_LEAN_PROJECT_DIR`.

If the isolated service or local toolchain is unavailable, the checker records a
`checker_unavailable` blocker; it never pretends to verify. Set both
`MATHLAB_LEAN_CHECKER_URL=""` and `MATHLAB_LEAN_PROJECT_DIR=""` to disable checking explicitly.

### Formalization stage

After each cull the scheduler gives every promoted (or best surviving unrefuted) idea one
`prover_formalizer` pass before the next generation branches from it. The formalizer is told
to prove the problem's approved formal target exactly if one exists, otherwise the strongest
*smaller* faithful statement (a lemma, finite case, equivalence or reduction), and it can
iterate against the lab's own checker with `POST /worker/attempts/{id}/lean-check` (a dry run;
nothing is recorded). Only the final `lean_attempt` artifact is checked for real, and the
public evidence carries `check_status`, `approved_target`, `target_origin`
(`problem_formal_target` vs `worker_proposed`) and `toolchain` so readers can tell a verified
sub-lemma from a verified solution of the stated problem. A worker never certifies its own
work.

## Development

```bash
cd backend && ruff check app tests && ruff format --check app tests && mypy app && pytest
cd frontend && npm run typecheck && npm test && npm run build
```

The backend suite includes a real Lean round-trip (skipped when the toolchain is absent).
Frontend tests (vitest) cover the palette (evidence-status colouring, never worker self-reports)
and the API clients (public calls carry no credentials; the private key travels only as
`X-API-Key`).
SQLite is the default database; set `MATHLAB_DATABASE_URL` to a PostgreSQL URL for deployment.

## API surface

| Prefix | Auth | Purpose |
|---|---|---|
| `GET /public/areas, /problems, /problems/{slug}, /ideas/{id}, /graph, /events, /labels` | none | published projection only |
| `POST /private/seed, /problems, /portfolios, /campaigns, /campaigns/{id}/assignments, /scheduler/tick …` | `X-API-Key` | research controls (owner or collaborator) |
| `POST /private/collaborators, /publications/{id}/withdraw` | owner key | account and retraction controls |
| `GET /worker/attempts/{id}/context`, `POST /worker/attempts/{id}/lean-check`, `POST /worker/attempts/{id}/submit` | `X-Worker-Token` | what a Devin session calls back into (context incl. Lean environment, dry-run checker, final submission) |

## Status

Phase 0–1 of the plan: atlas + research loop + Lean checker (Mathlib) + formalization stage +
automatic publication + 3D explorer, validated against the deterministic mock provider, one
live Devin API smoke session and the matched Fusion vs Ultra pilot on the lonely runner
conjecture (all ingested and auto-published with empirical/untested labels; no open conjecture
has been solved and nothing is labelled verified without the checker). The atlas grows via
the Wikipedia bulk import above (~480 problems, 30+ subareas). Not yet done: more import
sources, source review workflow UI, an MCP tool server (the
HTTP worker routes cover callbacks today), durable public hosting, PostgreSQL deployment
manifests, and the adapters for external evolution engines listed in the plan.
