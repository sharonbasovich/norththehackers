// Thin typed client. Public calls never send credentials; private calls send X-API-Key.

export type Layer = "atlas" | "lineage" | "dependency" | "association";
export const LAYERS: Layer[] = ["atlas", "lineage", "dependency", "association"];

export interface Area {
  id: string;
  slug: string;
  name: string;
  description: string;
  parent_id: string | null;
  depth: number;
  problem_count: number;
}

export interface PublishedRecord {
  record_type: string;
  record_id: string;
  version: number;
  evidence_label: string;
  policy_version: string;
  published_at: string;
}

export interface ProblemSource {
  title: string;
  url: string;
  location: string;
  asserted_status: string;
  asserted_at: string;
  retrieved_date: string;
  review_state: string;
}

export interface StatusReview {
  reviewer: string;
  date: string;
  from: string;
  to: string;
  note: string;
}

export interface Problem extends PublishedRecord {
  id: string;
  slug: string;
  title: string;
  statement: string;
  definitions: string;
  assumptions: string;
  attribution: string;
  status: string;
  formal_target: string;
  areas: { slug: string; name: string }[];
  sources: ProblemSource[];
  status_reviews?: StatusReview[];
  status_checked_at?: string;
  origin?: string;
}

export interface Idea extends PublishedRecord {
  id: string;
  problem_id: string;
  title: string;
  approach: string;
  mechanism: string;
  next_experiment: string;
  novelty_rationale: string;
  method_tags: string[];
  generation: number;
  depth: number;
  parent_ids: string[];
  evidence_status: string;
  scheduling_status: string;
  formalization_status: string;
  score: number;
  pinned: boolean;
}

export interface Claim extends PublishedRecord {
  id: string;
  idea_id: string;
  statement: string;
  assumptions: string;
  lean_declaration: string;
  formalization_status: string;
  claim_version: number;
}

export interface Evidence extends PublishedRecord {
  id: string;
  idea_id: string;
  claim_id: string | null;
  check_type: string;
  result: string;
  summary: string;
  certified: boolean;
  verifier: string;
  verifier_version: string;
  assumptions: string[];
  axioms: string[];
  reasons: string[];
}

export interface ProblemDetail {
  problem: Problem;
  campaigns: PublishedRecord[];
  ideas: Idea[];
  claims: Claim[];
  evidence: Evidence[];
}

export interface GraphNode {
  id: string;
  type: "area" | "problem" | "idea" | "claim";
  label: string;
  slug?: string;
  depth?: number;
  parent_id?: string | null;
  status?: string;
  evidence_label?: string;
  evidence_status?: string;
  scheduling_status?: string;
  formalization_status?: string;
  generation?: number;
  problem_id?: string;
  idea_id?: string;
  areas?: string[];
  score?: number;
  method_tags?: string[];
}

export interface GraphLink {
  source: string;
  target: string;
  layer: Layer;
  kind: string;
  status?: string;
}

export interface Graph {
  nodes: GraphNode[];
  links: GraphLink[];
  layers: string[];
}

export interface PublicEvent {
  id: number;
  type: string;
  record_type: string;
  record_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Portfolio {
  id: string;
  name: string;
  paused: boolean;
  max_concurrent_sessions: number;
}

export interface Campaign {
  id: string;
  problem_id: string;
  problem_title: string;
  portfolio_id: string;
  state: string;
  research_outcome: string;
  generation: number;
  session_budget: number;
  sessions_used: number;
  policy: Record<string, unknown>;
  policy_version: string;
  ideas: number;
  attempts: number;
}

export interface Attempt {
  id: string;
  campaign_id: string;
  idea_id: string | null;
  role: string;
  requested_mode: string;
  reported_mode: string | null;
  provider: string;
  provider_session_id: string | null;
  provider_session_url: string | null;
  status: string;
  status_detail: string;
  usage: Record<string, unknown>;
  error: string;
  retries: number;
  comparison_group: string;
  research_task?: { reason?: string; focus_claim_id?: string; target_problem_id?: string };
  created_at: string;
}

export interface ResearchPool {
  problems: { id: string; title: string }[];
  claims: { id: string; statement: string; state: string; scope: string }[];
  links: { id: string; claim: string; target: string; kind: string; status: string; reason: string }[];
  failed_directions: { id: string; title: string; state: string }[];
  total_claims?: number;
}

export interface SchedulerStatus {
  provider: string;
  background_enabled: boolean;
  interval_seconds: number;
  lean_available: boolean;
  running_attempts: number;
  modes: string[];
}

async function getJson<T>(url: string, headers: HeadersInit = {}): Promise<T> {
  const r = await fetch(url, { headers });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return (await r.json()) as T;
}

export const publicApi = {
  areas: () => getJson<Area[]>("/public/areas"),
  problems: (area?: string) =>
    getJson<Problem[]>(`/public/problems${area ? `?area=${encodeURIComponent(area)}` : ""}`),
  problem: (slug: string) => getJson<ProblemDetail>(`/public/problems/${encodeURIComponent(slug)}`),
  graph: (layers: Layer[], problem?: string) => {
    const q = new URLSearchParams({ layers: layers.join(",") });
    if (problem) q.set("problem", problem);
    return getJson<Graph>(`/public/graph?${q}`);
  },
  events: (sinceId = 0, limit = 1000) =>
    getJson<PublicEvent[]>(`/public/events?since_id=${sinceId}&limit=${limit}`),
  labels: () => getJson<{ evidence: Record<string, string>; problem_status: Record<string, string> }>("/public/labels"),
};

export class PrivateApi {
  constructor(private readonly key: string) {}

