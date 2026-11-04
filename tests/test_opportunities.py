"""Tests for v8_next.opportunities: ExposureResolver, OpportunityBook, Models & Lifecycle."""

from decimal import Decimal

import pytest

from v8_next.adapters.expert_strategy import (
    ExpertStrategyConfig,
    run_expert_strategy_backtest,
)
from v8_next.domain.market import Candle
from v8_next.opportunities.book import OpportunityBook
from v8_next.opportunities.exposure import (
    ExposureResolver,
    UnresolvedExposureError,
)
from v8_next.opportunities.models import (
    EconomicExposureStructure,
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)

HOUR_NS = 3600 * 10**9


def make_test_candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
) -> Candle:
    return Candle(
        instrument_id,
        i * HOUR_NS,
        (i + 1) * HOUR_NS,
        Decimal(str(open_)),
        Decimal(str(high)),
        Decimal(str(low)),
        Decimal(str(close)),
        Decimal(str(volume)),
        (i + 1) * HOUR_NS,
        (i + 1) * HOUR_NS,
        "test-opp-fixture",
    )


def test_exposure_resolver_exact_alias_collapse():
    """Aliases for the same factor must resolve to identical factor dimensions."""
    resolver = ExposureResolver()

    exp_primary = resolver.resolve_ticker("BTCUSDT", "binance-um", ExposureDirection.LONG)
    exp_alias1 = resolver.resolve_ticker("BTC_ALIAS1", "binance-um", ExposureDirection.LONG)
    exp_alias2 = resolver.resolve_ticker("BTC_ALIAS2", "binance-um", ExposureDirection.LONG)

    assert exp_primary.underlying_factors == ("BTC",)
    assert exp_alias1.underlying_factors == ("BTC",)
    assert exp_alias2.underlying_factors == ("BTC",)
    assert len(exp_primary.exposure_id) == 64


def test_exposure_resolver_basis_spread_protection():
    """Rule 26: Basis spread must retain multi-leg identity and never collapse to scalar zero."""
    resolver = ExposureResolver()
    basis = resolver.resolve_basis_spread(
        "BTCUSDT", "binance-spot", "BTCUSDT", "binance-um"
    )

    assert basis.is_basis_or_spread is True
    assert basis.direction == ExposureDirection.NEUTRAL
    assert len(basis.legs) == 2
    assert basis.gross_leg_weight == 2.0
    assert basis.underlying_factors == ("BTC", "BTC_BASIS")


def test_exposure_resolver_unregistered_fails_closed():
    """Unregistered tickers fail closed with UnresolvedExposureError."""
    resolver = ExposureResolver()
    with pytest.raises(UnresolvedExposureError) as exc_info:
        resolver.resolve_ticker("NONEXISTENT", "unknown-venue", ExposureDirection.LONG)
    assert exc_info.value.symbol == "NONEXISTENT"
    assert exc_info.value.venue == "unknown-venue"


def test_opportunity_book_lifecycle_and_invalidation():
    """Opportunities in the book transition deterministically on stop breach or TTL expiry."""
    book = OpportunityBook()
    exp = EconomicExposureStructure.single_perp(
        "BTCUSDT", "BTC", "binance-um", "USDT", ExposureDirection.LONG
    )

    # Create LONG opportunity with entry=100, stop=95, target=110, valid until 5 hours
    opp = OpportunityRecord.create(
        exposure=exp,
        instrument_id="BTCUSDT-PERP.BINANCE",
        direction=ExposureDirection.LONG,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        as_of_time_ns=HOUR_NS,
        valid_until_ns=5 * HOUR_NS,
    )
    book.insert(opp)

    assert len(book) == 1
    assert book.get(opp.opportunity_id) is not None
    assert len(book.active_opportunities(2 * HOUR_NS)) == 1

    # Bar 2: price stays safe (low=98 > 95)
    c2 = make_test_candle(2, 100, 102, 98, 101)
    transitioned = book.on_candle(c2)
    assert len(transitioned) == 0
    assert book.get(opp.opportunity_id).status == OpportunityStatus.CANDIDATE

    # Bar 3: price breaches stop (low=94 <= 95) -> INVALIDATED
    c3 = make_test_candle(3, 101, 102, 94, 95)
    transitioned = book.on_candle(c3)
    assert len(transitioned) == 1
    assert transitioned[0].opportunity_id == opp.opportunity_id
    assert transitioned[0].status == OpportunityStatus.INVALIDATED
    assert len(book.active_opportunities()) == 0


