"use client";

export default function Error({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="card">
      <h2>Could not load</h2>
      <p className="error">{error.message}</p>
      <p className="note">
        The dashboard reads a local API. Start it with{" "}
        <code>uv run uvicorn stock_screener.api:app --reload</code> and try again.
      </p>
      <button type="button" className="tab" onClick={reset}>
        Retry
      </button>
    </div>
  );
}
