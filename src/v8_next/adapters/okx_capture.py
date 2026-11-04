"""Second-venue public capture: OKX SWAP klines + trades, same honesty contract.

Why a separate module
---------------------
:mod:`v8_next.adapters.binance_capture` is the Binance capture and is owned by
another in-flight workstream; F6 needs a *second venue* captured without editing
it. This module keeps the same contract, deliberately:

* raw venue bytes are written verbatim (re-hashable by a later reader);
* every artifact records ``sha256``, its ``source_url``, the request and receive
  clocks, and ``historical_pit_status: "UNKNOWN"`` -- a live REST capture cannot
  certify when the venue made the data available historically;
* the manifest carries ``claim_status: "NO_ECONOMIC_CLAIM"`` and the capture
  never authenticates, so nothing here is an account or venue-truth claim.

Two venue-specific honesty notes are named rather than papered over:

* OKX's public market REST endpoints publish **no** download checksum (unlike
  Binance Vision daily archives), so ``venue_declared_sha256`` is ``None`` with
  a reason, never a locally computed digest presented as a venue guarantee;
* OKX is a *contract* venue: ``BTC-USDT-SWAP`` is a linear USDT-margined swap
  quoted per contract, so the mapping onto a Nautilus instrument id
  (``BTCUSDT-PERP.OKX``) is recorded alongside the contract multiplier and tick
  size instead of being assumed.

Instrument mapping
------------------
``BTC-USDT-SWAP`` -> ``BTCUSDT-PERP.OKX``. The mapping is derived from the
venue's own ``instId``/``ctType`` fields and refuses anything it cannot parse,
rather than guessing a symbol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

BASE = "https://www.okx.com"
VENUE = "OKX"

#: OKX bar codes accepted by ``/api/v5/market/candles`` and their duration.
BAR_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}

#: Venue-published limits: candles <= 300, trades <= 100 per page on the public
#: market endpoints. The capture refuses a larger limit instead of silently
#: receiving a different page size than the manifest describes.
CANDLE_LIMIT_MAX = 300
TRADE_LIMIT_MAX = 100

NAUTILUS_VENUE = "OKX"


def _fetch(url: str, timeout: int = 30) -> tuple[bytes, int, int]:
    """Fetch raw bytes; return (payload, request_ns, receive_ns)."""
    requested_ns = time.time_ns()
    with urlopen(url, timeout=timeout) as response:
        raw = bytes(response.read())
    received_ns = time.time_ns()
    return raw, requested_ns, received_ns


def _okx_json(raw: bytes, what: str) -> Any:
    """Decode an OKX envelope and fail closed on ``code != "0"``."""
    payload = json.loads(raw)
    if isinstance(payload, dict) and payload.get("code") not in (None, "0"):
        raise ValueError(f"OKX rejected {what}: {payload.get('code')} {payload.get('msg')}")
    return payload


def nautilus_instrument_id(inst_id: str) -> str:
    """Map a linear OKX swap instId onto a Nautilus instrument id.

    ``BTC-USDT-SWAP`` -> ``BTCUSDT-PERP.OKX``. Raises on anything that is not a
    linear ``<BASE>-<QUOTE>-SWAP`` id, because a guessed symbol would attribute
    this venue's prices to the wrong instrument.
    """
    parts = inst_id.split("-")
    if len(parts) != 3 or parts[2] != "SWAP":
        raise ValueError(f"not a linear swap instId: {inst_id!r}")
    base, quote, _ = parts
    if not base.isalnum() or not quote.isalnum():
        raise ValueError(f"unparsable instId: {inst_id!r}")
    return f"{base}{quote}-PERP.{NAUTILUS_VENUE}"


def instrument_mapping(payload: Any) -> list[dict[str, Any]]:
    """Extract the venue's own contract facts for the instruments it returned."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("OKX instruments payload lacks a data array")
    out: list[dict[str, Any]] = []
    for row in rows:
        inst_id = row.get("instId")
        if row.get("ctType") != "linear" or not isinstance(inst_id, str):
            raise ValueError("OKX instrument is not a linear contract")
        out.append(
            {
                "venue": VENUE,
                "inst_id": inst_id,
                "inst_type": row.get("instType"),
                "ct_type": row.get("ctType"),
                "ct_val": row.get("ctVal"),
                "ct_val_ccy": row.get("ctValCcy"),
                "ct_mult": row.get("ctMult"),
                "tick_sz": row.get("tickSz"),
                "lot_sz": row.get("lotSz"),
                "min_sz": row.get("minSz"),
                "nautilus_instrument_id": nautilus_instrument_id(inst_id),
            }
        )
    if not out:
        raise ValueError("OKX instruments payload is empty")
    return out


