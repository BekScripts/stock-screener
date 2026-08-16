import Link from "next/link";
import { notFound } from "next/navigation";

import { CategoryBadge, RiskBadge, ScoreBadge, StateBadge } from "@/components/badges";
import { Research } from "@/components/research";
import { ResearchButton } from "@/components/research-button";
import { WatchButton } from "@/components/watch-button";
import { fetchResearch, fetchStock, type Component } from "@/lib/api";
import { change, metricValue, money, percent, score, title } from "@/lib/format";

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
            <RiskBadge level={breakdown?.risk.level ?? null} labelled />
            <StateBadge state={detail.ranking_state} />
          </div>
        </div>
        <WatchButton ticker={detail.ticker} initial={detail.watched} />
      </div>

      <div className="card">
        <h2>Overview</h2>
        <div className="overview">
          <Field label="Market cap" value={money(detail.market_cap)} />
          <Field label="Market cap source" value={title(detail.market_cap_source ?? "UNKNOWN")} />
          <Field label="Exchange" value={detail.exchange ?? "—"} />
          <Field label="Score date" value={detail.score?.score_date ?? "—"} />
          <Field label="Score version" value={detail.score?.score_version ?? "—"} />
          <Field label="Change (30d)" value={change(detail.score?.score_change_30d)} />
        </div>
      </div>

      {breakdown ? (
        <div className="card">
          <h2>
            CompounderScore {score(breakdown.final_score)} · raw {score(breakdown.raw_score)} · risk{" "}
            {score(breakdown.risk.total_penalty)} · coverage {percent(breakdown.data_coverage, 0)}
          </h2>
          <div className="components">
            <ComponentCard component={breakdown.growth} label="Growth" />
            <ComponentCard component={breakdown.quality} label="Financial quality" />
            <ComponentCard component={breakdown.valuation} label="Valuation" />
            <ComponentCard component={breakdown.momentum} label="Market confirmation" />
          </div>
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
        </div>
      )}

      <div className="card">
        <h2>Metrics</h2>
        <div className="metrics">
          {detail.metrics.map((metric) => (
            <div className="metric" key={metric.key}>
              <span>{metric.label}</span>
              <span className="value" data-unknown={metric.value === null}>
                {metricValue(metric.value, metric.unit)}
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

function ComponentCard({ component, label }: { component: Component; label: string }) {
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
