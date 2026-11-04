"""Public venue instrument definitions via the upstream Nautilus adapter.

Read-only: fetches the public instrument catalogue through
``nautilus_trader.adapters.binance.load_binance_instruments`` with a keyless
data-client config. Accepts no credentials, submits no orders, opens no
positions. Output is a manifest carrying the venue source URL, receipt
timestamps, and sha256 over the canonical instrument bytes — the same identity
pattern as :mod:`v8_next.adapters.binance_capture`.

A venue or network failure fails closed: non-zero exit plus a stderr reason,
never an empty manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, cast

from nautilus_trader.adapters.binance import (
    BinanceDataClientConfig,
    BinanceInstrumentProviderConfig,
    BinanceProductType,
    load_binance_instruments,
)

VENUES = ("BINANCE",)

#: Public venue base behind the Nautilus USD-M data-client catalogue load.
VENUE_SOURCE_URLS = {
    "BINANCE": "https://fapi.binance.com",
}


def _adapter_version() -> str:
    try:
        return _pkg_version("nautilus-trader")
    except Exception:
        return "unknown"


def fetch_manifest(
    *,
    venue: str = "BINANCE",
    instrument_id: str | None = None,
) -> dict[str, Any]:
    """Load the public catalogue and bind it into a manifest dict (no disk I/O)."""
    if venue not in VENUES:
        raise ValueError(f"unsupported venue {venue!r}; expected one of {VENUES}")
    requested_ns = time.time_ns()
    provider_config = (
        BinanceInstrumentProviderConfig(load_ids=[instrument_id])
        if instrument_id
        else BinanceInstrumentProviderConfig()
    )
    config = BinanceDataClientConfig(
        product_type=BinanceProductType.USD_M,
        instrument_provider=provider_config,
    )
    try:
        instruments = asyncio.run(load_binance_instruments(config))
    except Exception as exc:
        raise RuntimeError(f"venue catalogue unreachable via nautilus-binance: {exc}") from exc
    received_ns = time.time_ns()
    entries: list[dict[str, Any]] = []
    for instrument in instruments:
        payload: dict[str, Any] = cast(Any, instrument).to_dict()
        if instrument_id is not None and payload.get("id") != instrument_id:
            continue
        raw = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
        entries.append(
            {
                "id": payload.get("id"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "body": payload,
            }
        )
    if instrument_id is not None and not entries:
        raise ValueError(f"instrument {instrument_id} absent from Binance response")
    return {
        "schema_version": 1,
        "venue": venue,
        "source_url": VENUE_SOURCE_URLS[venue],
        "adapter": f"nautilus-binance {_adapter_version()}",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "purpose": "public_instrument_definitions_not_historical_specification",
        "request_time_ns": requested_ns,
        "received_time_ns": received_ns,
        "credentials": "none",
        "orders_submitted": 0,
        "instruments": entries,
    }


def write_manifest(manifest: dict[str, Any], destination: Path) -> Path:
    """Write the manifest; refuse to overwrite (immutable artifact)."""
    if destination.exists():
        raise ValueError(f"refusing to overwrite {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode() + b"\n"
    manifest_with_digest = dict(manifest)
    manifest_with_digest["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    destination.write_bytes(
        json.dumps(manifest_with_digest, indent=2, sort_keys=True, default=str).encode()
        + b"\n"
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", default="BINANCE", choices=list(VENUES))
    parser.add_argument(
        "--instrument-id",
        default=None,
        help="Optional single instrument id filter (e.g. BTCUSDT-PERP.BINANCE)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Manifest output path (default prints to stdout, no file written)",
    )
    args = parser.parse_args()
    try:
        manifest = fetch_manifest(venue=args.venue, instrument_id=args.instrument_id)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out is None:
        print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
        return 0
    try:
        path = write_manifest(manifest, args.out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