def candle_stats(payload: Any) -> dict[str, Any]:
    """Row count and time window of one raw candles payload (OKX order = newest first)."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("candles payload lacks a data array")
    stamps: list[int] = []
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError("candles payload carries a malformed row")
        stamps.append(int(row[0]))
    return {
        "rows": len(rows),
        "first_ts_ms": min(stamps) if stamps else None,
        "last_ts_ms": max(stamps) if stamps else None,
    }


def trade_stats(payload: Any) -> dict[str, Any]:
    """Row count, time window and id range of one raw trades payload."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("trades payload lacks a data array")
    stamps = [int(r["ts"]) for r in rows if "ts" in r]
    ids = [int(r["tradeId"]) for r in rows if "tradeId" in r]
    return {
        "rows": len(rows),
        "first_ts_ms": min(stamps) if stamps else None,
        "last_ts_ms": max(stamps) if stamps else None,
        "min_trade_id": min(ids) if ids else None,
        "max_trade_id": max(ids) if ids else None,
    }


def _write_artifact(destination: Path, name: str, raw: bytes, url: str,
                    requested_ns: int, received_ns: int, **stats: Any) -> dict[str, Any]:
    path = destination / name
    path.write_bytes(raw)
    return {
        "path": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "source_url": url,
        "request_time_ns": requested_ns,
        "received_time_ns": received_ns,
        "historical_available_time_ns": None,
        "historical_pit_status": "UNKNOWN",
        # OKX publishes no checksum for these endpoints; say so, do not imply one.
        "venue_declared_sha256": None,
        "venue_checksum_status": "VENUE_PUBLISHES_NO_REST_DOWNLOAD_CHECKSUM",
        **stats,
    }


def capture(
    destination: Path | str,
    inst_id: str = "BTC-USDT-SWAP",
    *,
    bar: str = "1H",
    candle_limit: int = CANDLE_LIMIT_MAX,
    trade_pages: int = 4,
    trade_limit: int = TRADE_LIMIT_MAX,
    page_delay_s: float = 0.2,
) -> Path:
    """Capture OKX public instrument metadata, 1H candles and recent trades.

    Returns the manifest path. Nothing is authenticated, nothing is claimed to be
    a historical point-in-time dataset, and every artifact is stored verbatim.
    """
    dest = Path(destination)
    if not inst_id.isascii():
        raise ValueError("inst_id must be ASCII")
    if bar not in BAR_MS:
        raise ValueError(f"unsupported bar {bar!r}; known {sorted(BAR_MS)}")
    if not 1 <= candle_limit <= CANDLE_LIMIT_MAX:
        raise ValueError(f"candle_limit must be in [1, {CANDLE_LIMIT_MAX}]")
    if not 1 <= trade_limit <= TRADE_LIMIT_MAX:
        raise ValueError(f"trade_limit must be in [1, {TRADE_LIMIT_MAX}]")
    if trade_pages < 1:
        raise ValueError("trade_pages must be >= 1")
    dest.mkdir(parents=True, exist_ok=False)
    nautilus_id = nautilus_instrument_id(inst_id)
    artifacts: list[dict[str, Any]] = []
    started_ns = time.time_ns()

    # 1. Instrument metadata (venue's own contract facts for the mapping).
    url = BASE + "/api/v5/public/instruments?" + urlencode(
        {"instType": "SWAP", "instId": inst_id}
    )
    raw, req_ns, recv_ns = _fetch(url)
    payload = _okx_json(raw, "instruments")
    mapping = instrument_mapping(payload)
    artifacts.append(
        _write_artifact(
            dest, "instruments.json", raw, url, req_ns, recv_ns,
            rows=len(mapping), instrument_mapping=mapping,
        )
    )

    # 2. Klines.
    url = BASE + "/api/v5/market/candles?" + urlencode(
        {"instId": inst_id, "bar": bar, "limit": candle_limit}
    )
    raw, req_ns, recv_ns = _fetch(url)
    payload = _okx_json(raw, "candles")
    stats = candle_stats(payload)
    if stats["rows"] == 0:
        raise ValueError("OKX returned no candles")
    artifacts.append(_write_artifact(dest, "klines.json", raw, url, req_ns, recv_ns,
                                     bar=bar, **stats))

    # 3. Recent trades, paged backwards by tradeId via ``after``.
    after: int | None = None
    for page in range(trade_pages):
        params: dict[str, str | int] = {"instId": inst_id, "limit": trade_limit}
        if after is not None:
            params["after"] = after
        url = BASE + "/api/v5/market/trades?" + urlencode(params)
        raw, req_ns, recv_ns = _fetch(url)
        payload = _okx_json(raw, "trades")
        page_stats = trade_stats(payload)
        if page_stats["rows"] == 0:
            break
        artifacts.append(
            _write_artifact(dest, f"trades-{page:04d}.json", raw, url, req_ns, recv_ns,
                            **page_stats)
        )
        if page_stats["min_trade_id"] is None:
            break
        after = page_stats["min_trade_id"]
        if page_delay_s > 0 and page + 1 < trade_pages:
            time.sleep(page_delay_s)

    finished_ns = time.time_ns()
    trade_rows = [
        a for a in artifacts if a["path"].startswith("trades-")
    ]
    candle_art = next(a for a in artifacts if a["path"] == "klines.json")
    manifest = {
        "schema_version": 1,
        "venue": VENUE,
        "inst_id": inst_id,
        "nautilus_instrument_id": nautilus_id,
        "source": "OKX_PUBLIC_REST_MARKET_DATA",
        "purpose": "raw_second_venue_capture_not_certified_historical_PIT",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "instrument_mapping": mapping,
        "candle_bar": bar,
        "window": {
            "candles_first_ts_ms": candle_art["first_ts_ms"],
            "candles_last_ts_ms": candle_art["last_ts_ms"],
            "candle_rows": candle_art["rows"],
            "trade_pages": len(trade_rows),
            "trade_rows": sum(a["rows"] for a in trade_rows),
            "trades_first_ts_ms": (
                min(a["first_ts_ms"] for a in trade_rows) if trade_rows else None
            ),
            "trades_last_ts_ms": (
                max(a["last_ts_ms"] for a in trade_rows) if trade_rows else None
            ),
        },
        "capture_start_ns": started_ns,
        "capture_end_ns": finished_ns,
        "achieved_seconds": round((finished_ns - started_ns) / 1e9, 3),
        "artifacts": artifacts,
    }
    result = dest / "manifest.json"
    result.write_text(json.dumps(manifest, indent=2) + "\n")
    return result


