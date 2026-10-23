"""Paginating the security universe into chunks that tile it exactly.

Phase 9K.18 found `--limit 250` returning 100 securities: the limit was being
applied to the frozen validation batch, which the quotas cap at a hundred, so a
run looked successful while covering 60% less ground than asked for. And nothing
could reach the 101st security at all — there was no offset.

The tests that matter here are the tiling ones. A harvest that runs in chunks is
only trustworthy if the chunks cover the universe once each: any gap loses
securities silently, and any overlap re-fetches them. `TestTheChunksTileTheUniverse`
is the guarantee.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import HistoricalSecurity
from domain import CompanyProfile
from stock_screener.sec_evidence import (
    controlled_sample,
    production_universe,
    select_universe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.sec_evidence import Selection

pytestmark = pytest.mark.integration

#: Enough securities to slice several times over.
LIVE_COUNT = 400
DELISTED_COUNT = 220


def _short(index: int) -> str:
    """A distinct ticker of four characters or fewer."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return f"{letters[index // 26 % 26]}{letters[index % 26]}{index % 10}"


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A populated database with a universe larger than two chunks."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'universe.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    with session_scope(maker) as session:
        # Half short tickers, half long: the controlled sample buckets on ticker
        # length, and a pool that cannot fill a bucket makes its quota a ceiling
        # rather than a count — which would make the 100-security assertion below
        # test the fixture instead of the code.
        for i in range(LIVE_COUNT // 2):
            CompanyRepository(session).upsert_profile(
                CompanyProfile(ticker=_short(i), name=f"Short {i}", exchange="NASDAQ")
            )
        for i in range(LIVE_COUNT // 2):
            CompanyRepository(session).upsert_profile(
                CompanyProfile(ticker=f"LONG{i:04d}", name=f"Long {i}", exchange="NASDAQ")
            )
        for i in range(DELISTED_COUNT):
            session.add(
                HistoricalSecurity(
                    primary_ticker=f"D{i:04d}",
                    name=f"Delisted {i}",
                    cik=(1_000_000 + i) if i % 2 == 0 else None,
                    status="UNKNOWN",
                )
            )
        session.flush()
    try:
        yield maker
    finally:
        engine.dispose()


def identities(rows: Sequence[Selection]) -> list[tuple[str, int | None, int | None]]:
    """The identity of each selection, order preserved."""
    return [(r.symbol, r.company_id, r.historical_security_id) for r in rows]


class TestTheChunksTileTheUniverse:
    """The guarantee a chunked harvest rests on: cover everything, exactly once."""

    def test_every_security_appears_in_exactly_one_chunk(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            universe = production_universe(session)
            chunks = [
                select_universe(session, production=True, offset=start, limit=250)
                for start in range(0, len(universe) + 250, 250)
            ]

            seen: list[tuple[str, int | None, int | None]] = []
            for chunk in chunks:
                seen.extend(identities(chunk))

            assert len(seen) == len(universe)
            assert len(set(seen)) == len(universe)

    def test_the_chunks_reconstruct_the_universe_in_order(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            universe = production_universe(session)
            rebuilt: list[Selection] = []
            for start in range(0, len(universe), 250):
                rebuilt.extend(select_universe(session, production=True, offset=start, limit=250))

            assert identities(rebuilt) == identities(universe)

    def test_no_two_chunks_intersect(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            chunks = [
                set(identities(select_universe(session, production=True, offset=s, limit=250)))
                for s in (0, 250, 500)
            ]

            for i, first in enumerate(chunks):
                for second in chunks[i + 1 :]:
                    assert first & second == set()


class TestChunkBoundaries:
    def test_the_first_chunk_is_the_requested_size(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            assert len(select_universe(session, production=True, offset=0, limit=250)) == 250

    def test_the_second_chunk_continues_from_the_first(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            universe = production_universe(session)

            second = select_universe(session, production=True, offset=250, limit=250)

            assert identities(second) == identities(universe[250:500])

    def test_the_final_chunk_holds_only_the_remainder(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            total = len(production_universe(session))
            last_start = (total // 250) * 250

            final = select_universe(session, production=True, offset=last_start, limit=250)

            assert len(final) == total - last_start
            assert len(final) < 250

    def test_an_offset_past_the_end_is_empty_rather_than_an_error(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            assert select_universe(session, production=True, offset=10_000, limit=250) == ()

    def test_no_limit_takes_the_remainder(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            total = len(production_universe(session))

            rest = select_universe(session, production=True, offset=100)

            assert len(rest) == total - 100

    def test_a_negative_offset_is_refused(self, factory: sessionmaker[Session]) -> None:
        with (
            session_scope(factory) as session,
            pytest.raises(ValueError, match="must not be negative"),
        ):
            select_universe(session, production=True, offset=-1, limit=10)


class TestOrderingIsDeterministic:
    def test_the_same_slice_repeats_exactly(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            first = select_universe(session, production=True, offset=250, limit=250)
            second = select_universe(session, production=True, offset=250, limit=250)

            assert identities(first) == identities(second)

    def test_a_shared_symbol_is_ordered_by_kind_then_id(self, tmp_path: Path) -> None:
        """138 real symbols exist as both a live row and a delisted one."""
        engine = create_engine_from_url(f"sqlite:///{tmp_path / 'shared.db'}")
        create_all(engine)
        with session_scope(build_session_factory(engine)) as session:
            CompanyRepository(session).upsert_profile(
                CompanyProfile(ticker="STRR", name="Star Equity", exchange="NASDAQ")
            )
            session.add(
                HistoricalSecurity(
                    primary_ticker="STRR", name="Star Operating", cik=707388, status="UNKNOWN"
                )
            )
            session.flush()

            universe = production_universe(session)

        engine.dispose()
        assert [s.bucket for s in universe] == ["DELISTED", "LIVE"]
        assert len({identities(universe)[0], identities(universe)[1]}) == 2

    def test_the_universe_holds_every_security(self, factory: sessionmaker[Session]) -> None:
        """Unresolvable securities stay in the population — see the module docstring."""
        with session_scope(factory) as session:
            universe = production_universe(session)

            assert len(universe) == LIVE_COUNT + DELISTED_COUNT
            assert sum(1 for s in universe if s.cik is None) == LIVE_COUNT + DELISTED_COUNT // 2


class TestTheValidationBatchIsUntouched:
    def test_the_controlled_sample_stays_capped_at_its_quotas(
        self, factory: sessionmaker[Session]
    ) -> None:
        """It is the record of phases 9K.13-9K.18 and must not move."""
        with session_scope(factory) as session:
            assert len(controlled_sample(session)) == 100

    def test_a_larger_limit_cannot_stretch_the_validation_batch(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The 9K.18 defect: --limit 250 silently returned 100."""
        with session_scope(factory) as session:
            assert len(select_universe(session, production=False, offset=0, limit=250)) == 100

    def test_the_production_path_does_not_degrade_to_the_sample(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            assert len(select_universe(session, production=True, offset=0, limit=250)) == 250

    def test_the_sample_and_the_universe_are_different_populations(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            sample = identities(controlled_sample(session))
            universe = identities(production_universe(session))

            assert len(universe) > len(sample)
            assert set(sample) <= set(universe)
