/**
 * The one place that talks to the API.
 *
 * Every number the dashboard shows comes from here. Nothing in this app derives
 * a score, re-ranks a list or recomputes a metric — the database is the source
 * of truth, and a screen that did its own arithmetic could disagree with the CLI
 * about what a company scored today.
 */

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type RankingRow = {
  rank: number;
  ticker: string;
  name: string;
  sector: string | null;
  final_score: number | null;
  raw_score: number | null;
  growth_score: number | null;
  quality_score: number | null;
  valuation_score: number | null;
  momentum_score: number | null;
  risk_penalty: number | null;
  risk_level: string | null;
  score_category: string | null;
  data_coverage: number | null;
  market_cap: number | null;
  revenue_growth_yoy: number | null;
  ranking_state: string;
  score_change_30d: number | null;
  warnings: string[];
  watched: boolean;
};

export type Metric = {
  key: string;
  label: string;
  value: number | null;
  unit: string;
};

export type Subscore = {
  name: string;
  points: number | null;
  max_points: number;
  observed: number | null;
  unit: string | null;
  note: string | null;
};

export type Component = {
  name: string;
  status: string;
  score: number | null;
  max_points: number;
  coverage: number | null;
  subscores: Subscore[];
};

export type Breakdown = {
  growth: Component;
  quality: Component;
  valuation: Component;
  momentum: Component;
  raw_score: number | null;
  final_score: number | null;
  category: string | null;
  data_coverage: number | null;
  warnings: string[];
  risk: {
    total_penalty: number | null;
    level: string | null;
    dilution_penalty: number | null;
    runway_penalty: number | null;
    balance_sheet_penalty: number | null;
    liquidity_penalty: number | null;
    warnings: string[];
  };
};

export type StockDetail = {
  ticker: string;
  name: string;
  sector: string | null;
  industry: string | null;
  exchange: string | null;
  market_cap: number | null;
  market_cap_source: string | null;
  ranking_state: string | null;
  watched: boolean;
  has_research: boolean;
  metrics: Metric[];
  score: {
    score_date: string;
    score_version: string;
    scoring_status: string;
    final_score: number | null;
    score_change_7d: number | null;
    score_change_30d: number | null;
    breakdown: Breakdown;
  } | null;
};

export type Claim = {
  text: string;
  basis: "DETERMINISTIC" | "EXTRACTED" | "INTERPRETATION" | "UNKNOWN";
  evidence: string[];
};

export type ResearchReport = {
  ticker: string;
  status: string;
  generated_at: string;
  model_id: string;
  score_date: string;
  score_version: string;
  unknowns: string[];
  confidence: {
    level: string;
    ceiling: string;
    claimed: string;
    rationale: string;
    metric_coverage: number | null;
    filing_coverage: number;
  };
  issues: { code: string; detail: string; section: string | null }[];
  sections: { key: string; label: string; claims: Claim[] }[];
};

export type WatchlistEntry = {
  ticker: string;
  name: string;
  sector: string | null;
  note: string | null;
  added_at: string;
  final_score: number | null;
  score_category: string | null;
  ranking_state: string | null;
  scoring_status: string | null;
};

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`API ${response.status} for ${path}`);
  }
  return (await response.json()) as T;
}

export const RANKINGS = {
  top: { path: "/api/rankings", label: "Top Opportunities" },
  "hidden-gems": { path: "/api/rankings/hidden-gems", label: "Hidden Gems" },
  "wrong-price": { path: "/api/rankings/wrong-price", label: "Great Company, Wrong Price" },
  improving: { path: "/api/rankings/improving", label: "Improving Fast" },
} as const;

export type RankingKey = keyof typeof RANKINGS;

export async function fetchRanking(key: RankingKey, limit = 50): Promise<RankingRow[]> {
  const { rows } = await get<{ rows: RankingRow[] }>(`${RANKINGS[key].path}?limit=${limit}`);
  return rows;
}

export async function fetchStock(ticker: string): Promise<StockDetail> {
  return get<StockDetail>(`/api/stocks/${ticker}`);
}

export async function fetchResearch(ticker: string): Promise<ResearchReport | null> {
  const response = await fetch(`${API_URL}/api/stocks/${ticker}/research`, { cache: "no-store" });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`API ${response.status} for research/${ticker}`);
  }
  return (await response.json()) as ResearchReport;
}

export async function fetchWatchlist(): Promise<WatchlistEntry[]> {
  const { entries } = await get<{ entries: WatchlistEntry[] }>("/api/watchlist");
  return entries;
}

/** Watchlist writes run in the browser, so they use the public URL directly. */
export async function setWatched(ticker: string, watched: boolean): Promise<void> {
  const response = await fetch(`${API_URL}/api/watchlist/${ticker}`, {
    method: watched ? "POST" : "DELETE",
    headers: { "Content-Type": "application/json" },
    body: watched ? JSON.stringify({ note: null }) : undefined,
  });
  if (!response.ok) {
    throw new Error(`could not update the watchlist (${response.status})`);
  }
}

/* -- jobs ------------------------------------------------------------------
 *
 * A job is a pipeline command running in a process the API spawned. Nothing
 * here carries a result: every command persists what it produces, so a
 * finished job means the page should refetch, not read something from the job.
 */

export type JobStatus = "RUNNING" | "SUCCEEDED" | "FAILED" | "UNKNOWN";

export type Job = {
  id: number;
  kind: string;
  label: string;
  target: string | null;
  status: JobStatus;
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
  /** Only present when a single job is fetched by id. */
  log?: string;
};

export type JobKind = {
  kind: string;
  label: string;
  needs_target: boolean;
  spends_money: boolean;
  minutes: number;
};