def validate_capture(manifest_path: Path | str) -> None:
    """Re-verify a capture: integrity, provenance, clocks, and honesty of claim.

    Fails closed on a rewritten payload, a foreign host, a symbol mismatch,
    non-monotonic clocks, a claimed historical point-in-time, or an artifact that
    tries to present a local digest as a venue-published checksum.
    """
    path = Path(manifest_path)
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("claim_status") != "NO_ECONOMIC_CLAIM":
        raise ValueError("unsupported OKX capture schema or claim")
    inst_id = manifest.get("inst_id")
    if not isinstance(inst_id, str) or not inst_id.isascii():
        raise ValueError("invalid capture inst_id")
    if manifest.get("nautilus_instrument_id") != nautilus_instrument_id(inst_id):
        raise ValueError("capture instrument mapping does not match its inst_id")
    root = path.parent.resolve()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("empty OKX capture")
    previous_received = 0
    for artifact in artifacts:
        target = (root / artifact["path"]).resolve()
        if target.parent != root:
            raise ValueError("artifact escapes capture directory")
        if hashlib.sha256(target.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError(f"artifact hash mismatch: {target.name}")
        split = urlsplit(artifact["source_url"])
        if split.scheme != "https" or split.netloc != "www.okx.com":
            raise ValueError("unexpected OKX capture source")
        if parse_qs(split.query).get("instId") != [inst_id]:
            raise ValueError("OKX capture source instrument mismatch")
        requested, received = artifact["request_time_ns"], artifact["received_time_ns"]
        if type(requested) is not int or type(received) is not int or not 0 < requested <= received:
            raise ValueError("invalid OKX capture clocks")
        if received < previous_received:
            raise ValueError("OKX capture clocks are not monotonic")
        previous_received = received
        if (
            artifact.get("historical_pit_status") != "UNKNOWN"
            or artifact.get("historical_available_time_ns") is not None
        ):
            raise ValueError("OKX capture cannot certify historical availability")
        if artifact.get("venue_declared_sha256") is not None:
            raise ValueError("OKX capture must not claim a venue checksum it does not publish")


def capture_summary(manifest_path: Path | str) -> dict[str, Any]:
    """Publishable summary of what a capture actually returned."""
    manifest = json.loads(Path(manifest_path).read_text())
    window = manifest.get("window", {})
    return {
        "manifest_path": str(manifest_path),
        "venue": manifest.get("venue"),
        "inst_id": manifest.get("inst_id"),
        "nautilus_instrument_id": manifest.get("nautilus_instrument_id"),
        "claim_status": manifest.get("claim_status"),
        "candle_rows": window.get("candle_rows"),
        "candle_bar": manifest.get("candle_bar"),
        "candles_first_ts_ms": window.get("candles_first_ts_ms"),
        "candles_last_ts_ms": window.get("candles_last_ts_ms"),
        "trade_pages": window.get("trade_pages"),
        "trade_rows": window.get("trade_rows"),
        "trades_first_ts_ms": window.get("trades_first_ts_ms"),
        "trades_last_ts_ms": window.get("trades_last_ts_ms"),
        "artifacts": len(manifest.get("artifacts", [])),
        "achieved_seconds": manifest.get("achieved_seconds"),
        "instrument_mapping": manifest.get("instrument_mapping"),
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--inst-id", default="BTC-USDT-SWAP")
    parser.add_argument("--bar", default="1H")
    parser.add_argument("--candle-limit", type=int, default=CANDLE_LIMIT_MAX)
    parser.add_argument("--trade-pages", type=int, default=4)
    args = parser.parse_args(argv)
    manifest = capture(
        args.destination,
        args.inst_id,
        bar=args.bar,
        candle_limit=args.candle_limit,
        trade_pages=args.trade_pages,
    )
    validate_capture(manifest)
    print(json.dumps(capture_summary(manifest), indent=2))
    return 0


# Re-exported so a reader can convert a captured price to a Decimal without
# float() in the money path.
def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"not decimal-parsable: {value!r}") from None


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())
