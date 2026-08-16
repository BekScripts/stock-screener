/**
 * The one place that talks to the API.
 *
 * Every number the dashboard shows comes from here. Nothing in this app derives
 * a score, re-ranks a list or recomputes a metric — the database is the source
 * of truth, and a screen that did its own arithmetic could disagree with the CLI
 * about what a company scored today.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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
