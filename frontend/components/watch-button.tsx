"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { setWatched } from "@/lib/api";

/**
 * The only control in the dashboard that writes anything.
 *
 * Optimistic, because the round trip is local and a star that waits feels
 * broken — but it reverts on failure rather than lying about what was stored.
 */
export function WatchButton({ ticker, initial }: { ticker: string; initial: boolean }) {
  const [on, setOn] = useState(initial);
  const [failed, setFailed] = useState(false);
  const [pending, startTransition] = useTransition();
  const router = useRouter();

  async function toggle() {
    const next = !on;
    setOn(next);
    setFailed(false);
    try {
      await setWatched(ticker, next);
      startTransition(() => router.refresh());
    } catch {
      setOn(!next);
      setFailed(true);
    }
  }

  return (
    <button
      type="button"
      className="watch"
      data-on={on}
      disabled={pending}
      onClick={toggle}
      title={failed ? "Could not reach the API" : on ? "Remove from watchlist" : "Add to watchlist"}
      aria-label={on ? `Remove ${ticker} from watchlist` : `Add ${ticker} to watchlist`}
    >
      {failed ? "!" : on ? "★" : "☆"}
    </button>
  );
}
