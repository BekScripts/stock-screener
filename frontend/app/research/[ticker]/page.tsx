import Link from "next/link";

import { DeepResearchActions } from "@/components/deep-research-actions";
import { DeepResearchReportView } from "@/components/deep-research-report";
import { CategoryBadge, RiskBadge, ScoreBadge, StateBadge } from "@/components/badges";
import { fetchDeepResearch, fetchDeepResearchHistory, fetchStock } from "@/lib/api";

export async function generateMetadata({ params }: { params: Promise<{ ticker: string }> }) {
  const { ticker } = await params;
  return { title: `${ticker.toUpperCase()} Deep Research · Compounder Radar` };
}

/**
 * One company's deep research.
 *
 * The snapshot at the top comes from the stock endpoint and the report from the
 * deep research endpoint — two reads rather than one denormalised payload,
 * because the score is a property of the company today and the report explains
 * the score as it stood when the report was written. Showing them from one
 * source would hide the case where those differ, which is exactly the case a
 * reader needs to notice.
 */
export default async function DeepResearchPage({
  params,
}: {
  params: Promise<{ ticker: string }>;
}) {
  const { ticker } = await params;
  const symbol = ticker.toUpperCase();

  const [stock, report, history] = await Promise.all([
    fetchStock(symbol).catch(() => null),
    fetchDeepResearch(symbol),
    fetchDeepResearchHistory(symbol),
  ]);

  return (
    <main>
      <div className="card">
        <div className="deep-head">
          <div>
            <h2>
              {symbol}
              {stock ? <span className="company-name"> · {stock.name}</span> : null}
            </h2>
            {stock?.score ? (
              <div className="badge-row">
                <ScoreBadge value={stock.score.final_score} />
                <CategoryBadge value={stock.score.breakdown.category} />
                <RiskBadge level={stock.score.breakdown.risk?.level ?? null} labelled />
                <StateBadge state={stock.ranking_state} />
              </div>
            ) : (
              <p className="note" style={{ paddingTop: 0 }}>
                This company has no current score.
              </p>
            )}
          </div>
          <Link className="linklike" href={`/stocks/${symbol}`}>
            Score detail →
          </Link>
        </div>
      </div>

      {report ? (
        <DeepResearchReportView report={report} history={history} />
      ) : (
        <div className="card">
          <h2>No Deep Research report yet.</h2>
          <p className="note" style={{ paddingTop: 0 }}>
            Running Deep Research refreshes this company, prepares its SEC evidence, searches
            for current public material and asks a model to read all of it. It takes a few
            minutes.
          </p>
          <DeepResearchActions ticker={symbol} hasReport={false} />
        </div>
      )}
    </main>
  );
}
