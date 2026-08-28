import type { DeepSource } from "@/lib/api";
import { day, title } from "@/lib/format";

const TIER_GROUPS: { tier: string; label: string; help: string }[] = [
  {
    tier: "TIER_1_PRIMARY",
    label: "Primary Sources",
    help: "The filer or the company speaking directly",
  },
  {
    tier: "TIER_2_REPUTABLE",
    label: "Reputable News",
    help: "News organisations with an editorial process",
  },
  {
    tier: "TIER_3_SUPPORTING",
    label: "Supporting Sources",
    help: "Trade and industry publications",
  },
];

/**
 * Every current source the report's claims actually cite.
 *
 * Grouped by how close the publisher sits to the facts, because that is what a
 * reader discounts by. Titles link out — a citation you cannot follow is a
 * citation you have to take on trust, which is the opposite of the point.
 *
 * An empty set is a result, not a failure. A company nobody has written about
 * recently, researched honestly from filings and stored figures, is a better
 * outcome than one padded with whatever a search returned.
 */
export function Sources({ sources }: { sources: DeepSource[] }) {
  if (sources.length === 0) {
    return (
      <div className="card">
        <h2>Sources</h2>
        <p className="note" style={{ paddingTop: 0 }}>
          No additive current external sources were accepted. This report relies on
          Compounder Radar data and SEC evidence.
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Sources</h2>
      {TIER_GROUPS.map(({ tier, label, help }) => {
        const group = sources.filter((source) => source.tier === tier);
        if (group.length === 0) return null;
        return (
          <div className="section" key={tier}>
            <h3>
              {label} <span className="tier-help">{help}</span>
            </h3>
            {group.map((source) => (
              <div className="source" key={source.evidence_id}>
                <div className="source-head">
                  <span className="source-publisher">{source.publisher}</span>
                  <span className="source-meta">
                    {title(source.source_type)}
                    {source.published_at ? ` · ${day(source.published_at)}` : " · undated"}
                  </span>
                </div>
                <a href={source.url} target="_blank" rel="noreferrer" className="source-title">
                  {source.title}
                </a>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