def test_opportunity_book_ttl_expiration():
    """Opportunities expire after valid_until_ns has elapsed."""
    book = OpportunityBook()
    exp = EconomicExposureStructure.single_perp(
        "ETHUSDT", "ETH", "binance-um", "USDT", ExposureDirection.SHORT
    )

    # Valid until bar 3
    opp = OpportunityRecord.create(
        exposure=exp,
        instrument_id="ETHUSDT-PERP.BINANCE",
        direction=ExposureDirection.SHORT,
        entry_price=Decimal("3000"),
        stop_price=Decimal("3100"),
        as_of_time_ns=HOUR_NS,
        valid_until_ns=3 * HOUR_NS,
    )
    book.insert(opp)

    # Bar 2: safe
    c2 = make_test_candle(2, 2990, 3010, 2980, 3000, instrument_id="ETHUSDT-PERP.BINANCE")
    assert len(book.on_candle(c2)) == 0

    # Bar 4: end_ns = 4 * HOUR_NS > 3 * HOUR_NS -> EXPIRED
    c4 = make_test_candle(4, 2990, 3010, 2980, 3000, instrument_id="ETHUSDT-PERP.BINANCE")
    transitioned = book.on_candle(c4)
    assert len(transitioned) == 1
    assert transitioned[0].status == OpportunityStatus.EXPIRED


def test_expert_strategy_admission_wiring_synthetic():
    """MECHANICS ONLY (synthetic): book CONFIRMED/ADMITTED wiring on order submit.

    Proves plumbing, carries zero evaluative weight. Real firing behavior is
    measured on the BurnedDiagnosticReal population below.
    """
    # Build 48 flat bars, then bar 49 breakout
    candles_list = [make_test_candle(i, 100, 100.5, 99.5, 100) for i in range(48)]
    candles_list.append(make_test_candle(48, 101, 122, 100, 120, volume=500.0))
    candles_list.append(make_test_candle(49, 120, 125, 119, 123, volume=200.0))
    candles = tuple(candles_list)

    config = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        order_quantity=Decimal("0.010"),
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.05"),
    )

    result = run_expert_strategy_backtest(candles, config)
    book: OpportunityBook = result["opportunity_book"]

    assert len(book) >= 1
    admitted = book.active_opportunities(statuses=(OpportunityStatus.ADMITTED,))
    assert len(admitted) >= 1
    admitted_opp = admitted[0]
    assert admitted_opp.direction == ExposureDirection.LONG
    assert admitted_opp.entry_price == Decimal("120")
    assert admitted_opp.stop_price is not None
    assert admitted_opp.target_price is not None


def test_expert_strategy_real_tape_firing_telemetry():
    """EVALUATIVE (real tape): firing telemetry on BurnedDiagnosticReal.

    Full D-153 rule: opportunity/decision/trade counts are measured on the
    real population, never on synthetic candles. Counts are structural
    (non-empty pipeline, valid geometry); exact numbers are tape-dependent
    and must NOT be pinned here.
    """
    from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles

    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"Real tape not found at {DEFAULT_TAPE_PATH}")
    candles = tuple(load_tape_candles(DEFAULT_TAPE_PATH, limit=2000))

    config = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )
    result = run_expert_strategy_backtest(candles, config)
    book: OpportunityBook = result["opportunity_book"]
    decisions = result["decisions"]

    # One decision per bar: pipeline is live end to end
    assert len(decisions) == len(candles)
    # Policy fires on real data: at least one opportunity detected
    assert len(book) >= 1
    # Every record carries executable geometry and a valid lifecycle status
    valid_statuses = set(OpportunityStatus)
    for rec in book.all():
        assert rec.status in valid_statuses
        assert rec.entry_price is not None
        assert rec.stop_price is not None
        assert rec.target_price is not None
        assert rec.as_of_time_ns <= rec.valid_until_ns

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
