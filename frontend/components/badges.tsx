import { score as fmtScore, title } from "@/lib/format";

const CATEGORY_CLASS: Record<string, string> = {
  EXCEPTIONAL_RESEARCH_CANDIDATE: "exceptional",
  STRONG_RESEARCH_CANDIDATE: "strong",
  WORTH_WATCHING: "watching",
};

export function ScoreBadge({ value }: { value: number | null }) {
  return <span className="badge score">{fmtScore(value)}</span>;
}

export function CategoryBadge({ value }: { value: string | null }) {
  if (!value) return <span className="badge neutral">—</span>;
  return <span className={`badge ${CATEGORY_CLASS[value] ?? "neutral"}`}>{title(value)}</span>;
}

export function RiskBadge({ level }: { level: string | null }) {
  if (!level) return <span className="badge neutral">—</span>;
  return <span className={`badge risk-${level.toLowerCase()}`}>{level}</span>;
}

/** PRELIMINARY until the metered enrichment pass has verified market cap and
 *  liquidity. Shown on every row because a preliminary ranking is a valid
 *  ranking, and knowing which one you are reading matters. */
export function StateBadge({ state }: { state: string | null }) {
  if (!state) return <span className="badge neutral">—</span>;
  return <span className={`badge state-${state.toLowerCase()}`}>{state}</span>;
}
