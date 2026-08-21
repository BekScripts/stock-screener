/**
 * Formatting rules, in one place.
 *
 * The rule that matters: **missing is not zero.** A metric the data cannot
 * support renders as an explicit dash and never as 0.0%, because the difference
 * between "the company reported nothing" and "the company reported zero" is the
 * difference between a gap and a fact.
 */

export const UNKNOWN = "—";

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return UNKNOWN;
  return `${(value * 100).toFixed(digits)}%`;
}

export function points(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return UNKNOWN;
  const scaled = value * 100;
  return `${scaled >= 0 ? "+" : ""}${scaled.toFixed(digits)}pp`;
}

/**
 * Format an amount of money, in the currency it is actually in.
 *
 * A dollar sign is not decoration. A foreign issuer's net cash is in the
 * currency it files in, so rendering TSM's NT$1.9tn as "$1.9T" would overstate
 * it by a factor of thirty-two while looking entirely ordinary. Anything other
 * than dollars is therefore printed with its ISO code rather than a symbol —
 * "NT$" and "kr" are ambiguous across several currencies each, and a code that
 * the reader has to look up is better than a symbol that misleads them.
 */
export function money(value: number | null | undefined, currency = "USD"): string {
  if (value === null || value === undefined) return UNKNOWN;
  const sign = value < 0 ? "-" : "";
  const size = Math.abs(value);
  const unit = currency === "USD" ? "$" : `${currency} `;
  if (size >= 1e12) return `${sign}${unit}${(size / 1e12).toFixed(2)}T`;
  if (size >= 1e9) return `${sign}${unit}${(size / 1e9).toFixed(2)}B`;
  if (size >= 1e6) return `${sign}${unit}${(size / 1e6).toFixed(1)}M`;
  return `${sign}${unit}${size.toFixed(0)}`;
}

export function score(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return UNKNOWN;
  return value.toFixed(digits);
}

export function change(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return UNKNOWN;
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function metricValue(value: number | null, unit: string, currency = "USD"): string {
  if (unit === "money") return money(value, currency);
  if (unit === "points") return points(value);
  return percent(value);
}

export function title(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function day(value: string): string {
  return value.slice(0, 10);
}
