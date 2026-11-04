"""Native historical diagnostic: modeled close availability, no economic claim."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import Bar, BarType, Currency, Venue
from nautilus_trader.trading import Strategy

from v8_next.adapters.captured_market import load_candles
from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.native_tape import build_engine
from v8_next.domain.market import Candle, frame_at
from v8_next.economics.decisions import (
    UtilityInputs,
    observe_squeeze,
    opportunity_at,
    reconcile,
    utility_admission,
)
from v8_next.experts.catalog import observe_all


class HistoricalObserver(Strategy):
    """Receive native bar callbacks; observations cannot submit campaigns."""

    def __init__(self) -> None:
        super().__init__()
        self.source: dict[int, Candle] = {}
        self.prefix: list[Candle] = []
        self.decisions: list[dict[str, Any]] = []

    def on_start(self) -> None:
        self.subscribe_bars(BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"))

    def on_bar(self, bar: Bar) -> None:
        candle = self.source[bar.ts_event]
        # This is an explicitly modeled frame. Raw capture availability remains unknown.
        self.prefix.append(replace(candle, available_ns=candle.end_ns))
        frame = frame_at(candle.instrument_id, bar.ts_init, tuple(self.prefix))
        opportunity = opportunity_at(frame)
        stance = observe_squeeze(frame, opportunity)
        self.decisions.append(
            {
                "decision_ns": bar.ts_init,
                "source_received_ns": candle.received_ns,
                "historical_available_ns": candle.available_ns,
                "modeled_available_ns": candle.end_ns,
                "opportunity": asdict(opportunity) if opportunity else None,
                "stance": asdict(stance),
                "expert_diagnostics": [asdict(s) for s in observe_all(frame, opportunity)],
                "baseline": "BREAKOUT" if opportunity else "NO_OPPORTUNITY",
                "reconciliation": reconcile(opportunity, (stance,)) if opportunity else None,
                "admission": utility_admission(
                    UtilityInputs(None, None, None, None, None, None, None)
                ),
                "execution": "NOT_SUBMITTED",
                "claim_status": "NO_ECONOMIC_CLAIM",
            }
        )


def backtest(
    manifest: Path, maker_fee: Decimal, taker_fee: Decimal, balance: Decimal
) -> dict[str, Any]:
    candles = load_candles(manifest)
    if any(c.instrument_id != "BTCUSDT-PERP.BINANCE" for c in candles):
        raise ValueError("initial observer backtest scope is BTCUSDT")
    if len({c.end_ns for c in candles}) != len(candles):
        raise ValueError("duplicate historical bar boundary")
    engine, metadata = build_engine(manifest, maker_fee, taker_fee, balance)
    try:
        observer = HistoricalObserver()
        observer.source = {c.end_ns: c for c in candles}
        engine.add_strategy(observer)
        engine.run()
        return {
            **metadata,
            "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "scope": "HISTORICAL_OBSERVER_DIAGNOSTIC_NOT_QUALIFIED_ECONOMIC_BACKTEST",
            "decisions": observer.decisions,
            "account": economic_state(engine, Venue("BINANCE"), Currency.from_str("USDT")),
            "statistical_results": None,
            "promotion_eligible": False,
        }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--maker-fee", type=Decimal, required=True)
    parser.add_argument("--taker-fee", type=Decimal, required=True)
    parser.add_argument("--initial-balance", type=Decimal, required=True)
    args = parser.parse_args()
    result = backtest(args.manifest, args.maker_fee, args.taker_fee, args.initial_balance)
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
