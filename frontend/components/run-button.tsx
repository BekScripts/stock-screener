"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { JobConflictError, startJob, type Job, type JobKind } from "@/lib/api";

/**
 * Starts one pipeline command.
 *
 * Not optimistic, unlike the watchlist star. Starting a run is a real event
 * with a cost — an hour of ingest, or a model call — so the button waits for
 * the API to confirm rather than claiming something began that did not.
 *
 * A 409 is not an error state. It means the run is already going, which is the
 * answer the person wanted; the button says so and stays disabled.
 */
export function RunButton({
  kind,
  target,
  onStarted,
}: {
  kind: JobKind;
  target?: string;
  onStarted?: (job: Job) => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  async function run() {
    if (kind.spends_money && !confirm(spendWarning(kind, target))) {
      return;
    }

    setPending(true);
    setError(null);
    try {
      const job = await startJob(kind.kind, target);
      onStarted?.(job);
      router.refresh();
    } catch (caught) {
      if (caught instanceof JobConflictError) {
        onStarted?.(caught.running);
        setError("already running");
      } else {
        setError(caught instanceof Error ? caught.message : "could not start");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <button type="button" className="run" disabled={pending} onClick={run}>
      {pending ? "starting…" : `Run${target ? ` · ${target}` : ""}`}
      {error ? <span className="run-error"> {error}</span> : null}
    </button>
  );
}

/** What the confirmation says before anything that can be billed. */
function spendWarning(kind: JobKind, target?: string): string {
  const subject = target ? `${kind.label} for ${target}` : kind.label;
  return (
    `${subject} calls a metered provider and can incur a charge.\n\n` +
    `Roughly ${kind.minutes} minute${kind.minutes === 1 ? "" : "s"}. Continue?`
  );
}
