import Link from "next/link";

import { CategoryBadge, ScoreBadge, StateBadge } from "@/components/badges";
import { WatchButton } from "@/components/watch-button";
import { fetchWatchlist } from "@/lib/api";
import { day } from "@/lib/format";

export default async function Watchlist() {
  const entries = await fetchWatchlist();

  return (
    <div className="card">
      <h2>Watchlist · {entries.length} companies</h2>
      {entries.length === 0 ? (
        <p className="empty">
          Nothing watched yet. Star a company in a ranking and it will stay here even after it
          drops out of the top fifty.
        </p>
      ) : (
        <table className="rankings">
          <thead>
            <tr>
              <th>Company</th>
              <th>Score</th>
              <th>Category</th>
              <th>State</th>
              <th>Added</th>
              <th>Note</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.ticker}>
                <td>
                  <Link href={`/stocks/${entry.ticker}`}>
                    <div className="ticker">{entry.ticker}</div>
                    <div className="company">{entry.name}</div>
                  </Link>
                </td>
                <td>
                  <ScoreBadge value={entry.final_score} />
                </td>
                <td>
                  <CategoryBadge value={entry.score_category} />
                </td>
                <td>
                  <StateBadge state={entry.ranking_state} />
                </td>
                <td>{day(entry.added_at)}</td>
                <td className="company">{entry.note ?? "—"}</td>
                <td>
                  <WatchButton ticker={entry.ticker} initial />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
