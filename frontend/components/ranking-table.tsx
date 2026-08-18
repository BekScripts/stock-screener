import Link from "next/link";

import { CategoryBadge, RiskBadge, ScoreBadge, StateBadge } from "@/components/badges";
import { WatchButton } from "@/components/watch-button";
import type { RankingRow } from "@/lib/api";
import { money, percent, score } from "@/lib/format";

/**
 * One ranking view.
 *
 * Deliberately not every metric the engine derives — the columns here are the
 * ones that answer "is this worth opening", and the detail page answers the
 * rest. Coverage is a column because a 78 built on partial data is a different
 * claim from a 78 built on complete data.
 */
export function RankingTable({ rows }: { rows: RankingRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="empty">
        Nothing to show. Run <code>stock-screener score</code> to produce a ranking, then reload.
      </p>
    );
  }

  // Every cell is `white-space: nowrap`, so the table has a minimum width that
  // a narrow window — or one more column — will exceed. Without somewhere to
  // scroll it overflows the card instead, and the columns past the edge sit on
  // the page background with no card behind them.
  return (
    <div className="table-scroll">
      <table className="rankings">
      <thead>
        <tr>
          <th>#</th>
          <th>Company</th>
          <th>Score</th>
          <th>Category</th>
          <th>Growth</th>
          <th>Quality</th>
          <th>Value</th>
          <th>Momentum</th>
          <th>Financial risk</th>
          <th>Coverage</th>
          <th>State</th>
          <th>Market cap</th>
          <th>Rev growth</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.ticker}>
            <td>{row.rank}</td>
            <td>
              <Link href={`/stocks/${row.ticker}`}>
                <div className="ticker">{row.ticker}</div>
                <div className="company">{row.name}</div>
              </Link>
            </td>
            <td>
              <ScoreBadge value={row.final_score} />
            </td>
            <td>
              <CategoryBadge value={row.score_category} />
            </td>
            <td>{score(row.growth_score)}</td>
            <td>{score(row.quality_score)}</td>
            <td>{score(row.valuation_score)}</td>
            <td>{score(row.momentum_score)}</td>
            <td>
              {score(row.risk_penalty)} <RiskBadge level={row.risk_level} />
            </td>
            <td>{percent(row.data_coverage, 0)}</td>
            <td>
              <StateBadge state={row.ranking_state} />
            </td>
            <td>{money(row.market_cap)}</td>
            <td>{percent(row.revenue_growth_yoy)}</td>
            <td>
              <WatchButton ticker={row.ticker} initial={row.watched} />
            </td>
          </tr>
        ))}
      </tbody>
      </table>
    </div>
  );
}
