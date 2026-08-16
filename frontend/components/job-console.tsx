"use client";

import { useCallback, useEffect, useState } from "react";

import { RunButton } from "@/components/run-button";
import { fetchJob, fetchJobKinds, fetchJobs, type Job, type JobKind } from "@/lib/api";
import { day } from "@/lib/format";

const POLL_MS = 2000;

/** How often to retry after a request failed, rather than giving up on it. */
const RETRY_MS = 5000;

/**
 * Every command the API will run, plus what happened last time.
 *
 * The control list is built from `/api/jobs/kinds` rather than hard-coded, so a
 * command added to `JOB_KINDS` on the backend appears here without a frontend
 * change.
 *
 * Per-company commands are absent: a research run needs a ticker, and the place
 * to choose one is the company's own page, not a list of buttons.
 */
export function JobConsole() {
  const [kinds, setKinds] = useState<JobKind[]>([]);
  const [running, setRunning] = useState<Job[]>([]);
  const [recent, setRecent] = useState<Job[]>([]);
  const [watching, setWatching] = useState<number | null>(null);
  const [log, setLog] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const { running: inFlight, recent: history } = await fetchJobs();
      setRunning(inFlight);
      setRecent(history);
      setError(null);
      return inFlight;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not reach the API");
      // Null, not an empty list. "Nothing is running" ends the poll; "the
      // request failed" has to schedule another one, and returning `[]` for
      // both made a failure look like an idle console that never recovered.
      return null;
    }
  }, []);

  // Retried until it succeeds: without the kinds there are no controls to press,
  // so giving up here leaves an empty panel even once the API is back.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    function load() {
      fetchJobKinds()
        .then((loaded) => {
          if (!cancelled) setKinds(loaded);
        })
        .catch(() => {
          if (cancelled) return;
          setError("jobs are disabled, or the API is unreachable");
          timer = setTimeout(load, RETRY_MS);
        });
    }

    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      const inFlight = await refresh();
      if (cancelled) return;
      if (inFlight === null || inFlight.length > 0) {
        timer = setTimeout(poll, inFlight === null ? RETRY_MS : POLL_MS);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [refresh]);

  // The log of whichever job is being watched, followed while it runs.
  useEffect(() => {
    if (watching === null) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function follow(id: number) {
      try {
        const job = await fetchJob(id);
        if (cancelled) return;
        setLog(job.log ?? "");
        if (job.status === "RUNNING") {
          timer = setTimeout(() => follow(id), POLL_MS);
        }
      } catch {
        if (!cancelled) setLog("could not read the log");
      }
    }

    void follow(watching);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [watching]);

  const runnable = kinds.filter((kind) => !kind.needs_target);

  return (
    <>
      <div className="card">
        <h2>Run a command</h2>
        {error ? <p className="empty">{error}</p> : null}
        <table className="jobs">
          <tbody>
            {runnable.map((kind) => {
              const inFlight = running.find((job) => job.kind === kind.kind);
              return (
                <tr key={kind.kind}>
                  <td>
                    {kind.label}
                    {kind.spends_money ? <span className="cost" title="Can incur a charge">$</span> : null}
                  </td>
                  <td className="muted">~{kind.minutes} min</td>
                  <td>
                    {inFlight ? (
                      <button type="button" className="run" onClick={() => setWatching(inFlight.id)}>
                        running · watch
                      </button>
                    ) : (
                      <RunButton kind={kind} onStarted={(job) => setWatching(job.id)} />
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="muted">
          AI research runs one company at a time, from that company&apos;s page.
        </p>
      </div>

      {watching !== null ? (
        <div className="card">
          <h2>Output</h2>
          <pre className="log">{log || "waiting for output…"}</pre>
        </div>
      ) : null}

      <div className="card">
        <h2>History</h2>
        {recent.length === 0 ? (
          <p className="empty">Nothing has been run yet.</p>
        ) : (
          <table className="jobs">
            <thead>
              <tr>
                <th>Command</th>
                <th>Started</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {recent.map((job) => (
                <tr key={job.id}>
                  <td>
                    {job.label}
                    {job.target ? ` · ${job.target}` : ""}
                  </td>
                  <td className="muted">{day(job.started_at)}</td>
                  <td>
                    <span className="state" data-status={job.status}>
                      {job.status}
                    </span>
                    {job.exit_code !== null && job.exit_code !== 0 ? (
                      <span className="muted"> exit {job.exit_code}</span>
                    ) : null}
                  </td>
                  <td>
                    <button type="button" className="link" onClick={() => setWatching(job.id)}>
                      log
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
