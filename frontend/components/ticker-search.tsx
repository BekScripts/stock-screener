"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { searchCompanies, type SearchHit } from "@/lib/api";
import { score } from "@/lib/format";

/** Wait this long after the last keystroke before asking the API. */
const DEBOUNCE_MS = 200;

/**
 * Reaches any company, ranked or not.
 *
 * The rankings cap at 500 rows, so most of a scored universe cannot be browsed
 * to. This is the way in — including for a company that is not ranked at all,
 * whose page then explains which status kept it out.
 */
export function TickerSearch() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
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
        if (!cancelled) {
          setHits(found);
          setOpen(true);
        }
      } catch {
        if (!cancelled) setHits([]);
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query]);

  // A click anywhere else closes the list.
  useEffect(() => {
    function onClick(event: MouseEvent) {
      if (!container.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function go(ticker: string) {
    setQuery("");
    setHits([]);
    setOpen(false);
    router.push(`/stocks/${ticker}`);
  }

  return (
    <div className="search" ref={container}>
      <input
        type="search"
        value={query}
        placeholder="Find a company…"
        aria-label="Find a company by ticker or name"
        onChange={(event) => setQuery(event.target.value)}
        onFocus={() => setOpen(hits.length > 0)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && hits.length > 0) go(hits[0].ticker);
          if (event.key === "Escape") setOpen(false);
        }}
      />
      {open && hits.length > 0 ? (
        <ul className="hits">
          {hits.map((hit) => (
            <li key={hit.ticker}>
              <button type="button" onClick={() => go(hit.ticker)}>
                <span className="hit-ticker">{hit.ticker}</span>
                <span className="hit-name">{hit.name}</span>
                <span className="hit-score">
                  {hit.final_score === null ? (hit.scoring_status ?? "—") : score(hit.final_score)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
