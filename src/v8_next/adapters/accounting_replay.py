"""Revalue frozen paper campaigns using only final funding known at the cutoff."""

import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import Currency, InstrumentId, Price, Quantity, QuoteTick, Venue

from v8_next.adapters.binance_capture import verify
from v8_next.adapters.campaign import PaperCampaignAdapter
from v8_next.adapters.captured_market import load_candles
from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.native_tape import build_engine, capture_native_inputs
from v8_next.adapters.settlements import (
    final_funding,
    funding_exposure_history,
    funding_query_windows,
    missing_announced_settlements,
    position_funding_query_coverage,
)
from v8_next.domain.campaign import PaperCampaign
from v8_next.domain.market import CausalFrame, frame_at
from v8_next.evaluation.outcomes import observed_outcomes


def replay_frozen_campaigns(
    manifests: list[Path],
    campaigns: tuple[PaperCampaign, ...],
    config: dict[str, str],
    accounting_as_of_ns: int,
) -> dict[str, Any]:
    """No observers, calibration, allocator or economic callbacks run here.

    This is a revised simulated accounting view, not a replacement of historical
    decisions or their knowledge state. It does not establish complete funding
    coverage beyond the final settlement records actually supplied by the venue.
    """
    quotes = []
    instruments: dict[str, tuple[int, Path]] = {}
    quote_keys: set[tuple[str, int]] = set()
    validity_frames: dict[tuple[str, int], CausalFrame] = {}
    for manifest in manifests:
        verify(manifest)
        metadata = json.loads(manifest.read_text())
        artifact = next(a for a in metadata["artifacts"] if a["path"] == "quote.json")
        received = int(artifact["received_time_ns"])
        if received > accounting_as_of_ns:
            continue
        row = json.loads((manifest.parent / "quote.json").read_text())
        if row["symbol"] not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("unsupported accounting instrument")
        instrument_id = row["symbol"] + "-PERP.BINANCE"
        if (instrument_id, received) in quote_keys:
            raise ValueError("duplicate accounting instrument receipt")
        quote_keys.add((instrument_id, received))
        if instrument_id not in instruments or received < instruments[instrument_id][0]:
            instruments[instrument_id] = (received, manifest)
        event = int(row["time"]) * 10**6
        if event > received or Decimal(row["bidPrice"]) > Decimal(row["askPrice"]):
            raise ValueError("invalid quote clocks or spread")
        if any(
            c.close_invalidation_price is not None
            or c.live_channel_bars is not None
            or c.validity_indicator is not None
            or c.close_breach_price is not None
            for c in campaigns
        ):
            candles = load_candles(manifest)
            validity_frames[(instrument_id, received)] = frame_at(
                instrument_id,
                received,
                tuple(replace(c, available_ns=c.received_ns) for c in candles),
            )
        quotes.append(
            QuoteTick(
                InstrumentId.from_str(instrument_id),
                Price.from_str(row["bidPrice"]),
                Price.from_str(row["askPrice"]),
                Quantity.from_str(row["bidQty"]),
                Quantity.from_str(row["askQty"]),
                event,
                received,
            )
        )
    if not quotes:
        raise ValueError("no quotes known by accounting cutoff")
    quotes.sort(key=lambda q: (q.ts_init, str(q.instrument_id), q.ts_event))
    if any(
        c.instrument_id not in instruments
        or c.decision_ns < instruments[c.instrument_id][0]
        or c.decision_ns > accounting_as_of_ns
        for c in campaigns
    ):
        raise ValueError("campaign outside captured accounting window")
    settlements = final_funding(
        manifests, quotes[0].ts_init, accounting_as_of_ns, accounting_as_of_ns
    )
    instrument_sources = [item[1][1] for item in sorted(instruments.items())]
    settlements = tuple(s for s in settlements if s.instrument_id in instruments)
    engine, _ = build_engine(
        instrument_sources[0],
        Decimal(config["maker_fee"]),
        Decimal(config["taker_fee"]),
        Decimal(config["initial_balance"]),
        historical_data=False,
    )
    try:
        for manifest in instrument_sources[1:]:
            _, instrument, _, _ = capture_native_inputs(
                manifest, Decimal(config["maker_fee"]), Decimal(config["taker_fee"])
            )
            engine.add_instrument(instrument)
        execution = PaperCampaignAdapter(campaigns)
        execution.validity_frames = validity_frames
        engine.add_strategy(execution)
        engine.add_data(quotes)
        for settlement in settlements:
            mark, funding = settlement.execution_replay_events(accounting_as_of_ns)
            engine.add_data([mark])
            engine.add_data([funding])
        engine.run()
        if execution.callback_failure is not None:
            raise ValueError(f"accounting callback failed: {execution.callback_failure}")
        result = economic_state(engine, Venue("BINANCE"), Currency.from_str("USDT"))
        result["instruments"] = sorted(instruments)
        result["position_closures"] = execution.closed_position_records()
        result["campaign_observations"] = execution.campaign_observations(result)
        result["outcomes"] = observed_outcomes(
            [c.to_record() for c in campaigns],
            result["position_closures"],
            result,
            Decimal(config["initial_balance"]),
            economic_policy=config,
            campaign_observations=result["campaign_observations"],
        )
        result["view"] = "REVISED_SIMULATED_ACCOUNTING_FROZEN_DECISIONS"
        result["accounting_as_of_ns"] = accounting_as_of_ns
        result["settlement_sources"] = [
            {**asdict(s), "rate": str(s.rate), "mark_price": str(s.mark_price)} for s in settlements
        ]
        result["funding_query_windows"] = funding_query_windows(manifests, accounting_as_of_ns)
        result["position_funding_query_coverage"] = position_funding_query_coverage(
            funding_exposure_history(result), result["funding_query_windows"], accounting_as_of_ns
        )
        missing = missing_announced_settlements(
            manifests, settlements, quotes[0].ts_init, accounting_as_of_ns
        )
        result["missing_announced_settlements"] = list(missing) if missing is not None else None
        result["funding_coverage"] = funding_coverage_status(result, missing)
        return result
    finally:
        engine.dispose()


def funding_coverage_status(account: dict[str, Any], missing: tuple[int, ...] | None) -> str:
    """No exposure is inapplicability, never certification of settlement data."""
    filled = [Decimal(order["filled_qty"]) for order in account["orders"]]
    if any(not quantity.is_finite() or quantity < 0 for quantity in filled):
        raise ValueError("invalid native filled quantity")
    if not funding_exposure_history(account) and not any(quantity > 0 for quantity in filled):
        return "NOT_APPLICABLE_NO_POSITION_EXPOSURE"
    if missing:
        return "INCOMPLETE_ANNOUNCED_SETTLEMENT_MISSING"
    return "OBSERVED_FINAL_RECORDS_ONLY_NOT_COMPLETENESS_CERTIFIED"
