import type { DeepClaim, DeepSource } from "@/lib/api";
import { day } from "@/lib/format";

/** What each basis means, in the reader's terms rather than the contract's. */
export const BASIS_HELP: Record<string, string> = {
  DETERMINISTIC: "a figure Compounder Radar calculated",
  EXTRACTED: "read from SEC filing text",
  EXTERNAL: "reported by a named current source",
  INTERPRETATION: "reasoning over the cited evidence",
  UNKNOWN: "no validated statement",
};

const FILING_SECTIONS: Record<string, string> = {
  business: "Business",
  risk_factors: "Risk Factors",
  mda: "MD&A",
};

/**
 * One citation, rendered so a person does not have to decode it.
 *
 * `X.0000934549-26-000034.mda` tells a reader nothing; "SEC · MD&A" tells them
 * where it came from. Only what the id itself encodes is shown — the accession
 * and the section slug — because inventing a form type or a filing date the API
 * did not return would be fabricating provenance on a page whose whole purpose
 * is provenance.
 *
 * A `W.` citation resolves against the report's own sources, which is why the
 * API carries them: the id alone is a handle, and the publisher and date beside
 * it are what make the claim checkable.
 */
export function Citation({ id, sources }: { id: string; sources: DeepSource[] }) {
  if (id.startsWith("W.")) {
    const source = sources.find((candidate) => candidate.evidence_id === id);
    if (!source) {
      return <span className="cite cite-external">{id}</span>;
    }
    return (
      <a
        className="cite cite-external"
        href={source.url}
        target="_blank"
        rel="noreferrer"
        title={source.title}
      >
        {source.publisher}
        {source.published_at ? ` · ${day(source.published_at)}` : ""}
      </a>
    );
  }

  if (id.startsWith("X.")) {
    const section = id.split(".").slice(2).join(".");
    const label = FILING_SECTIONS[section] ?? section.replace(/_/g, " ");
    return (
      <span className="cite cite-filing" title={id}>
        SEC · {label}
      </span>
    );
  }

  if (id.startsWith("D.")) {
    return (
      <span className="cite cite-filing" title={id}>
        SEC filing
      </span>
    );
  }

  return (
    <span className="cite cite-deterministic" title={id}>
      {id}
    </span>
  );
}

/**
 * One claim, wearing its basis and its evidence.
 *
 * The basis label is printed as text, not signalled by colour alone: the
 * difference between a calculated figure, a quoted filing and the model's
 * reasoning is the point of the report, and it has to survive being read in
 * greyscale.
 */
export function Claim({ claim, sources }: { claim: DeepClaim; sources: DeepSource[] }) {
  return (
    <div className="claim">
      <span className={`basis ${claim.basis}`} title={BASIS_HELP[claim.basis]}>
        {claim.basis}
      </span>
      <span className="text">{claim.text}</span>
      {claim.evidence.length > 0 && (
        <span className="cites">
          {claim.evidence.map((id) => (
            <Citation key={id} id={id} sources={sources} />
          ))}
        </span>
      )}
    </div>
  );
}