/** Thrown when a job is already running, carrying the one that is. */
export class JobConflictError extends Error {
  constructor(readonly running: Job) {
    super(`${running.label} is already running`);
    this.name = "JobConflictError";
  }
}

export async function fetchJobKinds(): Promise<JobKind[]> {
  const { kinds } = await get<{ kinds: JobKind[] }>("/api/jobs/kinds");
  return kinds;
}

export async function fetchJobs(): Promise<{ running: Job[]; recent: Job[] }> {
  return get<{ running: Job[]; recent: Job[] }>("/api/jobs");
}

export async function fetchJob(id: number): Promise<Job> {
  return get<Job>(`/api/jobs/${id}`);
}

/**
 * Start a command.
 *
 * A 409 is not a failure — it means the button was pressed twice and the run is
 * already going. It surfaces as `JobConflictError` so a caller can show the job
 * in flight rather than an error.
 */
export async function startJob(kind: string, target?: string): Promise<Job> {
  const response = await fetch(`${API_URL}/api/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, target: target ?? null }),
  });

  if (response.status === 409) {
    const body = (await response.json()) as { detail: { job: Job } };
    throw new JobConflictError(body.detail.job);
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `could not start ${kind} (${response.status})`);
  }

  return (await response.json()) as Job;
}

/* -- search ---------------------------------------------------------------- */

export type SearchHit = {
  ticker: string;
  name: string;
  final_score: number | null;
  scoring_status: string | null;
};

export async function searchCompanies(query: string): Promise<SearchHit[]> {
  const { hits } = await get<{ hits: SearchHit[] }>(
    `/api/companies/search?q=${encodeURIComponent(query)}`,
  );
  return hits;
}

/* -- deep research ---------------------------------------------------------
 *
 * Phase 6. A separate layer over the screener: one company refreshed against
 * its current fundamentals, its filings and what has since been published,
 * then a source-grounded report.
 *
 * Read-only, deliberately. Running deep research is a job — it takes minutes
 * and can spend money, and the job system already has the concurrency guard.
 * `DEEP_RESEARCH_KIND` and `DEEP_RESEARCH_REFRESH_KIND` are the two keys the
 * backend allows; a caller picks a key, never an argument.
 */

export const DEEP_RESEARCH_KIND = "deep-research";
export const DEEP_RESEARCH_REFRESH_KIND = "deep-research-refresh";

/** Where a deep claim's authority comes from. */
export type DeepBasis =
  | "DETERMINISTIC"
  | "EXTRACTED"
  | "EXTERNAL"
  | "INTERPRETATION"
  | "UNKNOWN";

/** Why a section ended up empty. The two mean very different things. */
export type UnknownReason = "NO_EVIDENCE" | "NO_VALID_CLAIMS";

/** Whether the external evidence was searched for, reused, or came back thin. */
export type ExternalState = "FRESH" | "REUSED" | "DEGRADED";

export type DeepClaim = {
  text: string;
  basis: DeepBasis;
  evidence: string[];
  unknown_reason: UnknownReason | null;
};

export type DeepSection = {
  key: string;
  label: string;
  unknown_reason: UnknownReason | null;
  claims: DeepClaim[];
};

export type DeepSource = {
  evidence_id: string;
  source_type: string;
  tier: string;
  publisher: string;
  title: string;
  url: string;
  published_at: string | null;
  retrieved_at: string;
};

/** A validation issue, as a code and a place. Never the rejected text. */
export type DeepIssue = {
  code: string;
  section: string | null;
};

export type DeepResearchReport = {
  id: number;
  ticker: string;
  status: string;
  as_of: string;
  generated_at: string;
  score_version: string;
  contract_version: string;
  prompt_version: string;
  model_id: string;
  confidence: {
    level: string;
    ceiling: string;
    claimed: string;
    rationale: string;
    metric_coverage: number | null;
    filing_coverage: number;
    external_coverage: number;
  };
  unknowns: string[];
  unknown_reasons: Record<string, UnknownReason>;
  external_state: ExternalState | null;
  external_collected_at: string | null;
  sections: DeepSection[];
  sources: DeepSource[];
  issues: DeepIssue[];
};

export type DeepResearchSummary = {
  id: number;
  generated_at: string;
  as_of: string;
  status: string;
  confidence: string;
  model_id: string;
  prompt_version: string;
  external_state: ExternalState | null;
  external_collected_at: string | null;
};

/** The latest validated report, or null when the company has never been researched. */
export async function fetchDeepResearch(ticker: string): Promise<DeepResearchReport | null> {
  const response = await fetch(`${API_URL}/api/deep-research/${ticker}`, { cache: "no-store" });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`API ${response.status} for deep-research/${ticker}`);
  }
  return (await response.json()) as DeepResearchReport;
}

/**
 * Every report for a company, newest first.
 *
 * Empty means the company exists and has never been researched; a 404 means the
 * company is unknown, which is a different thing and left to the caller.
 */
export async function fetchDeepResearchHistory(ticker: string): Promise<DeepResearchSummary[]> {
  const response = await fetch(`${API_URL}/api/deep-research/${ticker}/history`, {
    cache: "no-store",
  });
  if (response.status === 404) {
    return [];
  }
  if (!response.ok) {
    throw new Error(`API ${response.status} for deep-research/${ticker}/history`);
  }
  return (await response.json()) as DeepResearchSummary[];
}

/** One historical report, opened from the history control. */
export async function fetchDeepResearchReport(id: number): Promise<DeepResearchReport> {
  return get<DeepResearchReport>(`/api/deep-research/reports/${id}`);
}
