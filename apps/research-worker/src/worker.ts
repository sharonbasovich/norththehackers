import "dotenv/config";
import { createHash, randomUUID } from "node:crypto";
import { Redis } from "ioredis";
import { getCollections, getMongoClient, proofDocument, recordClaimVersion, publishEpisode } from "@triviality/database";
import { config } from "./config.js";
import { runSwarm } from "./swarm.js";
import { searchLiterature } from "./literature.js";

const redis = new Redis(config.redisUrl, { maxRetriesPerRequest: null });

type OpenAlexWork = {
  id: string;
  title?: string | null;
  publication_year?: number | null;
  publication_date?: string | null;
  cited_by_count?: number;
  authorships?: Array<{ author?: { display_name?: string | null } | null }>;
  abstract_inverted_index?: Record<string, number[]> | null;
  primary_location?: { landing_page_url?: string | null; pdf_url?: string | null } | null;
  open_access?: { oa_url?: string | null } | null;
};

type LiteratureHit = OpenAlexWork & {
  discovery: "seed" | "expanded";
  matchedQuery: string;
};

function id(prefix: string): string {
  return `${prefix}_${randomUUID().replaceAll("-", "").slice(0, 16)}`;
}

function metadata(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function abstractFromIndex(index: OpenAlexWork["abstract_inverted_index"]): string | null {
  if (!index) return null;
  const words: string[] = [];
  for (const [word, positions] of Object.entries(index)) for (const position of positions) words[position] = word;
  return words.filter(Boolean).join(" ") || null;
}

async function emit(episodeId: string, type: string, payload: Record<string, unknown>): Promise<void> {
  const collections = await getCollections();
  await collections.researchEvents.insertOne({ _id: id("event"), episodeId, type, payload, createdAt: new Date(), updatedAt: new Date() });
}

async function updateStage(episodeId: string, stage: string, progress: number): Promise<void> {
  const collections = await getCollections();
  await collections.researchEpisodes.updateOne({ _id: episodeId }, { $set: { stage, updatedAt: new Date() }, $max: { progress } });
  await emit(episodeId, "research.stage.updated", { stage, progress });
}

async function addGraphNode(episodeId: string, entityId: string, entityType: string, label: string, detail: string, x: number, y: number, status: string): Promise<void> {
  const collections = await getCollections();
  const existing = await collections.graphNodes.findOne({ entityType: entityType as never, entityId });
  const existingMetadata = metadata(existing?.metadata);
  const existingEpisodeIds = Array.isArray(existingMetadata.episodeIds) ? existingMetadata.episodeIds.filter((value): value is string => typeof value === "string") : [];
  const episodeIds = Array.from(new Set([...existingEpisodeIds, typeof existingMetadata.episodeId === "string" ? existingMetadata.episodeId : undefined, episodeId].filter((value): value is string => Boolean(value))));
  await collections.graphNodes.updateOne(
    { entityType: entityType as never, entityId },
    { $set: { label, metadata: { ...existingMetadata, episodeId, episodeIds, type: entityType.toLowerCase().replace("research_", ""), detail, x, y, status }, updatedAt: new Date() }, $setOnInsert: { _id: id("graph"), createdAt: new Date() } },
    { upsert: true },
  );
}

async function addGraphEdge(episodeId: string, source: string, target: string, type: string, label: string): Promise<void> {
  const collections = await getCollections();
  const existing = await collections.graphRelationships.findOne({ fromNodeId: source, toNodeId: target, type: type as never });
  const existingMetadata = metadata(existing?.metadata);
  const existingEpisodeIds = Array.isArray(existingMetadata.episodeIds) ? existingMetadata.episodeIds.filter((value): value is string => typeof value === "string") : [];
  const episodeIds = Array.from(new Set([...existingEpisodeIds, typeof existingMetadata.episodeId === "string" ? existingMetadata.episodeId : undefined, episodeId].filter((value): value is string => Boolean(value))));
  await collections.graphRelationships.updateOne(
    { fromNodeId: source, toNodeId: target, type: type as never },
    { $set: { confidence: 0.8, rationale: label, metadata: { ...existingMetadata, episodeId, episodeIds, label }, updatedAt: new Date() }, $setOnInsert: { _id: id("edge"), createdAt: new Date() } },
    { upsert: true },
  );
}

async function fetchOpenAlex(query: string, perPage: number): Promise<OpenAlexWork[]> {
  const url = new URL("https://api.openalex.org/works");
  url.searchParams.set("search", query.replace(/[?*]/g, " ").slice(0, 450));
  url.searchParams.set("sort", "relevance_score:desc");
  url.searchParams.set("per-page", String(perPage));
  const response = await fetch(url, { signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`OpenAlex request failed: ${response.status} ${response.statusText}`);
  const payload = await response.json() as { results?: OpenAlexWork[] };
  return (payload.results ?? []).filter((work) => work.title);
}

async function fetchLiterature(title: string, statement: string, area: string): Promise<LiteratureHit[]> {
  const seedQuery = `${area} ${title} ${statement}`;
  const seedWorks = await fetchOpenAlex(seedQuery, 8);
  const expansionQueries = Array.from(new Set([
    `${area} ${title}`,
    `${area} ${statement}`,
    ...seedWorks.slice(0, 3).map((work) => work.title ?? ""),
  ].map((query) => query.trim()).filter(Boolean))).slice(0, 5);
  const expandedBatches = await Promise.all(expansionQueries.map((query) => fetchOpenAlex(query, 4)));
  const hits = new Map<string, LiteratureHit>();

  for (const work of seedWorks) {
    hits.set(work.id, { ...work, discovery: "seed", matchedQuery: seedQuery });
  }
  for (let index = 0; index < expandedBatches.length; index += 1) {
    for (const work of expandedBatches[index]) {
      if (!hits.has(work.id)) hits.set(work.id, { ...work, discovery: "expanded", matchedQuery: expansionQueries[index] });
    }
  }
  return [...hits.values()].slice(0, 24);
}

async function runEpisode(episodeId: string): Promise<void> {
  const collections = await getCollections();
  const episode = await collections.researchEpisodes.findOne({ _id: episodeId });
  const problem = await collections.researchProblems.findOne({ episodeId });
  if (!episode || !problem || episode.status !== "ACTIVE") return;
  try {
    await addGraphNode(episodeId, problem._id, "RESEARCH_PROBLEM", "Research space", `${episode.area ?? "Mathematics"} · ${episode.title}`, 50, 13, "active");
    await updateStage(episodeId, "Finding seed literature for the research space", 14);
    const works = await fetchLiterature(episode.title, problem.statement, episode.area ?? "Mathematics").catch(async (error) => {
      await emit(episodeId, "research.literature.unavailable", { message: "Literature lookup unavailable; continuing with explicitly uncited mathematical reasoning" });
      return [] as LiteratureHit[];
    });
    await emit(episodeId, "research.literature.expanded", {
      seedCount: works.filter((work) => work.discovery === "seed").length,
      expandedCount: works.filter((work) => work.discovery === "expanded").length,
      totalCount: works.length,
    });
    await updateStage(episodeId, "Expanding the literature graph", 24);
    const paperIds: string[] = [];
    for (const [index, work] of works.entries()) {
      const paperId = `paper_${createHash("sha1").update(work.id).digest("hex").slice(0, 16)}`;
      paperIds.push(paperId);
      const now = new Date();
      const existingPaper = await collections.papers.findOne({ _id: paperId });
      const existingPaperMetadata = metadata(existingPaper?.rawMetadata);
      const existingPaperEpisodeIds = Array.isArray(existingPaperMetadata.episodeIds) ? existingPaperMetadata.episodeIds.filter((value): value is string => typeof value === "string") : [];
      const paperEpisodeIds = Array.from(new Set([...existingPaperEpisodeIds, typeof existingPaperMetadata.episodeId === "string" ? existingPaperMetadata.episodeId : undefined, episodeId].filter((value): value is string => Boolean(value))));
      await collections.papers.updateOne({ _id: paperId }, { $set: { externalId: work.id, title: work.title ?? "Untitled paper", abstract: abstractFromIndex(work.abstract_inverted_index) ?? undefined, authors: (work.authorships ?? []).map((author) => author.author?.display_name ?? "").filter(Boolean), subjects: [episode.area ?? "mathematics"], citedByCount: work.cited_by_count ?? 0, publishedAt: work.publication_date ? new Date(work.publication_date) : undefined, landingUrl: work.primary_location?.landing_page_url ?? undefined, openAccessUrl: work.open_access?.oa_url ?? work.primary_location?.pdf_url ?? undefined, rawMetadata: { ...existingPaperMetadata, episodeId, episodeIds: paperEpisodeIds, source: "OpenAlex", discovery: work.discovery, matchedQuery: work.matchedQuery, relevance: "Retrieved from the seed or expanded literature search for this research space." }, updatedAt: now }, $setOnInsert: { createdAt: now } }, { upsert: true });
      await addGraphNode(episodeId, paperId, "PAPER", `${work.discovery === "seed" ? "Seed" : "Expanded"} paper ${index + 1}`, work.title ?? "Untitled paper", 8 + (index % 8) * 12, 42 + Math.floor(index / 8) * 18, "candidate");
      await addGraphEdge(episodeId, problem._id, paperId, "SUPPORTS", work.discovery === "seed" ? "seed literature" : "expanded related literature");
    }
    await emit(episodeId, "research.literature.completed", { count: works.length });

    {
      await updateStage(episodeId, "Starting WorkSwarm research team", 30);
      const phases: Record<string, number> = { Plan: 35, Explore: 45, Challenge: 60, Formalize: 75, Deliver: 95 };
      const outcome = await runSwarm({
        episode_id: episodeId, title: episode.title, statement: problem.statement,
        lean_statement: episode.leanStatement ?? "", proof_attempts: Math.min(6, episode.budget ?? 2),
        role_models: episode.roleModels,
        exploration_rounds: episode.explorationRounds ?? 4, stagnation_threshold: episode.stagnationThreshold ?? 2,
        literature: works.map((work, index) => ({ id: paperIds[index], title: work.title, year: work.publication_year,
          abstract: abstractFromIndex(work.abstract_inverted_index), url: work.primary_location?.landing_page_url ?? work.id })),
      }, async (message) => {
        await emit(episodeId, "research.swarm.event", message);
        if (message.kind !== "progress") return;
        const event = metadata(message.event);
        if (event.kind === "log" && typeof event.message === "string" && event.message.startsWith("TRIVIALITY_EVENT ")) {
          const domain = JSON.parse(event.message.slice(17));
          if (domain.kind === "discovery") {
            const { id: discoveryId, ...entry } = domain.entry;
            const now = new Date();
            await collections.researchDiscoveries.updateOne({ _id: `${episodeId}:${discoveryId}` }, {
              $set: { ...entry, episodeId, discoveryId, updatedAt: now }, $setOnInsert: { createdAt: now },
            }, { upsert: true });
          }
          if (domain.kind === "branch") {
            await collections.researchEpisodes.updateOne({ _id: episodeId }, { $set: { [`branches.${domain.branch.id - 1}`]: domain.branch } });
          }
          if (domain.kind === "round") await updateStage(episodeId, domain.summary, 30 + Math.floor(60 * domain.round / domain.limit));
        }
        if (event.kind === "phase") await updateStage(episodeId, `Research team: ${String(event.phase)}`, phases[String(event.phase)] ?? 40);
        if (typeof event.agent_id === "string" && String(event.kind).startsWith("agent_")) {
          const attemptId = `attempt_${createHash("sha256").update(`${episodeId}:${event.agent_id}`).digest("hex").slice(0, 24)}`;
          const status = event.kind === "agent_started" ? "RUNNING" : event.kind === "agent_completed" ? "SUCCEEDED" : "FAILED";
          const now = new Date();
          await collections.researchAttempts.updateOne({ _id: attemptId }, {
            $set: { status, updatedAt: now, proofState: String(event.outcome ?? event.message ?? "Working"),
              ...(status !== "RUNNING" ? { completedAt: now } : {}) },
            $setOnInsert: { episodeId, hypothesisId: "", strategy: `WorkSwarm · ${String(event.phase ?? "research")}`,
              input: { role: event.label, model: event.model, prompt: event.prompt, framework: "WorkSwarm SwarmFlow" },
              createdAt: now, startedAt: now },
          }, { upsert: true });
        }
      }, async (request) => searchLiterature(episodeId, String(request.query ?? ""), request.broad === true));
      const hypothesisIds: string[] = [];
      for (const [index, report] of outcome.reports.entries()) {
        if (!report) continue;
        const hypothesisId = id("hypothesis");
        hypothesisIds.push(hypothesisId);
        const now = new Date();
        await collections.researchHypotheses.insertOne({ _id: hypothesisId, episodeId, problemId: problem._id,
          statement: report.evidence, rationale: report.approach, assumptions: report.risks,
          expectedConsequences: { approach: report.approach, nextStep: report.next_step },
          status: outcome.branches?.[index]?.status === "abandoned" ? "ABANDONED" : "PROMISING", createdAt: now, updatedAt: now });
        await addGraphNode(episodeId, hypothesisId, "RESEARCH_HYPOTHESIS", `Researcher ${index + 1}`, report.approach, 20 + index * 30, 35, "candidate");
        await addGraphEdge(episodeId, problem._id, hypothesisId, "PRODUCES", "investigates");
      }
      let formalizationId: string | undefined;
      if (outcome.proof) {
        const proof = outcome.proof;
        formalizationId = id("formalization");
        const now = new Date();
        // Versioned claim: an unchanged statement reuses the claim; an edited
        // statement starts a new version that never inherits verification.
        const claim = await recordClaimVersion(collections, {
          statement: proof.statement || proof.theoremName,
          leanDeclaration: proof.lean,
          formalizationStatus: proof.verified ? "complete" : "in_progress",
          episodeId,
          problemId: episode.atlasProblemId ?? problem._id,
        });
        await collections.formalizations.insertOne({ _id: formalizationId, episodeId, claimId: claim._id, claimVersion: claim.claimVersion, system: "Lean", systemVersion: "4 / Std",
          verified: proof.verified, checker: proof.checker, axioms: proof.axioms, verificationLog: proof.log,
          theoremName: proof.theoremName, statement: proof.statement, leanSource: proof.lean,
          explanation: proof.explanation,
          latexSource: proofDocument(episode.title, problem.statement, proof.explanation ?? "", `${outcome.summary}\n\n${proof.checker}`),
          createdAt: now, updatedAt: now });
        await addGraphNode(episodeId, formalizationId, "FORMALIZATION", "Fixed theorem", proof.checker, 50, 65, proof.verified ? "verified" : "candidate");
        for (const hypothesisId of hypothesisIds) await addGraphEdge(episodeId, hypothesisId, formalizationId, "SUPPORTS", "reviewed evidence");
      }
      const targetVerified = outcome.status === "verified" && !!episode.leanStatement && outcome.proof?.verified === true;
      const now = new Date();
      const resultId = id("result");
      await collections.researchResults.insertOne({ _id: resultId, episodeId,
        title: targetVerified ? "Verified formal target" : outcome.status === "formalized" ? "Checked formalization — target review required" : "Research findings",
        summary: outcome.summary, status: targetVerified ? "VERIFIED" : "CANDIDATE",
        evidence: { formalizationId, framework: "WorkSwarm SwarmFlow", targetOrigin: outcome.target_origin, outcome },
        createdAt: now, updatedAt: now });
      await addGraphNode(episodeId, resultId, "RESEARCH_RESULT", "Team result", outcome.summary, 50, 87, targetVerified ? "verified" : "candidate");
      if (formalizationId) await addGraphEdge(episodeId, formalizationId, resultId, "PRODUCES", "checked outcome");
      if (outcome.stop_reason) await collections.researchAttempts.updateMany({ episodeId, status: "RUNNING" }, { $set: {
        status: "CANCELLED", proofState: outcome.summary, completedAt: now, updatedAt: now,
      } });
      await collections.researchEpisodes.updateOne({ _id: episodeId }, { $set: {
        status: targetVerified ? "VERIFIED" : "PROMISING", stage: outcome.stop_reason === "token_budget" ? "Token budget reached"
          : outcome.stop_reason === "checker_unavailable" ? "Lean verification unavailable"
          : outcome.status === "blocked" ? "Research team blocked" : "Research team complete",
        summary: outcome.summary, progress: 100, completedAt: now, updatedAt: now,
      } });
      await emit(episodeId, "research.job.completed", { verified: targetVerified, outcome: outcome.status });
      try {
        const published = await publishEpisode(collections, episodeId);
        if (published > 0) await emit(episodeId, "research.publish.completed", { published });
      } catch (error) {
        console.warn(`Publication projection failed for episode ${episodeId}:`, error);
      }
      return;
    }

  } catch (error) {
    const message = error instanceof Error ? error.message : "Research worker failed";
    {
      await collections.researchAttempts.updateMany({ episodeId, status: "RUNNING" }, { $set: {
        status: "FAILED", error: message, proofState: "Research team stopped before this agent completed.",
        completedAt: new Date(), updatedAt: new Date(),
      } });
    }
    await collections.researchEpisodes.updateOne({ _id: episodeId }, { $set: { status: "ABANDONED", stage: "Research worker blocked", error: message, updatedAt: new Date() } });
    await emit(episodeId, "research.job.failed", { error: message });
    console.error(`Research episode ${episodeId} failed:`, error);
  }
}

async function main(): Promise<void> {
  await redis.ping();
  console.log("Research worker listening on triviality:research:jobs");
  while (true) {
    const item = await redis.brpop("triviality:research:jobs", 0);
    if (!item?.[1]) continue;
    const payload = JSON.parse(item[1]) as { episodeId?: string };
    if (payload.episodeId) await runEpisode(payload.episodeId);
  }
}

main().catch(async (error) => {
  console.error(error);
  await redis.quit();
  await (await getMongoClient()).close();
  process.exitCode = 1;
});
