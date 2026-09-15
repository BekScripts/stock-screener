"""The live Backblaze canary — the one test that makes real requests.

Skipped unless pytest is given `--live-b2`. `make check` never passes it, so
the default suite makes zero Backblaze requests and needs no credential.

When it runs, it reads the project's own `.env` (the test fixture that
isolates every other test from it is deliberately bypassed here — a live
canary is *about* the real configuration), requires `SEC_STORAGE_BACKEND=b2`,
and drives `run_live_canary`: production wiring, a temporary control root,
every contract operation under `_canary/<run-id>/`, one harvest, a replay, a
reconciliation, and cleanup. It writes about two megabytes and removes them.

Run it deliberately:

    uv run pytest tests/live --live-b2 -p no:randomly -s

Or, equivalently, the command: `stock-screener evidence b2-canary --confirm`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_screener.config import Settings
from stock_screener.sec_evidence import StorageBackend, run_live_canary, storage_backend

pytestmark = [pytest.mark.live_b2, pytest.mark.slow]

PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"


@pytest.fixture
def live_settings() -> Settings:
    """The real configuration, from the project `.env` and the environment."""
    settings = Settings(_env_file=PROJECT_ENV if PROJECT_ENV.is_file() else None)  # type: ignore[call-arg]
    if storage_backend(settings) is not StorageBackend.B2:
        pytest.skip("SEC_STORAGE_BACKEND is not b2; the live canary has nothing to talk to")
    return settings


def test_the_live_canary_passes_and_leaves_nothing_behind(
    live_settings: Settings, tmp_path: Path
) -> None:
    control_dir = tmp_path / "canary-control"
    control_dir.mkdir()

    report = run_live_canary(live_settings, control_dir=control_dir, cleanup=True)

    summary = "\n".join(report.summary())
    (tmp_path / "canary-report.json").write_text(
        json.dumps(
            {
                "run_id": report.run_id,
                "prefix": report.prefix,
                "passed": report.passed,
                "steps": [
                    {"name": s.name, "ok": s.ok, "elapsed_ms": s.elapsed_ms, "detail": s.detail}
                    for s in report.steps
                ],
                "operations": report.operations,
                "requests": report.requests,
                "objects": report.objects,
                "cleanup": report.cleanup,
                "remaining": report.remaining,
                "failure": report.failure,
            },
            indent=2,
        )
    )
    print("\n" + summary)  # noqa: T201 - the live report is the point of this test
    print(f"report written to {tmp_path / 'canary-report.json'}")  # noqa: T201

    secret = live_settings.b2_application_key
    key_id = live_settings.b2_key_id
    assert secret is not None and key_id is not None
    assert secret.get_secret_value() not in summary
    assert key_id.get_secret_value() not in summary
    assert report.passed, report.failure
    assert report.remaining == [], report.cleanup
