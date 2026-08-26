import Link from "next/link";
import { notFound } from "next/navigation";

import { CategoryBadge, RiskBadge, ScoreBadge, StateBadge } from "@/components/badges";
import { Research } from "@/components/research";
import { ResearchButton } from "@/components/research-button";
import { WatchButton } from "@/components/watch-button";
import { fetchResearch, fetchStock, type Component, type StockDetail } from "@/lib/api";
import { UNKNOWN, change, metricValue, money, percent, score, title } from "@/lib/format";

export default async function StockPage({ params }: { params: Promise<{ ticker: string }> }) {
  const { ticker } = await params;

  const detail = await fetchStock(ticker).catch(() => null);
  if (!detail) notFound();

  const report = detail.has_research ? await fetchResearch(detail.ticker) : null;
  const breakdown = detail.score?.breakdown;

  return (
    <>
      <Link className="back" href="/">
        ← Rankings
      </Link>

      <div className="detail-head">
        <div>
          <h1>
            {detail.ticker} · {detail.name}
          </h1>
          {/* The registrant's own SIC description from EDGAR, which is a filing
              classification rather than a judgement about what the business does
              today — a diversified acquirer keeps whatever code it first
              registered under. Attributed so it is not read as ours. */}
          <div className="sub">
            {detail.industry ?? detail.sector
              ? `SEC industry (SIC): ${detail.industry ?? detail.sector}`
              : "SEC industry (SIC) unknown"}
          </div>
          <div className="chips">
            <ScoreBadge value={detail.score?.final_score ?? null} />
            <CategoryBadge value={breakdown?.category ?? null} />
            <RiskBadge level={breakdown?.risk?.level ?? null} labelled />
            <StateBadge state={detail.ranking_state} />
          </div>
        </div>
        <WatchButton ticker={detail.ticker} initial={detail.watched} />
      </div>

      <div className="card">
        <h2>Overview</h2>
        <div className="overview">
          <Field
            label="Market cap"
            value={money(detail.market_cap, detail.market_cap_currency)}
          />
          <Field label="Market cap source" value={title(detail.market_cap_source ?? "UNKNOWN")} />
          <Field label="Exchange" value={detail.exchange ?? "—"} />
          <Field label="Score date" value={detail.score?.score_date ?? "—"} />
          <Field label="Score version" value={detail.score?.score_version ?? "—"} />
          <Field label="Fundamentals basis" value={fundamentalsBasis(detail)} />
          {detail.reporting_currency !== detail.market_cap_currency && (
            <Field
              label="Reports in"
              value={
                detail.fx
                  ? `${detail.reporting_currency} · ${detail.fx.base}/${detail.fx.quote} ${detail.fx.rate.toFixed(4)} on ${detail.fx.rate_date} (${detail.fx.provider})`
                  : `${detail.reporting_currency} · no exchange rate available`
              }
            />
          )}
          <Field label="Change (30d)" value={change(detail.score?.score_change_30d)} />
        </div>
      </div>

      {breakdown ? (
        <div className="card">
          <h2>
            CompounderScore {score(breakdown.final_score)} · raw {score(breakdown.raw_score)} · risk{" "}
            {score(breakdown.risk?.total_penalty)} · coverage {percent(breakdown.data_coverage, 0)}
          </h2>
          <div className="components">
            <ComponentCard component={breakdown.growth} label="Growth" />
            <ComponentCard component={breakdown.quality} label="Financial quality" />
            <ComponentCard component={breakdown.valuation} label="Valuation" />
            <ComponentCard component={breakdown.momentum} label="Market confirmation" />
          </div>
          {detail.score?.rank_eligible === false && (
            <p className="note" data-stale="true">
              This score is based on stale fundamentals and is excluded from current rankings.
            </p>
          )}
          {breakdown.warnings.length > 0 && (
            <p className="note">Warnings: {breakdown.warnings.join(", ")}</p>
          )}
        </div>
      ) : (
        <div className="card">
          <h2>CompounderScore</h2>
          <p className="empty">
            This company has no score under the current version. That is the answer to why it is
            missing from every ranking.
          </p>
          {detail.score && <p className="note">{whyUnscored(detail.score)}</p>}
        </div>
      )}

      <div className="card">
        <h2>Metrics</h2>
        <div className="metrics">
          {detail.metrics.map((metric) => (
            <div className="metric" key={metric.key}>
              <span>{metric.label}</span>
              <span className="value" data-unknown={metric.value === null}>
                {metricValue(metric.value, metric.unit, metric.currency ?? "USD")}
              </span>
            </div>
          ))}
        </div>
      </div>

      {report ? (
        <>
          <Research report={report} />
          <div className="card">
            <ResearchButton ticker={detail.ticker} hasReport />
            <p className="muted">
              A re-run reuses the stored report unless the evidence, the scoring rules or the
              prompt have changed, so pressing this again usually costs nothing.
            </p>
          </div>
        </>
      ) : (
        <div className="card">
          <h2>AI research</h2>
          <p className="empty">
            No research stored for {detail.ticker}.
            {detail.score ? "" : " A company without a score has no brief to research from."}
          </p>
          <ResearchButton ticker={detail.ticker} hasReport={false} />
        </div>
      )}
    </>
  );
}

