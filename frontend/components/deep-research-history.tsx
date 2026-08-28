"use client";

import { useState } from "react";

import { fetchDeepResearchReport, type DeepResearchReport, type DeepResearchSummary } from "@/lib/api";
import { day } from "@/lib/format";

/**
 * The append-only trail of reports for one company.
 *
 * Deep reports are never overwritten, so how a reading changed across runs is
 * recoverable. This exposes that and nothing more: selecting a row renders that
 * report. No diffing, no comparison — those are their own feature and inventing
 * a half version here would be worse than not having one.
 */
export function DeepResearchHistory({
  history,
  currentId,
  onSelect,
}: {
  history: DeepResearchSummary[];
  currentId: number;
  onSelect: (report: DeepResearchReport) => void;
}) {
  const [loading, setLoading] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (history.length <= 1) return null;

  async function open(id: number) {
    if (id === currentId) return;
    setLoading(id);
    setError(null);
    try {
      onSelect(await fetchDeepResearchReport(id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not load that report");
    } finally {
      setLoading(null);
    }
  }

  return (
    <div className="card">
      <h2>History</h2>
      <p className="note" style={{ paddingTop: 0 }}>
        Deep Research reports are kept, never replaced.
      </p>
      <div className="history">
        {history.map((entry) => (
          <button
            type="button"
            key={entry.id}
            className={`history-row${entry.id === currentId ? " current" : ""}`}
            disabled={loading !== null}
            onClick={() => open(entry.id)}
          >
            <span className="history-when">
              {day(entry.generated_at)}
              {entry.id === currentId ? " · latest" : ""}
            </span>
            <span className="history-meta">
              {entry.status} · {entry.confidence}
              {entry.external_state ? ` · sources ${entry.external_state}` : ""}
            </span>
            {loading === entry.id && <span className="history-meta">loading…</span>}
          </button>
        ))}
      </div>
      {error && <p className="note">{error}</p>}
    </div>
  );
}
