"""Map captured real data to the pinned native engine for diagnostic replay."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
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

from v8_next.adapters.captured_market import load_candles
from v8_next.adapters.execution_models import (
    ExecutionProfile,
    profile_summary,
    resolve_profile,
    venue_kwargs,
)
from v8_next.adapters.funding_history import funding_artifacts
from v8_next.domain.market import Candle


def capture_native_inputs(
    manifest_path: Path,
    maker_fee: Decimal,
    taker_fee: Decimal,
) -> tuple[tuple[Candle, ...], CryptoPerpetual, list[Bar], Currency]:
    """Decode verified capture data without allocating an execution engine."""
    if any(not x.is_finite() or x < 0 for x in (maker_fee, taker_fee)):
        raise ValueError("finite nonnegative fee assumptions required")
    candles = load_candles(manifest_path)
    if not candles:
        raise ValueError("no completed real candles")
    manifest = json.loads(manifest_path.read_text())
    metadata = json.loads((manifest_path.parent / "instruments.json").read_text())
    record = next(s for s in metadata["symbols"] if s["symbol"] == manifest["symbol"])
    if record["contractType"] != "PERPETUAL" or record["marginAsset"] != record["quoteAsset"]:
        raise ValueError("only linear perpetuals supported")
    filters = {f["filterType"]: f for f in record["filters"]}
    lot = filters["LOT_SIZE"]
    price = filters["PRICE_FILTER"]
    currency = Currency.from_str(record["marginAsset"])
    instrument_id = InstrumentId.from_str(candles[0].instrument_id)
    instrument = CryptoPerpetual(
        instrument_id=instrument_id,
        raw_symbol=Symbol(record["symbol"]),
        base_currency=Currency.from_str(record["baseAsset"]),
        quote_currency=Currency.from_str(record["quoteAsset"]),
        settlement_currency=currency,
        is_inverse=False,
        price_precision=record["pricePrecision"],
        size_precision=record["quantityPrecision"],
        price_increment=Price.from_str(price["tickSize"]),
        size_increment=Quantity.from_str(lot["stepSize"]),
        min_quantity=Quantity.from_str(lot["minQty"]),
        max_quantity=Quantity.from_str(lot["maxQty"]),
        min_notional=Money.from_str(f"{filters['MIN_NOTIONAL']['notional']} {currency}"),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        ts_event=0,
        ts_init=0,
    )
    bar_type = BarType.from_str(f"{instrument_id}-1-HOUR-LAST-EXTERNAL")
    bars = [
        Bar(
            bar_type,
            Price.from_str(str(c.open)),
            Price.from_str(str(c.high)),
            Price.from_str(str(c.low)),
            Price.from_str(str(c.close)),
            Quantity.from_str(str(c.volume)),
            c.end_ns,
            c.end_ns,
        )
        for c in candles
    ]
    return candles, instrument, bars, currency


def build_engine(
    manifest_path: Path,
    maker_fee: Decimal,
    taker_fee: Decimal,
    initial_balance: Decimal,
    *,
    historical_data: bool = True,
    execution_profile: str | ExecutionProfile | None = None,
) -> tuple[BacktestEngine, dict[str, Any]]:
    """Historical timing model is diagnostic-only, never a certified PIT conversion.

    Caller owns disposal. Fees are explicit simulation assumptions, not observed fees.

    execution_profile: named Nautilus simulated-execution profile. None keeps the
    engine defaults, and the returned metadata then reports no declared execution
    semantics rather than implying a model nobody chose.
    """
    if any(not x.is_finite() or x < 0 for x in (maker_fee, taker_fee, initial_balance)):
        raise ValueError("finite nonnegative simulation inputs required")
    if initial_balance == 0:
        raise ValueError("positive initial balance required")
    candles, instrument, bars, currency = capture_native_inputs(manifest_path, maker_fee, taker_fee)
    instrument_id = instrument.id
    engine = BacktestEngine(BacktestEngineConfig())
    profile = resolve_profile(execution_profile) if execution_profile is not None else None
    try:
        engine.add_venue(
            Venue("BINANCE"),
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money.from_str(f"{initial_balance} {currency}")],
            default_leverage=Decimal(1),
            liquidation_enabled=False,
            **(venue_kwargs(profile) if profile is not None else {}),
        )
        engine.add_instrument(instrument)
        if historical_data:
            engine.add_data(bars)
        funding_rows = [
            row
            for artifact in funding_artifacts(json.loads(manifest_path.read_text()))
            for row in json.loads((manifest_path.parent / artifact["path"]).read_text())
        ]
        settlements = 0
        for row in funding_rows:
            if not historical_data:
                break
            boundary = int(row["fundingTime"]) * 1_000_000
            if not candles[0].end_ns <= boundary <= candles[-1].end_ns:
                continue
            if not row.get("markPrice"):
                raise ValueError("funding settlement mark unavailable")
            engine.add_data(
                [
                    MarkPriceUpdate(
                        instrument_id, Price.from_str(row["markPrice"]), boundary, boundary
                    )
                ]
            )
            engine.add_data(
                [
                    FundingRateUpdate(
                        instrument_id,
                        Decimal(row["fundingRate"]),
                        boundary,
                        boundary,
                        next_funding_ns=boundary,
                    )
                ]
            )
            settlements += 1
        return engine, {
            "claim_status": "NO_ECONOMIC_CLAIM",
            "authority": "DIAGNOSTIC_ONLY",
            "timing_model": "historical_close_assumed_available_not_certified",
            "metadata_scope": "current_metadata_not_historical_universe",
            "margin_scope": "generic_1x_netting_not_venue_liquidation_qualification",
            "maker_fee_assumption": str(maker_fee),
            "taker_fee_assumption": str(taker_fee),
            "initial_balance_assumption": str(initial_balance),
            "bar_count": len(bars),
            "funding_event_count": settlements,
            "execution_semantics": (
                profile_summary(profile)
                if profile is not None
                else {
                    "profile": None,
                    "evidence_class": "UNSPECIFIED_EXECUTION_SEMANTICS",
                }
            ),
        }
    except BaseException:
        engine.dispose()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--maker-fee", type=Decimal, required=True)
    parser.add_argument("--taker-fee", type=Decimal, required=True)
    parser.add_argument("--initial-balance", type=Decimal, required=True)
    args = parser.parse_args()
    engine, metadata = build_engine(
        args.manifest, args.maker_fee, args.taker_fee, args.initial_balance
    )
    try:
        engine.run()
        metadata["engine_iterations"] = engine.iteration
        metadata["orders"] = len(engine.cache.orders())
        metadata["positions"] = len(engine.cache.positions())
        print(json.dumps(metadata, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
