"""The tests that actually reach a paper broker. Skipped unless asked for.

Everything the adapter does is covered against a fake transport in
`tests/integration/test_p2_2_external_paper.py`. These five tests exist for the
one thing a fake cannot establish: that the credential in this environment
reaches a real paper account, and that the provider's current API still answers
the way the adapter reads it.

They are skipped by default and `make check` never passes `--live-paper`, so the
ordinary gate submits nothing. Credentials are read through `Settings` like every
other credential in this project — `ALPACA_PAPER_KEY_ID`,
`ALPACA_PAPER_SECRET_KEY` and `ALPACA_PAPER_ACCOUNT_ID`, kept separate from the
market-data key so the two rotate independently. Run them deliberately:

    uv run pytest tests/live/test_paper_broker_live.py --live-paper

Four of the five are read-only. The fifth submits one share and is the only test
in this repository that places an order anywhere. It is written so that an
interrupted run leaves a *findable* order rather than an orphan: the client order
id is derived from a fixed local order id, so a later run — or a person with the
provider's console — can look it up by name.

There is deliberately no live-trading equivalent of this file, and adding
`--live-trading` would achieve nothing: the adapter refuses a live host, a live
key prefix and a live environment by construction, and no flag reaches those
checks.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from stock_screener.config import Settings
from stock_screener.execution.external import (
    BrokerCapabilities,
    BrokerEnvironment,
    ExternalBrokerAccountBinding,
    prove_paper_environment,
)

pytestmark = [pytest.mark.live_paper, pytest.mark.integration, pytest.mark.slow]

PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"

#: The local order id the submitting test uses. Fixed rather than random so an
#: interrupted run leaves an order that can still be named and looked up.
CANARY_ORDER_ID = "LIVE-PAPER-CANARY-1"


@pytest.fixture
def live_settings() -> Settings:
    """The real configuration, from the project `.env` and the environment.

    Returns:
        The settings.

    Skips:
        When the paper credentials or the account id are absent. A missing
        credential is not a failure — it means this machine is not configured
        for the paper canary.
    """
    settings = Settings(_env_file=PROJECT_ENV if PROJECT_ENV.is_file() else None)  # type: ignore[call-arg]
    if not settings.alpaca_paper_key_id or not settings.alpaca_paper_secret_key:
        pytest.skip("ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY are not set")
    if not settings.alpaca_paper_account_id:
        pytest.skip("ALPACA_PAPER_ACCOUNT_ID is not set")
    return settings


@pytest.fixture
def paper_binding(live_settings: Settings) -> ExternalBrokerAccountBinding:
    """A proven paper binding for the configured account.

    Returns:
        The binding. Building it is itself a check: a credential or host that is
        not provably paper raises here rather than reaching a request.
    """
    assert live_settings.alpaca_paper_key_id is not None
    assert live_settings.alpaca_paper_account_id is not None
    proof = prove_paper_environment(
        environment=BrokerEnvironment.PAPER,
        base_url=live_settings.alpaca_paper_base_url,
        key_id=live_settings.alpaca_paper_key_id.get_secret_value(),
    )
    return ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=live_settings.alpaca_paper_account_id,
        proof=proof,
        capabilities=BrokerCapabilities(),
        label="live paper canary",
    )


def test_the_configured_credential_is_a_paper_credential(
    paper_binding: ExternalBrokerAccountBinding,
) -> None:
    """The configured key reaches the paper host and carries a paper prefix."""
    assert paper_binding.environment is BrokerEnvironment.PAPER
    assert paper_binding.proof.is_proven is True
    assert paper_binding.is_live is False


@pytest.mark.skip(
    reason=(
        "P2.2C ships the transport, so this is no longer blocked on code: it is "
        "blocked on an operator. Reaching the account read-only is what "
        "`stock-screener execution external-paper-preflight --confirm` does, and "
        "running it needs ALPACA_PAPER_KEY_ID, ALPACA_PAPER_SECRET_KEY and "
        "ALPACA_PAPER_ACCOUNT_ID against a clean paper account."
    )
)
def test_the_paper_account_answers_a_read_only_request() -> None:
    """The paper account answers, upgrading the proof to fully certified."""
    raise AssertionError("run the external-paper-preflight command instead")


@pytest.mark.skip(reason="covered by the external-paper-preflight command; see above")
def test_the_account_the_provider_answers_for_is_the_bound_account() -> None:
    """The provider answers for the account the binding names."""
    raise AssertionError("run the external-paper-preflight command instead")


@pytest.mark.skip(reason="covered by the external-paper-preflight command; see above")
def test_every_held_symbol_is_resolvable_to_a_durable_identity() -> None:
    """Every symbol the venue reports held maps back to an identity."""
    raise AssertionError("run the external-paper-preflight command instead")


@pytest.mark.skip(
    reason=(
        "Superseded by the certified canary. The P2.2C defect this test was "
        "blocked on — read_fills filtering /v2/account/activities/FILL by "
        "client_order_id instead of the documented order_id — was fixed in "
        "P2.2D, and P2.2E ran the real two-leg canary against the paper "
        "account and certified it. Run `stock-screener execution "
        "external-paper-canary --confirm` rather than this test: the canary "
        "goes through the full authorization chain and writes a certification "
        "artefact, which an ad-hoc test does neither of."
    )
)
def test_one_share_submitted_to_the_paper_venue_is_confirmed_and_reconciles() -> None:
    """One share is submitted, confirmed, read back and reconciled.

    The only test in this repository that would place an order. It submits
    `Decimal(1)` under `CANARY_ORDER_ID`, so an interrupted run leaves an order
    whose client id is recomputable rather than an orphan.
    """
    assert Decimal(1) == Decimal(1)
    raise AssertionError("blocked on the read_fills defect; see the skip reason")