/**
 * One component of the score.
 *
 * Null when the company could not be scored at all — a bank, an ineligible
 * listing, a company with two quarters of history. It renders as explicitly
 * unscored rather than as a zero-filled bar, because a bar at zero reads as
 * "scored badly" and the truth is "not scored", which are different answers to
 * different questions.
 */
function ComponentCard({ component, label }: { component: Component | null; label: string }) {
  if (component === null) {
    return (
      <div className="component">
        <div className="head">
          <span className="name">{label}</span>
          <span className="points">—</span>
        </div>
        <div className="bar" />
        <p className="note component-empty">Not scored.</p>
      </div>
    );
  }

  const filled = component.score !== null ? (component.score / component.max_points) * 100 : 0;
  return (
    <div className="component">
      <div className="head">
        <span className="name">{label}</span>
        <span className="points">
          {score(component.score)} / {component.max_points}
        </span>
      </div>
      <div className="bar">
        <span style={{ width: `${Math.max(0, Math.min(100, filled))}%` }} />
      </div>
      {component.subscores.map((sub) => (
        <div className="subscore" key={sub.name}>
          <span>{title(sub.name)}</span>
          <span className="observed">
            {score(sub.points)}/{sub.max_points} · {formatObserved(sub.observed, sub.unit)}
          </span>
        </div>
      ))}
    </div>
  );
}

function formatObserved(observed: number | null, unit: string | null): string {
  if (observed === null) return "—";
  if (unit === "PERCENT") return percent(observed);
  if (unit === "POINTS") return `${(observed * 100).toFixed(1)}pp`;
  if (unit === "MULTIPLE") return `${observed.toFixed(2)}x`;
  if (unit === "MONEY") return money(observed);
  return observed.toFixed(2);
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="field">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

/**
 * Why a company has no score, in the words a reader needs.
 *
 * `NOT_ELIGIBLE` on its own is the wall of dashes: a company reporting in
 * Taiwan dollars, a fund, a delisted shell and a stock that trades a hundred
 * dollars a day all arrive under it and are four different things. The stored
 * exclusion reasons are what tell them apart, so they are spelled out rather
 * than shown as enum names.
 */
const EXCLUSION_TEXT: Record<string, string> = {
  UNSUPPORTED_CURRENCY:
    "Reports its financial statements in a currency other than US dollars, so its figures cannot be compared with a dollar market capitalisation. Fundamentals are not yet normalised.",
  UNSUPPORTED_SECURITY_TYPE:
    "Not common stock on NASDAQ, NYSE or NYSE American — funds, preferred shares and other listings are outside the screen.",
  MISSING_REQUIRED_DATA:
    "No price or market capitalisation was available, so the company could not be screened at all.",
  MARKET_CAP_BELOW_MINIMUM: "Market capitalisation is below the configured minimum.",
  PRICE_BELOW_MINIMUM: "Share price is below the configured minimum.",
  LOW_LIQUIDITY: "Trades too thinly, or has too little price history to judge liquidity.",
  INACTIVE: "The security is no longer trading.",
};

function whyUnscored(score: NonNullable<StockDetail["score"]>): string {
  if (score.scoring_status === "INSUFFICIENT_DATA") {
    return "Not enough reported history to score a component honestly. A missing metric earns neither zero points nor full marks.";
  }
  if (score.scoring_status === "UNSUPPORTED_SECTOR") {
    return "A business whose economics this model misreads — banks, insurers and similar are kept in the universe and out of the ranking.";
  }
  if (score.exclusion_reasons.length > 0) {
    return score.exclusion_reasons
      .map((reason) => EXCLUSION_TEXT[reason] ?? title(reason))
      .join(" ");
  }
  return "No reason was recorded for this row. It predates exclusion reasons being stored; the next scoring run will fill it in.";
}

/**
 * How this company's fundamentals were measured, in one line.
 *
 * A quarterly filer and an annual one both have "revenue growth", and the
 * phrase means a different span of time for each. Saying which prevents a
 * reader taking TSM's figures — a fiscal year ending last December — for
 * current-quarter ones.
 */
const CADENCE_LABEL: Record<string, string> = {
  QUARTERLY: "Quarterly",
  SEMIANNUAL: "Half-yearly",
  ANNUAL: "Annual",
};

function fundamentalsBasis(detail: StockDetail): string {
  const cadence = CADENCE_LABEL[detail.fundamental_cadence];
  if (!cadence) return UNKNOWN;
  if (!detail.fundamentals_through) return cadence;
  const through = new Date(detail.fundamentals_through).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  const basis = `${cadence} · through ${through}`;
  return detail.fundamentals_stale ? `${basis} · stale` : basis;
}
