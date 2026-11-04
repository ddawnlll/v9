"""Multi-asset portfolio backtest: one account, N instruments, real funding feed.

P and P+E both execute inside a single Nautilus MARGIN/NETTING account with
shared capital and margin. P attaches one incumbent ensemble per leg; P+E
attaches incumbent + challenger ensembles per leg with halved notionals, so
the total per-leg risk budget is identical and the comparison is engine-level,
never a summation of standalone P&Ls.

Funding settles natively: every in-window tape funding row becomes a
MarkPriceUpdate (mark = leg close at the boundary, labeled mark-proxy) plus a
FundingRateUpdate. Settlement counts and funding-paid reconciliation are
reported; rows outside the window are excluded, never extrapolated.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
    BookType,
    CryptoPerpetual,
    Currency,
    FundingRateUpdate,
    InstrumentId,
    MarkPriceUpdate,
    Money,
    OmsType,
    Price,
    Quantity,
    Symbol,
    Venue,
)

from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.execution_models import (
    DEFAULT_PROFILE,
    ExecutionProfile,
    resolve_profile,
    venue_kwargs,
)
from v8_next.adapters.execution_telemetry import (
    as_float,
    execution_telemetry,
    fill_signature,
    json_safe,
    money_amounts,
    native_fill_records,
)
from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy, ExpertStrategyConfig
from v8_next.domain.market import Candle
from v8_next.domain.positioning import PositioningReading
from v8_next.evaluation.multitape import FundingRow

#: Base currency per raw symbol, from the venue's instrument metadata for the
#: four-year universe. A symbol missing here is REJECTED (see ``base_currency``):
#: defaulting an unknown symbol to BTC would fabricate instrument metadata.
BASE_CURRENCIES = {
    "ADAUSDT": "ADA",
    "AVAXUSDT": "AVAX",
    "BNBUSDT": "BNB",
    "BTCUSDT": "BTC",
    "DOGEUSDT": "DOGE",
    "ETHUSDT": "ETH",
    "LINKUSDT": "LINK",
    "LTCUSDT": "LTC",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
}


def _causal_funding_proxy(
    candles: tuple[Candle, ...], boundary_ns: int, ends: list[int] | None = None
) -> Decimal | None:
    """Historical close-time proxy, never a venue mark or certified availability.

    When availability is unknown, close-time availability is modeled only. A
    proxy older than its own bar duration is rejected rather than filled forever.

    Bars are chronological, so the eligible close is found by bisecting the end
    timestamps instead of scanning the whole leg per settlement: the previous
    linear scan made a four-year tape repair cost seconds per portfolio run.
    """
    if not candles:
        return None
    if ends is None:
        ends = [candle.end_ns for candle in candles]
    index = bisect_right(ends, boundary_ns) - 1
    while index >= 0:
        candle = candles[index]
        if candle.available_ns is None or candle.available_ns <= boundary_ns:
            if boundary_ns - candle.end_ns > candle.end_ns - candle.start_ns:
                return None
            return candle.close
        index -= 1
    return None


def _plan_funding_events(
    funding: tuple[FundingRow, ...],
    legs: dict[str, tuple[Candle, ...]],
    window_start: int | None,
    window_end: int | None,
) -> tuple[list[tuple[int, int, FundingRow, InstrumentId, Decimal]], dict[str, int]]:
    """Decide when each settlement becomes chargeable; count what cannot be.

    A venue settlement is knowable no earlier than its boundary and no earlier
    than local receipt. Charging a delayed record at its boundary would date its
    cash effect before it was observable; dropping it would hide the cost. It is
    therefore charged at ``max(boundary, receipt)`` and the shift is counted.
    """
    counters = {
        "out_of_window": 0,
        "unknown_leg": 0,
        "missing_mark": 0,
        "receipt_shifted": 0,
        "receipt_unknown": 0,
        "duplicate_boundary": 0,
    }
    prepared: list[tuple[int, int, FundingRow, InstrumentId, Decimal]] = []
    seen: set[tuple[str, int]] = set()
    ends_by_instrument: dict[str, list[int]] = {}
    for row in funding:
        boundary = row.funding_time_ms * 1_000_000
        if window_start is None or window_end is None:
            counters["out_of_window"] += 1
            continue
        if not window_start <= boundary <= window_end:
            counters["out_of_window"] += 1
            continue
        fund_iid = InstrumentId.from_str(f"{row.instrument}-PERP.BINANCE")
        leg = legs.get(row.instrument)
        if leg is None:
            counters["unknown_leg"] += 1
            continue
        ends = ends_by_instrument.get(row.instrument)
        if ends is None:
            ends = [candle.end_ns for candle in leg]
            ends_by_instrument[row.instrument] = ends
        mark = _causal_funding_proxy(leg, boundary, ends)
        if mark is None:
            counters["missing_mark"] += 1
            continue
        # Two records for one instrument/boundary would charge the same opening
        # twice; the venue charge is per boundary, so the repeat is reported.
        key = (row.instrument, row.funding_time_ms)
        if key in seen:
            counters["duplicate_boundary"] += 1
            continue
        seen.add(key)
        if row.received_ns is None:
            counters["receipt_unknown"] += 1
            effective = boundary
        else:
            effective = max(boundary, int(row.received_ns))
            if effective > window_end:
                counters["out_of_window"] += 1
                continue
            if effective != boundary:
                counters["receipt_shifted"] += 1
        prepared.append((effective, boundary, row, fund_iid, mark))
    prepared.sort(key=lambda item: (item[0], str(item[3]), item[1]))
    return prepared, counters


def base_currency(raw_symbol: str) -> str:
    """Base currency of a raw symbol, or an explicit refusal.

    Never guesses: an unmapped instrument is unsupported, not a BTC perpetual.
    """
    try:
        return BASE_CURRENCIES[raw_symbol]
    except KeyError:
        raise ValueError(
            f"no instrument metadata for raw symbol {raw_symbol!r}; refusing to "
            "fabricate a base currency"
        ) from None


@dataclass(frozen=True)
class SleeveSpec:
    name: str  # incumbent | challenger
    quorum: int
    tolerance: int
    notional_fraction: float  # share of the per-leg notional budget


def _instrument(
    instrument_id: str,
    raw_symbol: str,
    base: str,
    currency: Currency,
    maker_fee: Decimal,
    taker_fee: Decimal,
) -> CryptoPerpetual:
    return CryptoPerpetual(
        instrument_id=InstrumentId.from_str(instrument_id),
        raw_symbol=Symbol(raw_symbol),
        base_currency=Currency.from_str(base),
        quote_currency=currency,
        settlement_currency=currency,
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price(0.01, 2),
        size_increment=Quantity(0.001, 3),
        min_quantity=Quantity(0.001, 3),
        max_quantity=Quantity(1000.0, 3),
        min_notional=Money(1, currency),
        ts_event=0,
        ts_init=0,
        margin_init=Decimal("1"),
        margin_maint=Decimal("0.05"),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def _bars(candles: tuple[Candle, ...], bar_type: BarType) -> list[Bar]:
    return [
        Bar(
            bar_type,
            Price(float(c.open), 2),
            Price(float(c.high), 2),
            Price(float(c.low), 2),
            Price(float(c.close), 2),
            Quantity(float(c.volume), 3),
            c.end_ns,
            c.end_ns,
        )
        for c in candles
    ]


def trade_signature(result: dict[str, Any]) -> str:
    """Identity of executed trades: instrument/side/qty/avg/time per open."""
    import hashlib as _hl
    import json as _js

    opens = sorted(
        (
            str(p.get("instrument_id", "")),
            str(p.get("side", "")),
            str(p.get("quantity", "")),
            str(p.get("avg_px_open", "")),
            int(p.get("event_ns", 0) or 0),
        )
        for p in result.get("opened_positions", [])
        if isinstance(p, dict)
    )
    return _hl.sha256(_js.dumps(opens).encode()).hexdigest()


def _native_fill_records(engine: BacktestEngine) -> tuple[list[dict[str, Any]], str]:
    """Backwards-compatible alias for the shared telemetry helper."""
    return native_fill_records(engine)


def _fill_signature(records: list[dict[str, Any]]) -> str:
    """Backwards-compatible alias for the shared telemetry helper."""
    return fill_signature(records)


def _execution_telemetry(
    profile: ExecutionProfile,
    fill_records: list[dict[str, Any]],
    fill_report_type: str,
    opened: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Backwards-compatible alias for the shared telemetry helper."""
    return execution_telemetry(profile, fill_records, fill_report_type, opened, decisions)


