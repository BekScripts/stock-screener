import Link from "next/link";

import { RankingTable } from "@/components/ranking-table";
import { RANKINGS, fetchRanking, type RankingKey } from "@/lib/api";

/** The four views, as tabs. Each is a stored ranking, not a client-side sort. */
export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ view?: string }>;
}) {
  const { view } = await searchParams;
  const active: RankingKey = view && view in RANKINGS ? (view as RankingKey) : "top";
  const rows = await fetchRanking(active);

  return (
    <>
      <div className="tabs">
        {(Object.keys(RANKINGS) as RankingKey[]).map((key) => (
          <Link key={key} href={key === "top" ? "/" : `/?view=${key}`}>
            <span className="tab" data-active={key === active}>
              {RANKINGS[key].label}
            </span>
          </Link>
        ))}
      </div>

      <div className="card">
        <h2>
          {RANKINGS[active].label} · {rows.length} companies
        </h2>
        <RankingTable rows={rows} />
      </div>
    </>
  );
}
