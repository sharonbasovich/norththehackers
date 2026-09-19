import { useCallback, useEffect, useState } from "react";
import { PrivateApi, type Attempt, type Campaign, type Portfolio, type Problem, type ResearchPool, type SchedulerStatus } from "./api";

const MODES = ["ultra", "fusion", "normal", "fast", "lite"];
const PROBLEM_STATUSES = ["reported_open", "resolution_claimed", "resolved", "disputed", "unknown"];
const ROLES = ["hypothesis_generator", "experimenter", "critic", "prover_formalizer", "status_researcher", "synthesizer"];
const KEY_STORAGE = "mathlab.apiKey";

interface Props {
  problems: Problem[];
  selectedIdeaId: string | null;
  onChanged: () => void;
}

export default function PrivatePanel({ problems, selectedIdeaId, onChanged }: Props) {
  const [key, setKey] = useState(() => sessionStorage.getItem(KEY_STORAGE) ?? "");
  const [api, setApi] = useState<PrivateApi | null>(null);
  const [status, setStatus] = useState<SchedulerStatus | null>(null);
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [campaignId, setCampaignId] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [newPortfolio, setNewPortfolio] = useState({ name: "pilot", max: 2 });
  const [newCampaign, setNewCampaign] = useState({ portfolio_id: "", problem_id: "", budget: 6, mode: "ultra" });
  const [assignment, setAssignment] = useState({ role: "hypothesis_generator", mode: "fusion", group: "" });
  const [poolId, setPoolId] = useState("");
  const [poolProblems, setPoolProblems] = useState<string[]>([]);
  const [poolBudget, setPoolBudget] = useState(12);
  const [pool, setPool] = useState<ResearchPool | null>(null);
  const [prompt, setPrompt] = useState<string | null>(null);
  const [review, setReview] = useState({ problem_id: "", status: "reported_open", note: "" });

  const load = useCallback(
    async (a: PrivateApi) => {
      const [s, p, c] = await Promise.all([a.status(), a.portfolios(), a.campaigns()]);
      setStatus(s);
      setPortfolios(p);
      setCampaigns(c);
      if (campaignId) setAttempts(await a.attempts(campaignId));
      if (poolId) setPool(await a.researchPool(poolId));
    },
    [campaignId, poolId],
  );

  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>) => {
      if (!api) return;
      setBusy(true);
      try {
        const r = await fn();
        setMsg(`${label}: ${typeof r === "object" ? JSON.stringify(r) : String(r)}`);
        await load(api);
        onChanged();
      } catch (e) {
        setMsg(`${label} failed — ${(e as Error).message}`);
      } finally {
        setBusy(false);
      }
    },
    [api, load, onChanged],
  );

  const connect = async () => {
    const a = new PrivateApi(key);
    try {
      await load(a);
      sessionStorage.setItem(KEY_STORAGE, key);
      setApi(a);
      setMsg(null);
    } catch (e) {
      setMsg(`Authentication failed — ${(e as Error).message}`);
    }
  };

  useEffect(() => {
    if (api && campaignId) api.attempts(campaignId).then(setAttempts).catch(() => setAttempts([]));
  }, [api, campaignId]);

  useEffect(() => {
    setPool(null);
    if (!api || !poolId) return;
    let cancelled = false;
    api.researchPool(poolId).then((value) => { if (!cancelled) setPool(value); })
      .catch((e) => { if (!cancelled) setMsg((e as Error).message); });
    return () => { cancelled = true; };
  }, [api, poolId]);

  if (!api) {
    return (
      <div className="detail">
        <h2>Research controls</h2>
        <p>
          Private. Requires an owner or collaborator API key; it is sent only as <code>X-API-Key</code> to this backend and kept in session
          storage. Public browsing never needs it and never launches paid work.
        </p>
        <input type="password" placeholder="API key" value={key} onChange={(e) => setKey(e.target.value)} aria-label="API key" />
        <button className="primary" onClick={connect} disabled={!key}>
          Connect
        </button>
        {msg && <p className="error">{msg}</p>}
      </div>
    );
  }

  return (
    <div className="detail private">
      <h2>Research controls</h2>
      {status && (
        <p>
          <small>
            provider <b>{status.provider}</b> · background loop {status.background_enabled ? `every ${status.interval_seconds}s` : "off"} ·{" "}
            {status.running_attempts} running · Lean {status.lean_available ? "available" : "missing"}
          </small>
        </p>
      )}
      {status && status.provider !== "mock" && (
        <p className="warn">Live provider: every tick may create Devin sessions billed to the configured account.</p>
      )}
      <div className="toolbar">
        <button disabled={busy} onClick={() => run("tick", () => api.tick())}>
          Run scheduler tick
        </button>
        <button disabled={busy} onClick={() => run("seed", () => api.seed())}>
          Load seed atlas
        </button>
        <button disabled={busy} onClick={() => run("wikipedia import (dry run)", () => api.importWikipedia(true))}>
          Preview Wikipedia import
        </button>
        <button disabled={busy} onClick={() => run("wikipedia import", () => api.importWikipedia(false))}>
          Import Wikipedia list
        </button>
        <button disabled={busy} onClick={() => run("erdős import (dry run)", () => api.importErdos(true))}>
          Preview Erdős import
        </button>
        <button disabled={busy} onClick={() => run("erdős import", () => api.importErdos(false))}>
          Import Erdős problems
        </button>
        <button
          onClick={() => {
            sessionStorage.removeItem(KEY_STORAGE);
            setApi(null);
          }}
        >
          Disconnect
        </button>
      </div>
      {msg && <p className="msg">{msg}</p>}

      <h4>Portfolios</h4>
      {portfolios.map((p) => (
        <div key={p.id} className="row static">
          <span className="grow">
            {p.name} <small>max {p.max_concurrent_sessions} concurrent</small>
          </span>
          <button disabled={busy} onClick={() => run(p.paused ? "resume" : "pause", () => api.pausePortfolio(p.id, !p.paused))}>
            {p.paused ? "Resume" : "Pause"}
          </button>
        </div>
      ))}
      <div className="form">
        <input value={newPortfolio.name} onChange={(e) => setNewPortfolio({ ...newPortfolio, name: e.target.value })} placeholder="name" />
        <input
          type="number"
          min={1}
          value={newPortfolio.max}
          onChange={(e) => setNewPortfolio({ ...newPortfolio, max: Number(e.target.value) })}
          aria-label="max concurrent sessions"
        />
        <button disabled={busy} onClick={() => run("create portfolio", () => api.createPortfolio(newPortfolio.name, newPortfolio.max))}>
          New portfolio
        </button>
      </div>

      <h4>Explore a problem set</h4>
      <p><small>Problems in a portfolio share lemmas, experiments and failed directions. The scheduler explores connections and works on useful shared claims.</small></p>
      <div className="form">
        <select aria-label="research pool" value={poolId} onChange={(e) => setPoolId(e.target.value)}>
          <option value="">choose portfolio…</option>
          {portfolios.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select multiple size={7} aria-label="problems to explore together" value={poolProblems}
          onChange={(e) => setPoolProblems(Array.from(e.target.selectedOptions, (o) => o.value))}>
          {problems.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
        </select>
        <label>Session allowance per new problem
          <input type="number" min={1} value={poolBudget} onChange={(e) => setPoolBudget(Number(e.target.value))} />
        </label>
        <button disabled={busy || !poolId || !poolProblems.length || poolBudget < 1}
          onClick={() => run("add problem set", () => api.startResearchPool(poolId, {
            problem_ids: poolProblems, session_budget_per_problem: poolBudget, default_mode: "ultra",
          }))}>Add problems to shared research</button>
      </div>
      {pool && <div className="card">
        <b>{pool.problems.length} problems · {pool.total_claims ?? pool.claims.length} shared claims</b>
        <p><small>Adopted connections are useful research routes. Only claims checked by Lean are formally verified.</small></p>
        {pool.links.filter((l) => l.kind === "applies_to").slice(-20).map((link) => <div key={link.id} className="card">
          <small><b>{link.status}</b> · {pool.claims.find((c) => c.id === link.claim)?.statement ?? link.claim}
            {" → "}{pool.problems.find((p) => p.id === link.target)?.title ?? link.target}</small>
          <p>{link.reason}</p>
        </div>)}
        <small>{pool.failed_directions.length} recorded failed or disputed directions in this view.</small>
      </div>}

      <h4>Problem status review</h4>
      <p>
        <small>
          Workers can only flag a claimed resolution; changing a problem's status is a collaborator decision and is published with your name.
        </small>
      </p>
      <div className="form">
        <select value={review.problem_id} onChange={(e) => setReview({ ...review, problem_id: e.target.value })} aria-label="problem to review">
          <option value="">problem…</option>
          {problems.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title} ({p.status})
            </option>
          ))}
        </select>
        <select value={review.status} onChange={(e) => setReview({ ...review, status: e.target.value })} aria-label="new status">
          {PROBLEM_STATUSES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <input value={review.note} onChange={(e) => setReview({ ...review, note: e.target.value })} placeholder="what you checked" />
        <button
          disabled={busy || !review.problem_id}
          onClick={() => run("status review", () => api.reviewProblem(review.problem_id, review.status, review.note))}
        >
          Record review
        </button>
      </div>

      <h4>Campaigns</h4>
      {campaigns.map((c) => (
        <div key={c.id} className={`card ${campaignId === c.id ? "active" : ""}`}>
          <button className="link" onClick={() => setCampaignId(c.id)}>
            {c.problem_title}
          </button>
          <br />
          <small>
            {c.state} · {c.research_outcome} · gen {c.generation} · {c.sessions_used}/{c.session_budget} sessions · {c.ideas} ideas · mode{" "}
            {String(c.policy.default_mode ?? "ultra")} · policy {c.policy_version}
          </small>
          <div className="toolbar">
            {c.state !== "paused" && (
              <button disabled={busy} onClick={() => run("pause campaign", () => api.setCampaignState(c.id, "paused"))}>
                Pause
              </button>
            )}
            {c.state !== "active" && (
              <button disabled={busy} onClick={() => run("activate campaign", () => api.setCampaignState(c.id, "active"))}>
                Activate
              </button>
            )}
          </div>
        </div>
      ))}
      <div className="form">
        <select value={newCampaign.portfolio_id} onChange={(e) => setNewCampaign({ ...newCampaign, portfolio_id: e.target.value })}>
          <option value="">portfolio…</option>
          {portfolios.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select value={newCampaign.problem_id} onChange={(e) => setNewCampaign({ ...newCampaign, problem_id: e.target.value })}>
          <option value="">problem…</option>
          {problems.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title}
            </option>
          ))}
        </select>
        <select value={newCampaign.mode} onChange={(e) => setNewCampaign({ ...newCampaign, mode: e.target.value })} aria-label="default mode">
          {MODES.map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
        <input
          type="number"
          min={1}
          value={newCampaign.budget}
          onChange={(e) => setNewCampaign({ ...newCampaign, budget: Number(e.target.value) })}
          aria-label="session budget"
        />
        <button
          disabled={busy || !newCampaign.portfolio_id || !newCampaign.problem_id}
          onClick={() =>
            run("create campaign", () =>
              api.createCampaign({
                portfolio_id: newCampaign.portfolio_id,
                problem_id: newCampaign.problem_id,
                session_budget: newCampaign.budget,
                default_mode: newCampaign.mode,
              }),
            )
          }
        >
          New campaign
        </button>
      </div>

      {campaignId && (
        <>
          <h4>Manual assignment (Fusion/Ultra pilot)</h4>
          <div className="form">
            <select value={assignment.role} onChange={(e) => setAssignment({ ...assignment, role: e.target.value })} aria-label="role">
              {ROLES.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
            <select value={assignment.mode} onChange={(e) => setAssignment({ ...assignment, mode: e.target.value })} aria-label="mode">
              {MODES.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
            <input value={assignment.group} onChange={(e) => setAssignment({ ...assignment, group: e.target.value })} placeholder="comparison group" />
            <button
              disabled={busy}
              onClick={() =>
                run("assign", () =>
                  api.assign(campaignId, {
                    role: assignment.role,
                    mode: assignment.mode,
                    comparison_group: assignment.group,
                    idea_id: selectedIdeaId,
                  }),
                )
              }
            >
              Enqueue{selectedIdeaId ? " on selected idea" : ""}
            </button>
          </div>

          <h4>Attempts ({attempts.length})</h4>
          {attempts.map((a) => (
            <div key={a.id} className="card">
              {a.research_task?.reason && <p>{a.research_task.reason}</p>}
              <small>
                <b>{a.role}</b> · {a.status}
                {a.status_detail ? ` (${a.status_detail})` : ""} · requested <b>{a.requested_mode}</b>
                {a.reported_mode ? ` · reported ${a.reported_mode}` : ""}
                {a.comparison_group ? ` · ${a.comparison_group}` : ""}
                {a.usage.acus_consumed !== undefined ? ` · ${String(a.usage.acus_consumed)} ACU` : ""}
              </small>
              <div className="toolbar">
                {a.provider_session_url && (
                  <a href={a.provider_session_url} target="_blank" rel="noreferrer noopener">
                    session
                  </a>
                )}
                <button onClick={() => api.prompt(a.id).then((p) => setPrompt(p.prompt))}>prompt</button>
                {["queued", "dispatching", "running", "blocked"].includes(a.status) && (
                  <button disabled={busy} onClick={() => run("cancel", () => api.cancel(a.id))}>
                    cancel
                  </button>
                )}
              </div>
              {a.error && <p className="error">{a.error}</p>}
            </div>
          ))}
        </>
      )}

      {selectedIdeaId && (
        <>
          <h4>Selected idea</h4>
          <div className="toolbar">
            <button disabled={busy} onClick={() => run("pin", () => api.pin(selectedIdeaId, true))}>
              Pin
            </button>
            <button disabled={busy} onClick={() => run("unpin", () => api.pin(selectedIdeaId, false))}>
              Unpin
            </button>
            <button disabled={busy} onClick={() => run("revive", () => api.revive(selectedIdeaId))}>
              Revive archived branch
            </button>
          </div>
        </>
      )}

      {prompt && (
        <div className="card">
          <div className="toolbar">
            <b>Bounded prompt</b>
            <button onClick={() => setPrompt(null)}>close</button>
          </div>
          <pre className="prompt">{prompt}</pre>
        </div>
      )}
    </div>
  );
}
