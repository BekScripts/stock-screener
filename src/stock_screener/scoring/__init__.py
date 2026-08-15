"""Scoring and ranking: stored metrics in, a shortlist out.

Organised by feature, like `scanning`:

- `engine` runs CompounderScore over the stored market and saves a snapshot per
  company per day
- `rankings` reads those snapshots back as the four views — Top Opportunities,
  Hidden Gems, Great Company Wrong Price, Improving Fast
- `report` renders a ranking as a table or a CSV, and one company's score as an
  explanation

The formula itself is not here. It lives in `domain.scoring`, where it is a pure
function of already-calculated metrics and can be tested against hand-worked
numbers without a database.
"""

from stock_screener.scoring.engine import (
    ScoredCompany,
    ScoringRun,
    build_scores,
    load_benchmark_returns,
    score_market,
)
from stock_screener.scoring.enrichment import (
    EnrichmentReport,
    EnrichmentStatus,
    enrich_candidates,
)
from stock_screener.scoring.rankings import (
    DEFAULT_RANKING_LIMIT,
    GreatCompanyCriteria,
    HiddenGemsCriteria,
    RankingRow,
    ScoreDetail,
    great_company_wrong_price,
    hidden_gems,
    improving_fast,
    latest_score,
    top_opportunities,
)
from stock_screener.scoring.report import (
    format_explanation,
    format_rankings_table,
    write_rankings_csv,
)

__all__ = [
    "DEFAULT_RANKING_LIMIT",
    "EnrichmentReport",
    "EnrichmentStatus",
    "GreatCompanyCriteria",
    "HiddenGemsCriteria",
    "RankingRow",
    "ScoreDetail",
    "ScoredCompany",
    "ScoringRun",
    "build_scores",
    "enrich_candidates",
    "format_explanation",
    "format_rankings_table",
    "great_company_wrong_price",
    "hidden_gems",
    "improving_fast",
    "latest_score",
    "load_benchmark_returns",
    "score_market",
    "top_opportunities",
    "write_rankings_csv",
]