# Re-exported under the historical private names so existing callers keep working.
_as_float = as_float
_json_safe = json_safe
_money_amounts = money_amounts


def _ordered_book_deltas(
    book_deltas: tuple[Any, ...], legs: dict[str, tuple[Candle, ...]]
) -> tuple[tuple[Any, ...], list[str]]:
    """Sort captured book deltas deterministically and reject foreign instruments.

    Book data for an instrument that is not part of this run would open an L2
    book the engine never matches against (or, worse, silently disappear), so it
    is rejected rather than dropped. Within the run the deltas are sorted by
    ``(ts_event, sequence)`` so two runs of the same capture feed the engine the
    same bytes in the same order.
    """
    if not book_deltas:
        return (), []
    run_ids = {f"{raw}-PERP.BINANCE" for raw in legs}
    stray = sorted({str(d.instrument_id) for d in book_deltas} - run_ids)
    ordered = tuple(sorted(book_deltas, key=lambda d: (int(d.ts_event), int(d.sequence))))
    return ordered, stray


def run_portfolio_backtest(
    legs: dict[str, tuple[Candle, ...]],
    sleeves: tuple[SleeveSpec, ...],
    funding: tuple[FundingRow, ...] = (),
    *,
    per_leg_notional: Decimal = Decimal("1000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    initial_balance: Decimal = Decimal("10000"),
    venue: str = "BINANCE",
    currency: str = "USDT",
    bracket_stop_pct: Decimal | None = Decimal("0.02"),
    bracket_target_pct: Decimal | None = Decimal("0.04"),
    readings: tuple[PositioningReading, ...] = (),
    funding_dropped: int = 0,
    execution_profile: str | ExecutionProfile = DEFAULT_PROFILE,
    trades: tuple[Any, ...] = (),
    book_deltas: tuple[Any, ...] = (),
    book_type: Any = BookType.L2_MBP,
    extra_strategies: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """Execute the portfolio through one shared Nautilus account.

    legs maps raw symbols (BTCUSDT) to chronological candles; instrument ids
    are derived as <RAW>-PERP.BINANCE.

    funding_dropped counts malformed funding records skipped at tape load
    (MultiTape.funding_dropped). It is reported in the result, never ignored:
    a zero-funding P&L with dropped records is flagged, not presented as
    fully-covered.

    ``book_deltas`` is an optional captured L2/L3 ``OrderBookDelta`` sequence
    (see ``book_tape``). When it is present the venue is opened with ``book_type``
    and the profile is upgraded to ``depth_data_available=True`` -- the
    depth-dependent knobs only stop being inert when the engine really has a book
    to consume from, so the flag follows the data rather than the caller's
    intention. When it is absent nothing changes and the bar-only inert report
    stays true.
    """
    if not legs:
        raise ValueError("at least one instrument leg required")
    frac = sum(s.notional_fraction for s in sleeves)
    if abs(frac - 1.0) > 1e-9:
        raise ValueError(f"sleeve fractions must sum to 1.0, got {frac}")
    curr = Currency.from_str(currency)
    ven = Venue(venue)
    profile = resolve_profile(execution_profile)
    ordered_deltas, stray_instruments = _ordered_book_deltas(book_deltas, legs)
    if stray_instruments:
        raise ValueError(f"book deltas reference instruments outside the run: {stray_instruments}")
    book_deltas_fed = len(ordered_deltas)
    window_start = min((c[0].end_ns for c in legs.values() if c), default=None)
    window_end = max((c[-1].end_ns for c in legs.values() if c), default=None)
    # Depth-conditioned execution only exists where the captured book and the bar
    # window describe the same period. With disjoint windows the engine still runs
    # (and can even fill resting limits) but MARKET orders submitted from a bar
    # callback do not execute while an L2 book is configured -- measured on the
    # real capture: the same single-leg run produces 2 fills without the book and
    # 0 fills with it. Publishing `depth_data_available=True` there would dress an
    # empty run up as depth-exercised execution, so the flag follows the overlap.
    book_window_ns: tuple[int, int] | None = None
    if ordered_deltas:
        book_window_ns = (
            int(ordered_deltas[0].ts_event),
            int(ordered_deltas[-1].ts_event),
        )
    book_window_overlap: bool | None = None
    if book_window_ns is not None and window_start is not None and window_end is not None:
        book_window_overlap = not (
            book_window_ns[1] < window_start or book_window_ns[0] > window_end
        )
    if book_window_overlap:
        profile = replace(profile, depth_data_available=True)
    venue_exec = dict(venue_kwargs(profile))
    if ordered_deltas:
        venue_exec["book_type"] = book_type
    engine = BacktestEngine(
        BacktestEngineConfig(
            bypass_logging=True,
            logging=LoggerConfig(stdout_level=LogLevel.WARNING),
        )
    )
    try:
        # Execution semantics are explicit and digestible: fill model, fee model
        # and latency model all come from the named profile rather than from
        # engine defaults that were never written down.
        engine.add_venue(
            ven,
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(float(initial_balance), curr)],
            default_leverage=Decimal(1),
            liquidation_enabled=False,
            **venue_exec,
        )
        strategies: list[ExpertEnsembleStrategy] = []
        for raw, candles in legs.items():
            instrument_id = f"{raw}-PERP.BINANCE"
            base = base_currency(raw)
            engine.add_instrument(_instrument(instrument_id, raw, base, curr, maker_fee, taker_fee))
            if not candles:
                # A leg carried only for its instrument (e.g. a book-only probe
                # run) contributes no bars; adding an empty Bar list is skipped.
                continue
            bar_type = BarType.from_str(f"{instrument_id}-1-HOUR-LAST-EXTERNAL")
            engine.add_data(_bars(candles, bar_type))
            source_map = {c.end_ns: c for c in candles}
            for sleeve in sleeves:
                cfg = ExpertStrategyConfig(
                    instrument_id=instrument_id,
                    bar_type_str=str(bar_type),
                    min_support_quorum=sleeve.quorum,
                    max_contradiction_tolerance=sleeve.tolerance,
                    target_notional=per_leg_notional * Decimal(str(sleeve.notional_fraction)),
                    bracket_stop_pct=bracket_stop_pct,
                    bracket_target_pct=bracket_target_pct,
                    max_concurrent_positions=1,
                )
                strat = ExpertEnsembleStrategy(cfg, source_candles=source_map, readings=readings)
                strategies.append(strat)
                engine.add_strategy(strat)

        # Probe runs may supply their own strategy list instead of the incumbent
        # ensembles (the incumbent order logic itself is untouched). Those
        # strategies are added here, after every instrument exists.
        for probe in extra_strategies:
            engine.add_strategy(probe)

        # Captured order-book depth (L2/L3) drives the depth-dependent knobs.
        # Without it the venue is left at its default L1 book and the profile
        # reports those knobs as inert.
        if ordered_deltas:
            engine.add_data(list(ordered_deltas))

        # Trade ticks drive trade-based matching (aggressor evidence) alongside
        # bars. Empty by default; sorted by the loader for determinism.
        trades_fed = 0
        if trades:
            ordered = sorted(trades, key=lambda t: (t.ts_event, str(t.trade_id)))
            engine.add_data(ordered)
            trades_fed = len(ordered)

        prepared, funding_counts = _plan_funding_events(funding, legs, window_start, window_end)
        out_of_window = funding_counts["out_of_window"]
        unknown_leg = funding_counts["unknown_leg"]
        missing_mark = funding_counts["missing_mark"]
        receipt_shifted = funding_counts["receipt_shifted"]
        unknown_receipt = funding_counts["receipt_unknown"]
        settlements = 0
        for effective, boundary, row, fund_iid, mark in prepared:
            engine.add_data([MarkPriceUpdate(fund_iid, Price(float(mark), 2), boundary, boundary)])
            engine.add_data(
                [
                    FundingRateUpdate(
                        fund_iid,
                        row.funding_rate,
                        effective,
                        effective,
                        next_funding_ns=effective,
                    )
                ]
            )
            settlements += 1

        engine.run()
        account = economic_state(engine, ven, curr)
        opened: list[dict[str, Any]] = []
        closed: list[dict[str, Any]] = []
        all_decisions: list[dict[str, Any]] = []
        for strat in strategies:
            opened.extend(strat.opened_positions)
            closed.extend(strat.closed_positions)
            # Tag each decision with its instrument: execution telemetry must
            # match a fill to the decision of the SAME leg, never to another
            # asset's price.
            all_decisions.extend(
                {**d, "instrument_id": str(strat.instrument_id)} for d in strat.decisions
            )

        fill_records, fill_report_type = _native_fill_records(engine)
        execution = _execution_telemetry(
            profile, fill_records, fill_report_type, opened, all_decisions
        )
        return {
            "legs": sorted(legs),
            "sleeves": [s.name for s in sleeves],
            "total_bars": sum(len(c) for c in legs.values()),
            "decisions_count": len(all_decisions),
            "opened_positions": opened,
            "closed_positions": closed,
            "account": account,
            "execution": execution,
            "fill_signature": execution.get("fill_signature"),
            "fill_records": fill_records,
            "trades_fed": trades_fed,
            "book_deltas_fed": book_deltas_fed,
            "book_type": str(book_type) if ordered_deltas else None,
            # Whether the captured book and the bar window describe the same
            # period. False means the run cannot exercise depth-conditioned
            # execution: the execution block then keeps reporting the knobs as
            # INERT (with the named reason below) instead of active.
            "bar_window_ns": [window_start, window_end]
            if window_start is not None and window_end is not None
            else None,
            "book_window_ns": list(book_window_ns) if book_window_ns else None,
            "book_window_overlap": book_window_overlap,
            "book_window_reason": (
                None
                if book_window_overlap
                else "NO_OVERLAP_BETWEEN_CAPTURED_BOOK_AND_BAR_WINDOW"
                if book_window_ns is not None
                else "NO_BOOK_DELTAS_FED"
            ),
            "funding_settlements_fed": settlements,
            "funding_rows_available": len(funding),
            # Coverage accounting: a zero-funding P&L must be distinguishable
            # from a fully-covered one. NO_FUNDING_ROWS is explicit (the
            # no-fund experimental arm or a tape without funding records);
            # DROPPED_RECORDS flags cost that never reached the engine.
            "funding_dropped_at_load": funding_dropped,
            "funding_out_of_window": out_of_window,
            "funding_unknown_leg": unknown_leg,
            "funding_missing_mark": missing_mark,
            "funding_mark_basis": "OHLC_CLOSE_ASOF_BOUNDED_HISTORICAL_MODEL_NOT_VENUE_MARK",
            "funding_receipt_shifted": receipt_shifted,
            "funding_receipt_unknown": unknown_receipt,
            "funding_attribution_basis": (
                "CHARGE_AT_MAX"
                "(BOUNDARY,RECEIPT)"
            ),
            "funding_coverage": (
                "NO_FUNDING_ROWS"
                if not funding
                else (
                    "DROPPED_RECORDS"
                    if (funding_dropped or unknown_leg or missing_mark)
                    else (
                        "FULL_BOUNDARY_RECEIPT_ATTRIBUTED"
                        if not unknown_receipt
                        else "FULL_WITH_UNKNOWN_RECEIPT_TIME"
                    )
                )
            ),
        }
    finally:
        engine.dispose()
