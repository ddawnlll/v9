"""Decode verified venue responses without inventing historical availability."""

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from v8_next.adapters.binance_capture import validate_capture
from v8_next.adapters.funding_history import funding_artifacts
from v8_next.domain.market import Candle
from v8_next.domain.positioning import PositioningReading


def load_candles(manifest_path: Path) -> tuple[Candle, ...]:
    validate_capture(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    artifact = next(a for a in manifest["artifacts"] if a["path"] == "bars.json")
    rows = json.loads((manifest_path.parent / "bars.json").read_text())
    received_ns = int(artifact["received_time_ns"])
    result = []
    for row in rows:
        # Binance kline closeTime is the inclusive final millisecond.
        end_ns = (int(row[6]) + 1) * 1_000_000
        if end_ns > received_ns:
            continue  # The current mutable candle is not a closed-bar observation.
        result.append(
            Candle(
                instrument_id=f"{manifest['symbol']}-PERP.BINANCE",
                start_ns=int(row[0]) * 1_000_000,
                end_ns=end_ns,
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
                received_ns=received_ns,
                available_ns=None,
                source_hash=artifact["sha256"],
            )
        )
    return tuple(result)


def load_settled_funding(
    manifest_path: Path, *, max_age_ns: int
) -> tuple["PositioningReading", ...]:
    """Decode final rates known at receipt, never at historical settlement.

    max_age_ns is an explicit economic freshness policy measured from the
    settlement event; it is not the venue's next funding interval.
    """
    if type(max_age_ns) is not int or max_age_ns <= 0:
        raise ValueError("positive funding freshness policy required")
    validate_capture(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    result: dict[int, PositioningReading] = {}
    for artifact in funding_artifacts(manifest):
        rows = json.loads((manifest_path.parent / artifact["path"]).read_text())
        if not isinstance(rows, list):
            raise ValueError("funding response must be a list")
        for row in rows:
            if row["symbol"] != manifest["symbol"] or type(row["fundingTime"]) is not int:
                raise ValueError("invalid funding symbol or settlement timestamp")
            event_ns = row["fundingTime"] * 1_000_000
            reading = PositioningReading(
                instrument_id=f"{manifest['symbol']}-PERP.BINANCE",
                metric="settled_funding_rate",
                value=Decimal(str(row["fundingRate"])),
                event_ns=event_ns,
                received_ns=artifact["received_time_ns"],
                available_ns=artifact["received_time_ns"],
                valid_until_ns=event_ns + max_age_ns,
                source_hash=artifact["sha256"],
            )
            if event_ns in result and result[event_ns] != reading:
                raise ValueError("conflicting captured funding rates")
            result[event_ns] = reading
    return tuple(result[t] for t in sorted(result))


def load_open_interest(manifest_path: Path, *, max_age_ns: int) -> tuple[PositioningReading, ...]:
    """Present OI snapshot; absence remains absent in older capture schemas."""
    if type(max_age_ns) is not int or max_age_ns <= 0:
        raise ValueError("positive OI freshness policy required")
    validate_capture(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    artifact = next((a for a in manifest["artifacts"] if a["path"] == "open_interest.json"), None)
    if artifact is None:
        return ()
    row = json.loads((manifest_path.parent / "open_interest.json").read_text())
    if row["symbol"] != manifest["symbol"] or type(row["time"]) is not int:
        raise ValueError("invalid OI symbol or event timestamp")
    event_ns = row["time"] * 1_000_000
    return (
        PositioningReading(
            instrument_id=f"{manifest['symbol']}-PERP.BINANCE",
            metric="open_interest",
            value=Decimal(str(row["openInterest"])),
            event_ns=event_ns,
            received_ns=artifact["received_time_ns"],
            available_ns=artifact["received_time_ns"],
            valid_until_ns=event_ns + max_age_ns,
            source_hash=artifact["sha256"],
        ),
    )


def load_account_ratio(
    manifest_path: Path, *, period: str, max_age_ns: int
) -> tuple[PositioningReading, ...]:
    """Global account-count ratio, not top-trader or position-notional ratio."""
    from v8_next.adapters.binance_capture import RATIO_PERIODS

    if period not in RATIO_PERIODS or type(max_age_ns) is not int or max_age_ns <= 0:
        raise ValueError("explicit ratio period and positive freshness required")
    validate_capture(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    artifact = next((a for a in manifest["artifacts"] if a["path"] == "account_ratio.json"), None)
    if artifact is None:
        return ()
    if parse_qs(urlsplit(artifact["source_url"]).query).get("period") != [period]:
        raise ValueError("ratio source period differs from policy")
    rows = json.loads((manifest_path.parent / "account_ratio.json").read_text())
    if not isinstance(rows, list):
        raise ValueError("account ratio response must be a list")
    result: dict[int, PositioningReading] = {}
    for row in rows:
        if row["symbol"] != manifest["symbol"] or type(row["timestamp"]) is not int:
            raise ValueError("invalid ratio symbol or timestamp")
        event = row["timestamp"] * 1_000_000
        reading = PositioningReading(
            instrument_id=f"{manifest['symbol']}-PERP.BINANCE",
            metric="long_short_ratio",
            value=Decimal(str(row["longShortRatio"])),
            event_ns=event,
            received_ns=artifact["received_time_ns"],
            available_ns=artifact["received_time_ns"],
            valid_until_ns=event + max_age_ns,
            source_hash=artifact["sha256"],
        )
        if event in result and result[event] != reading:
            raise ValueError("conflicting account ratio observations")
        result[event] = reading
    return tuple(result[t] for t in sorted(result))
