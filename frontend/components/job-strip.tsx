"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { fetchJobs, type Job } from "@/lib/api";

/** How often to ask while something is in flight. */
const POLL_MS = 2000;

/**
 * What is running, visible from every page.
 *
 * Polls only while a job is in flight and stops when nothing is, so an idle
 * dashboard makes one request per navigation rather than one every two seconds
 * forever.
 *
 * When a run finishes it refreshes the route, which is how the rankings pick up
 * a completed scoring run without anyone reloading the page.
 */
export function JobStrip() {
  const [running, setRunning] = useState<Job[]>([]);
  const [reachable, setReachable] = useState(true);
  const wasRunning = useRef(false);
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const { running: inFlight } = await fetchJobs();
        if (cancelled) return;

        setRunning(inFlight);
        setReachable(true);

        // The transition from "something running" to "nothing running" is the
        // only moment the rest of the page has new data to show.
        if (wasRunning.current && inFlight.length === 0) {
          router.refresh();
        }
        wasRunning.current = inFlight.length > 0;

        if (inFlight.length > 0) {
          timer = setTimeout(poll, POLL_MS);
        }
      } catch {
        if (!cancelled) setReachable(false);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [router]);

  if (!reachable) {
    return <div className="strip" data-state="down">API unreachable</div>;
  }

  if (running.length === 0) {
    return null;
  }

  return (
    <div className="strip" data-state="running">
      {running.map((job) => (
        <span key={job.id} className="strip-job">
          <span className="pulse" aria-hidden="true" />
          {job.label}
          {job.target ? ` · ${job.target}` : ""}
        </span>
      ))}
    </div>
  );
}
