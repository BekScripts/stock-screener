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

/** The risk engine prices three things and only three: dilution, cash runway
 *  and leverage. It reads a balance sheet, so a LOW here means the finances are
 *  sound — not that the business is. Business-model, integration, commodity and
 *  capital-allocation risk are outside what the score can see, and an unqualified
 *  "Risk: LOW" claims more than the algorithm knows. `labelled` names the measure
 *  where the badge stands alone; in a table the column heading does that job. */
export function RiskBadge({ level, labelled }: { level: string | null; labelled?: boolean }) {
  if (!level) return <span className="badge neutral">—</span>;
  return (
    <span
      className={`badge risk-${level.toLowerCase()}`}
      title="Financial risk only: dilution, cash runway and leverage"
    >
      {labelled ? `Financial risk: ${level}` : level}
    </span>
  );
}

/** PRELIMINARY until the metered enrichment pass has verified market cap and
 *  liquidity. Shown on every row because a preliminary ranking is a valid
 *  ranking, and knowing which one you are reading matters. */
export function StateBadge({ state }: { state: string | null }) {
  if (!state) return <span className="badge neutral">—</span>;
  return <span className={`badge state-${state.toLowerCase()}`}>{state}</span>;
}