  private headers(): HeadersInit {
    return { "X-API-Key": this.key, "Content-Type": "application/json" };
  }

  private async send<T>(method: string, url: string, body?: unknown): Promise<T> {
    const r = await fetch(url, {
      method,
      headers: this.headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    return (await r.json()) as T;
  }

  status = () => this.send<SchedulerStatus>("GET", "/private/scheduler/status");
  tick = () => this.send<Record<string, number>>("POST", "/private/scheduler/tick");
  seed = () => this.send<Record<string, number>>("POST", "/private/seed");
  importWikipedia = (dry_run: boolean) =>
    this.send<Record<string, unknown>>("POST", "/private/atlas/import/wikipedia", { dry_run });
  importErdos = (dry_run: boolean) =>
    this.send<Record<string, unknown>>("POST", "/private/atlas/import/erdos", { dry_run });
  reviewProblem = (problemId: string, status: string, note: string) =>
    this.send<{ status: string }>("POST", `/private/problems/${problemId}/review`, { status, note });
  portfolios = () => this.send<Portfolio[]>("GET", "/private/portfolios");
  createPortfolio = (name: string, max_concurrent_sessions: number) =>
    this.send<{ id: string }>("POST", "/private/portfolios", { name, max_concurrent_sessions });
  pausePortfolio = (id: string, paused: boolean) =>
    this.send<{ paused: boolean }>("POST", `/private/portfolios/${id}/pause?paused=${paused}`);
  researchPool = (id: string) => this.send<ResearchPool>("GET", `/private/portfolios/${id}/research`);
  startResearchPool = (id: string, body: {
    problem_ids: string[]; session_budget_per_problem: number; default_mode: string;
  }) => this.send<{ created_campaign_ids: string[] }>("POST", `/private/portfolios/${id}/research`, body);
  campaigns = () => this.send<Campaign[]>("GET", "/private/campaigns");
  createCampaign = (body: {
    portfolio_id: string;
    problem_id: string;
    session_budget: number;
    default_mode: string;
  }) => this.send<Campaign>("POST", "/private/campaigns", body);
  setCampaignState = (id: string, state: string) =>
    this.send<Campaign>("POST", `/private/campaigns/${id}/state?state=${state}`);
  attempts = (campaignId: string) =>
    this.send<Attempt[]>("GET", `/private/campaigns/${campaignId}/attempts`);
  assign = (
    campaignId: string,
    body: { role: string; idea_id?: string | null; mode?: string; comparison_group?: string },
  ) => this.send<Attempt>("POST", `/private/campaigns/${campaignId}/assignments`, body);
  prompt = (attemptId: string) =>
    this.send<{ prompt: string; prompt_hash: string }>("GET", `/private/attempts/${attemptId}/prompt`);
  cancel = (attemptId: string) => this.send<Attempt>("POST", `/private/attempts/${attemptId}/cancel`);
  pin = (ideaId: string, pinned: boolean) =>
    this.send<{ pinned: boolean }>("POST", `/private/ideas/${ideaId}/pin?pinned=${pinned}`);
  revive = (ideaId: string) => this.send<Record<string, unknown>>("POST", `/private/ideas/${ideaId}/revive`);
  review = (evidenceId: string, result: string, note: string) =>
    this.send<Record<string, unknown>>("POST", `/private/evidence/${evidenceId}/review`, { result, note });
  events = (sinceId = 0) => this.send<PublicEvent[]>("GET", `/private/events?since_id=${sinceId}`);
}
