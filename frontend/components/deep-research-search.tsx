"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { searchCompanies, type SearchHit } from "@/lib/api";
import { score } from "@/lib/format";

const DEBOUNCE_MS = 200;

/**
 * Pick a company to research.
 *
 * Uses the same search endpoint as the masthead, because there is one company
 * index and a second implementation of it would be a second thing to get wrong.
 * The difference is where a hit goes: here it opens the research page rather
 * than the stock page.
 */
export function DeepResearchSearch() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const router = useRouter();

  useEffect(() => {
    const fragment = query.trim();
    if (fragment.length === 0) {
      setHits([]);
      return;
    }

    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const found = await searchCompanies(fragment);
        if (!cancelled) setHits(found);
      } catch {
        if (!cancelled) setHits([]);
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query]);

  function open(ticker: string) {
    router.push(`/research/${ticker}`);
  }

  return (
    <div className="deep-search">
      <input
        type="search"
        value={query}
        placeholder="Ticker or company — MU, ACTG, FANG"
        aria-label="Search for a company to research"
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && hits.length > 0) open(hits[0].ticker);
        }}
      />
      {hits.length > 0 && (
        <ul className="deep-hits">
          {hits.map((hit) => (
            <li key={hit.ticker}>
              <button type="button" onClick={() => open(hit.ticker)}>
                <span className="hit-ticker">{hit.ticker}</span>
                <span className="hit-name">{hit.name}</span>
                <span className="hit-score">{score(hit.final_score)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
