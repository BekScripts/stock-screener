import Link from "next/link";

import { DeepResearchSearch } from "@/components/deep-research-search";

export const metadata = { title: "Deep Research · Compounder Radar" };

/**
 * The way in to Deep Research.
 *
 * Deliberately one thing: name a company. Deep research is asked for one ticker
 * at a time — it refreshes that company, searches for current material about it
 * and pays a model to read the result — so a list of everything would be an
 * invitation to spend money by browsing.
 */
export default function ResearchPage() {
  return (
    <main>
      <div className="card">
        <h2>Deep Research</h2>
        <p className="note" style={{ paddingTop: 0 }}>
          Refresh a company&apos;s fundamentals, SEC filings and current public evidence, then
          produce a source-grounded research report. Every claim carries the evidence it rests
          on, and the CompounderScore is explained, never revised.
        </p>

        <DeepResearchSearch />

        <p className="note">
          Try <Link href="/research/MU">MU</Link>, <Link href="/research/ACTG">ACTG</Link> or{" "}
          <Link href="/research/FANG">FANG</Link>.
        </p>
      </div>
    </main>
  );
}
