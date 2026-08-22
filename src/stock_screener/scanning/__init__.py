"""The scan pipeline: ingest provider data, calculate, screen, report.

Organised by feature rather than by technical layer, so the whole Phase 1
workflow is one directory:

- `ingestion` pulls universe, prices, fundamentals and the filing index into
  the database
- `scanner` turns stored rows into metrics and an eligibility verdict
- `report` renders the result as a console table or a CSV export

Nothing here constructs a provider or a database session. Both are passed in, by
`cli.py` in production and by fixtures in the tests.
"""

from stock_screener.scanning.ingestion import (
    IngestionReport,
    select_text_filings,
    update_benchmark,
    update_eligibility_volume,
    update_filing_text,
    update_filings,
    update_fundamentals,
    update_market_data,
    update_universe,
)
from stock_screener.scanning.report import format_table, write_csv
from stock_screener.scanning.scanner import ScanResult, ScanRow, scan_market

__all__ = [
    "IngestionReport",
    "ScanResult",
    "ScanRow",
    "format_table",
    "scan_market",
    "select_text_filings",
    "update_benchmark",
    "update_eligibility_volume",
    "update_filing_text",
    "update_filings",
    "update_fundamentals",
    "update_market_data",
    "update_universe",
    "write_csv",
]
