"""Compose verified captures into one diagnostic native portfolio engine."""

from decimal import Decimal
from pathlib import Path

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model import AccountType, Money, OmsType, Venue

from v8_next.adapters.native_tape import capture_native_inputs
from v8_next.adapters.settlements import final_funding
from v8_next.domain.market import Candle


def build_portfolio_engine(
    manifests: tuple[Path, ...],
    *,
    maker_fee: Decimal,
    taker_fee: Decimal,
    initial_balance: Decimal,
    accounting_as_of_ns: int,
) -> tuple[BacktestEngine, tuple[Candle, ...]]:
    """Intersect capture extents, reject internal gaps, never certify historical PIT.

    Fees/current metadata and final funding are diagnostic replay assumptions.
    The caller owns engine disposal and experiment registration.
    """
    if not manifests or not initial_balance.is_finite() or initial_balance <= 0:
        raise ValueError("captures and positive capital required")
    inputs = [capture_native_inputs(path, maker_fee, taker_fee) for path in manifests]
    if len({str(item[1].id) for item in inputs}) != len(inputs):
        raise ValueError("duplicate portfolio instrument")
    currencies = {str(item[3]) for item in inputs}
    if currencies != {"USDT"}:
        raise ValueError("portfolio requires USDT settlement")
    start = max(item[0][0].end_ns for item in inputs)
    end = min(item[0][-1].end_ns for item in inputs)
    if start >= end:
        raise ValueError("no common portfolio interval")
    selected = [tuple(c for c in item[0] if start <= c.end_ns <= end) for item in inputs]
    boundaries = [tuple(c.end_ns for c in candles) for candles in selected]
    if any(times != boundaries[0] for times in boundaries):
        raise ValueError("portfolio capture gap or misalignment")
    hour = 3600 * 10**9
    if any(b - a != hour for a, b in zip(boundaries[0], boundaries[0][1:], strict=False)):
        raise ValueError("portfolio requires continuous hourly data")
    source = tuple(
        sorted(
            (c for candles in selected for c in candles), key=lambda c: (c.end_ns, c.instrument_id)
        )
    )
    if any(c.received_ns > accounting_as_of_ns for c in source):
        raise ValueError("portfolio capture unknown at accounting cutoff")
    settlements = final_funding(list(manifests), start, end, accounting_as_of_ns)
    engine = BacktestEngine(BacktestEngineConfig())
    try:
        engine.add_venue(
            Venue("BINANCE"),
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money.from_str(f"{initial_balance} USDT")],
            default_leverage=Decimal(1),
            liquidation_enabled=False,
        )
        for _, instrument, bars, _ in inputs:
            engine.add_instrument(instrument)
            engine.add_data([bar for bar in bars if start <= bar.ts_event <= end])
        for settlement in settlements:
            mark, funding = settlement.execution_replay_events(accounting_as_of_ns)
            engine.add_data([mark])
            engine.add_data([funding])
        return engine, source
    except Exception:
        engine.dispose()
        raise
