"use client";

import { useState } from "react";

import { BASIS_HELP } from "@/components/deep-research-claim";
import { DeepResearchActions } from "@/components/deep-research-actions";
import { DeepResearchHistory } from "@/components/deep-research-history";
import { Section } from "@/components/deep-research-section";
import { Sources } from "@/components/deep-research-sources";
import type { DeepResearchReport as Report, DeepResearchSummary, ExternalState } from "@/lib/api";
import { day, percent, title } from "@/lib/format";

/** How current the external half of the evidence is, in a sentence. */
function currentness(state: ExternalState | null, collectedAt: string | null): string {
  const when = collectedAt ? ` (${day(collectedAt)})` : "";
  if (state === "REUSED") {
    return `Current sources were checked recently and reused${when}.`;
  }
  if (state === "DEGRADED") {
    return (
      "Current-source collection was incomplete; this report relies more heavily on " +
      `Compounder Radar data and SEC evidence${when}.`
    );
  }
  if (state === "FRESH") {
    return `Current sources were searched for this report${when}.`;
  }
  return "No record of a current-source search for this report.";
}

/**
 * One validated deep research report.
 *
 * Everything on this page came out of the database through the API. Nothing
 * here recomputes a score, re-reads evidence or derives a figure — the report is
 * the product of a validated pipeline, and a screen that did its own arithmetic
 * could disagree with it.
 */
export function DeepResearchReportView({
  report: latest,
  history,
}: {
  report: Report;
  history: DeepResearchSummary[];
}) {
  const [report, setReport] = useState<Report>(latest);
  const viewingOlder = report.id !== latest.id;

  return (
    <>
      <div className="card">
        <div className="deep-head">
          <div>
            <h2>Deep Research</h2>
            <p className="note" style={{ paddingTop: 0 }}>
              {currentness(report.external_state, report.external_collected_at)}
            </p>
          </div>
          <DeepResearchActions ticker={report.ticker} hasReport />
        </div>

        {viewingOlder && (
          <p className="note viewing-older">
            Viewing an earlier report from {day(report.generated_at)}.{" "}
            <button type="button" className="linklike" onClick={() => setReport(latest)}>
              Back to latest
            </button>
          </p>
        )}

        <div className="overview">
          <Field label="Status" value={title(report.status)} />
          <Field label="Confidence" value={report.confidence.level} />
          <Field label="Generated" value={day(report.generated_at)} />
          <Field label="Explains score" value={report.as_of} />
          <Field label="Score version" value={report.score_version} />
          <Field label="Model" value={report.model_id} />
          <Field label="Sources cited" value={String(report.sources.length)} />
          <Field
            label="Claims citing a filing"
            value={percent(report.confidence.filing_coverage, 0)}
          />
          <Field
            label="Claims citing a source"
            value={percent(report.confidence.external_coverage, 0)}
          />
        </div>

        <p className="note" style={{ paddingTop: 0 }}>
          {report.confidence.rationale}
        </p>

        <div className="legend">
          {Object.entries(BASIS_HELP).map(([basis, help]) => (
            <span key={basis}>
              <span className={`basis ${basis}`}>{basis}</span> {help}
            </span>
          ))}
        </div>

        {report.issues.length > 0 && (
          <p className="note validation-note">
            Some generated claims were removed by validation ({report.issues.length}
            {report.issues.length === 1 ? " issue" : " issues"}:{" "}
            {[...new Set(report.issues.map((issue) => issue.code))].join(", ")}). The removed
            text is not shown.
          </p>
        )}

        {report.sections.map((section) => (
          <Section key={section.key} section={section} sources={report.sources} />
        ))}
      </div>

      <Sources sources={report.sources} />

      <DeepResearchHistory history={history} currentId={report.id} onSelect={setReport} />
    </>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="field">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}
