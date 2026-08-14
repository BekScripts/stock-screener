import csv
from pathlib import Path

import pytest

from domain import (
    CompanyMetrics,
    CompanyProfile,
    EligibilityResult,
    EligibilityWarning,
    ExclusionReason,
    VolumeBasis,
)
from stock_screener.scanning.report import (
    format_money,
    format_percent,
    format_points,
    format_price,
    format_table,
    write_csv,
)
from stock_screener.scanning.scanner import ScanRow


def _row(*, ticker: str = "XYZ", eligible: bool = True, **metrics: object) -> ScanRow:
    fields: dict[str, object] = {
        "ticker": ticker,
        "price": 25.0,
        "market_cap": 1_200_000_000.0,
        "average_dollar_volume_20d": 12_400_000.0,
        "liquidity_basis": VolumeBasis.CONSOLIDATED,
        "trading_days_used": 20,
        "revenue_growth_yoy": 0.425,
        "revenue_growth_acceleration": 0.13,
        "gross_margin": 0.382,
        "fcf_margin": 0.065,
        "net_cash": 82_000_000.0,
    }
    fields.update(metrics)
    reasons = () if eligible else (ExclusionReason.PRICE_BELOW_MINIMUM,)
    return ScanRow(
        profile=CompanyProfile(ticker=ticker, name=f"{ticker} Inc", exchange="NASDAQ"),
        metrics=CompanyMetrics(**fields),  # type: ignore[arg-type]
        eligibility=EligibilityResult(ticker=ticker, eligible=eligible, reasons=reasons),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_200_000_000.0, "$1.2B"),
        (540_000_000.0, "$540.0M"),
        (12_400.0, "$12.4K"),
        (42.5, "$42.50"),
        (-86_000_000.0, "-$86.0M"),
        (0.0, "$0.00"),
        (None, "-"),
    ],
)
def test_money_is_formatted_with_a_magnitude_suffix(value: float | None, expected: str) -> None:
    assert format_money(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.425, "42.5%"), (-0.082, "-8.2%"), (0.0, "0.0%"), (None, "-")],
)
def test_proportions_are_formatted_as_percentages(value: float | None, expected: str) -> None:
    assert format_percent(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.13, "+13.0pp"), (-0.204, "-20.4pp"), (0.0, "+0.0pp"), (None, "-")],
)
def test_acceleration_is_formatted_as_signed_percentage_points(
    value: float | None, expected: str
) -> None:
    # "+13.0pp" not "13.0%" — the difference between two rates is not a rate,
    # and the specification calls out that confusion by name.
    assert format_points(value) == expected


@pytest.mark.unit
def test_a_missing_price_renders_as_a_dash_not_zero() -> None:
    assert format_price(None) == "-"


@pytest.mark.unit
def test_the_table_has_a_header_and_one_line_per_company() -> None:
    output = format_table([_row(ticker="AAA"), _row(ticker="BBB")])
    lines = output.splitlines()

    assert lines[0].startswith("Ticker")
    assert len(lines) == 4  # header, divider, two companies
    assert "AAA" in lines[2]
    assert "BBB" in lines[3]


@pytest.mark.unit
def test_an_empty_scan_says_so_rather_than_printing_a_bare_header() -> None:
    assert "No companies matched." in format_table([])


@pytest.mark.unit
def test_missing_metrics_render_as_dashes_in_the_table() -> None:
    output = format_table([_row(gross_margin=None, net_cash=None)])

    assert output.splitlines()[2].count("-") >= 2


@pytest.mark.unit
def test_the_csv_has_a_header_and_one_row_per_company(tmp_path: Path) -> None:
    path = tmp_path / "scan.csv"

    written = write_csv([_row(ticker="AAA"), _row(ticker="BBB")], path)

    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert written == 2
    assert [row["ticker"] for row in rows] == ["AAA", "BBB"]


@pytest.mark.unit
def test_the_csv_writes_raw_numbers_not_formatted_strings(tmp_path: Path) -> None:
    # A spreadsheet cannot sort or average "42.5%".
    path = tmp_path / "scan.csv"
    write_csv([_row()], path)

    row = next(iter(csv.DictReader(path.read_text(encoding="utf-8").splitlines())))

    assert float(row["revenue_growth_yoy"]) == pytest.approx(0.425)
    assert float(row["market_cap"]) == pytest.approx(1_200_000_000.0)


@pytest.mark.unit
def test_a_missing_metric_becomes_an_empty_cell_not_a_zero(tmp_path: Path) -> None:
    # A blank is skipped by a spreadsheet average; a zero drags it down, which
    # would corrupt exactly the manual review this export exists for.
    path = tmp_path / "scan.csv"
    write_csv([_row(gross_margin=None, revenue_cagr_3y=None)], path)

    row = next(iter(csv.DictReader(path.read_text(encoding="utf-8").splitlines())))

    assert row["gross_margin"] == ""
    assert row["revenue_cagr_3y"] == ""


@pytest.mark.unit
def test_exclusion_reasons_are_exported_so_the_review_can_see_them(tmp_path: Path) -> None:
    path = tmp_path / "scan.csv"
    write_csv([_row(eligible=False)], path)

    row = next(iter(csv.DictReader(path.read_text(encoding="utf-8").splitlines())))

    assert row["eligible"] == "False"
    assert row["exclusion_reasons"] == "PRICE_BELOW_MINIMUM"


@pytest.mark.unit
def test_the_csv_directory_is_created_if_it_does_not_exist(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "output" / "scan.csv"

    write_csv([_row()], path)

    assert path.is_file()


@pytest.mark.unit
def test_a_consolidated_liquidity_figure_is_printed_plainly() -> None:
    output = format_table([_row()])

    assert output.splitlines()[2].endswith("$12.4M")
    assert "*" not in output


@pytest.mark.unit
def test_a_partial_liquidity_figure_is_marked_and_explained() -> None:
    # Printing single-exchange volume bare invites the reader to compare it
    # against the configured threshold, which is the one comparison that is
    # invalid — it understates real volume roughly twenty-five fold.
    output = format_table([_row(liquidity_basis=VolumeBasis.PARTIAL)])

    assert "$12.4M*" in output
    assert "single-exchange volume only" in output
    assert "threshold, which was not applied" in output


@pytest.mark.unit
def test_the_csv_records_the_liquidity_basis_and_any_warnings(tmp_path: Path) -> None:
    # A spreadsheet must be able to filter on the basis rather than trusting
    # the number beside it.
    path = tmp_path / "scan.csv"
    row = _row(liquidity_basis=VolumeBasis.PARTIAL)
    row = ScanRow(
        profile=row.profile,
        metrics=row.metrics,
        eligibility=EligibilityResult(
            ticker="XYZ",
            eligible=True,
            warnings=(EligibilityWarning.LIQUIDITY_UNVERIFIED,),
        ),
    )
    write_csv([row], path)

    record = next(iter(csv.DictReader(path.read_text(encoding="utf-8").splitlines())))

    assert record["liquidity_basis"] == "PARTIAL"
    assert record["warnings"] == "LIQUIDITY_UNVERIFIED"
