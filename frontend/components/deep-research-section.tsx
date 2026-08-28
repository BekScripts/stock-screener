import { Claim } from "@/components/deep-research-claim";
import type { DeepSection, DeepSource } from "@/lib/api";

/**
 * One section of the report, and the two ways it can be empty.
 *
 * The distinction this component exists to draw: `NO_EVIDENCE` means the brief
 * offered nothing, which is ordinary and worth stating calmly. `NO_VALID_CLAIMS`
 * means the evidence was there and nothing generated from it survived
 * validation — a limitation of this tool, not of the company, and styling it
 * like missing data would send a reader looking for information the system
 * already had.
 *
 * Neither case shows what was rejected. That text is exactly what validation
 * refused to publish.
 */
export function Section({
  section,
  sources,
}: {
  section: DeepSection;
  sources: DeepSource[];
}) {
  const reason = section.unknown_reason;

  if (reason) {
    return (
      <div className="section deep-section">
        <h3>{section.label}</h3>
        <div className={`empty-section ${reason === "NO_VALID_CLAIMS" ? "warned" : "calm"}`}>
          <span className="empty-mark">{reason === "NO_VALID_CLAIMS" ? "!" : "—"}</span>
          <span>
            {reason === "NO_VALID_CLAIMS"
              ? "Evidence was available, but no generated statement passed validation."
              : "No relevant evidence was available for this section."}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="section deep-section">
      <h3>{section.label}</h3>
      {section.claims.map((claim, index) => (
        <Claim key={`${section.key}-${index}`} claim={claim} sources={sources} />
      ))}
    </div>
  );
}
