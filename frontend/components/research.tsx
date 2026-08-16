import type { ResearchReport } from "@/lib/api";
import { day, percent, title } from "@/lib/format";

const BASIS_HELP: Record<string, string> = {
  DETERMINISTIC: "restates a figure this system calculated",
  EXTRACTED: "read from SEC filing text supplied to the model",
  INTERPRETATION: "the model's reasoning over the cited evidence",
  UNKNOWN: "the evidence does not answer this",
};

/**
 * The validated research report.
 *
 * Every claim wears its basis, because the difference between a figure the
 * pipeline calculated, a sentence quoted from a filing, and the model's own
 * reasoning is the whole point of the phase. Citations are printed rather than
 * hidden — a claim you cannot trace is a claim you should not trust.
 */
export function Research({ report }: { report: ResearchReport }) {
  return (
    <div className="card">
      <h2>AI research</h2>

      <div className="overview" style={{ marginBottom: 16 }}>
        <Field label="Confidence" value={report.confidence.level} />
        <Field label="Status" value={title(report.status)} />
        <Field label="Generated" value={day(report.generated_at)} />
        <Field label="Explains score" value={report.score_date} />
        <Field label="Model" value={report.model_id} />
        <Field label="Claims cite a filing" value={percent(report.confidence.filing_coverage, 0)} />
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

      {report.sections.map((section) => (
        <div className="section" key={section.key}>
          <h3>{section.label}</h3>
          {section.claims.map((claim, index) => (
            <div className="claim" key={`${section.key}-${index}`}>
              <span className={`basis ${claim.basis}`}>{claim.basis}</span>
              <span className="text">{claim.text}</span>
              <span className="cites">{claim.evidence.join(" · ")}</span>
            </div>
          ))}
        </div>
      ))}

      {report.issues.length > 0 && (
        <div className="section">
          <h3>Dropped by validation</h3>
          {report.issues.map((issue, index) => (
            <div className="subscore" key={index}>
              <span>
                {issue.code}
                {issue.section ? ` · ${issue.section}` : ""}
              </span>
              <span className="observed">{issue.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
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
