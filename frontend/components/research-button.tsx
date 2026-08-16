"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { JobConflictError, fetchJob, startJob, type Job } from "@/lib/api";

const POLL_MS = 2000;

/**
 * Asks for AI research on one company.
 *
 * The result never comes back through the job. Research persists what
 * validation accepted, so when the run finishes this refreshes the route and
 * the report renders through the ordinary research endpoint — the same path a
 * report generated from the CLI takes.
 *
 * A repeat press is cheap by design: the pipeline reuses a stored report when
 * the evidence, the scoring rules and the prompt are all unchanged, so pressing
 * this twice costs nothing the second time.
 */
export function ResearchButton({ ticker, hasReport }: { ticker: string; hasReport: boolean }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const router = useRouter();

  useEffect(() => {
    if (job === null || job.status !== "RUNNING") return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function follow(id: number) {
      try {
        const latest = await fetchJob(id);
        if (cancelled) return;
        setJob(latest);
        if (latest.status === "RUNNING") {
          timer = setTimeout(() => follow(id), POLL_MS);
        } else {
          // Whatever validation accepted is now stored; ask the page for it.
          router.refresh();
        }
      } catch {
        if (!cancelled) setError("lost track of the run");
      }
    }

    void follow(job.id);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [job, router]);

  async function run() {
    const warning =
      `Research ${ticker} with the configured model. This calls a paid API.\n\n` +
      `A report already stored for unchanged evidence is reused at no cost. Continue?`;
    if (!confirm(warning)) return;

    setPending(true);
    setError(null);
    try {
      setJob(await startJob("research", ticker));
    } catch (caught) {
      if (caught instanceof JobConflictError) {
        setJob(caught.running);
      } else {
        setError(caught instanceof Error ? caught.message : "could not start");
      }
    } finally {
      setPending(false);
    }
  }

  const running = job?.status === "RUNNING";

  return (
    <div className="research-run">
      <button type="button" className="run" disabled={pending || running} onClick={run}>
        {running ? "researching…" : hasReport ? "Re-run research" : "Research this company"}
      </button>
      {job && job.status === "FAILED" ? (
        <span className="run-error">the run failed — see the log on Jobs</span>
      ) : null}
      {error ? <span className="run-error">{error}</span> : null}
    </div>
  );
}
