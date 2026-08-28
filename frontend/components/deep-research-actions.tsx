"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  DEEP_RESEARCH_KIND,
  DEEP_RESEARCH_REFRESH_KIND,
  JobConflictError,
  startJob,
  type Job,
} from "@/lib/api";

const NORMAL_NOTE =
  "Reuses current external evidence collected in the last few hours, and reuses " +
  "the whole report when nothing has changed. Cached runs cost $0; a fresh model " +
  "generation has recently been around $0.10.";

const REFRESH_NOTE =
  "Searches current external sources again, and generates a new report if the " +
  "evidence changed. Costs more than a normal run.";

/**
 * The two ways to run deep research, kept visibly apart.
 *
 * They are one flag apart in the backend and a world apart in cost. The normal
 * run usually reuses recent evidence and an existing report and costs nothing;
 * the refresh deliberately searches again and often pays for a new one. Showing
 * them as equal buttons would make the expensive one an easy mis-click, so the
 * refresh is secondary and says what it does before it is pressed.
 *
 * Execution goes through the existing job system. A 409 is not an error: it
 * means the run is already going, which is the answer the person wanted.
 */
export function DeepResearchActions({
  ticker,
  hasReport,
}: {
  ticker: string;
  hasReport: boolean;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const router = useRouter();

  async function run(kind: string, note: string) {
    if (!confirm(`${note}\n\nRun for ${ticker}?`)) return;

    setPending(kind);
    setMessage(null);
    try {
      const job: Job = await startJob(kind, ticker);
      setMessage(`Started · job ${job.id}. Progress appears in the strip above.`);
      router.refresh();
    } catch (caught) {
      if (caught instanceof JobConflictError) {
        setMessage(`Deep Research is already running for ${ticker}.`);
      } else {
        setMessage(caught instanceof Error ? caught.message : "could not start");
      }
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="deep-actions">
      <button
        type="button"
        className="run"
        disabled={pending !== null}
        onClick={() => run(DEEP_RESEARCH_KIND, NORMAL_NOTE)}
      >
        {pending === DEEP_RESEARCH_KIND ? "starting…" : "Run Deep Research"}
      </button>

      {hasReport && (
        <button
          type="button"
          className="run secondary"
          disabled={pending !== null}
          onClick={() => run(DEEP_RESEARCH_REFRESH_KIND, REFRESH_NOTE)}
        >
          {pending === DEEP_RESEARCH_REFRESH_KIND ? "starting…" : "Refresh Current Sources"}
        </button>
      )}

      <p className="note deep-actions-note">
        {hasReport
          ? "A normal run reuses recent evidence and may cost $0. Refreshing searches current sources again and may generate a new report."
          : "Cached runs may cost $0. A fresh model generation has recently been around $0.10."}
      </p>

      {message && <p className="note deep-actions-message">{message}</p>}
    </div>
  );
}
