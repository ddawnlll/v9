"""Captured Binance depth snapshots -> deterministic Nautilus OrderBookDelta stream.

What the capture actually contains
----------------------------------
``binance_capture.capture_depth`` stores a *sequence of point-in-time snapshots*
of the venue's public REST depth endpoint. Each snapshot is the full displayed
aggregated book (price level -> resting size) at the instant it was polled; it is
not a change stream, and Binance publishes no order-level (L3) data publicly.
Converting a snapshot into engine input therefore means: at the snapshot's venue
timestamp the book *was* exactly these levels, so the reader emits a ``CLEAR``
followed by one ``ADD`` per level. Nothing between two snapshots is invented; a
consumer that needs sub-snapshot resolution does not have it and must say so.

Why the top N levels and not all of them
----------------------------------------
``DEPTH_LEVEL_BOUND`` (10) bounds each side because:

* the venue's REST book is aggregated L2 (market-by-price), so 10 levels per side
  is already more depth than the order sizes this lab can trade consume, which
  means the bound cannot be hiding the level a fill would have hit;
* it fixes the delta count per snapshot deterministically (one CLEAR plus 2N
  ADDs), so a 15-minute capture replays in a bounded, reproducible way;
* levels *beyond* the bound are dropped and **counted** (``levels_beyond_bound``),
  so the truncation is reported rather than silently assumed away.

Determinism
-----------
Snapshots are read in filename order, the venue timestamp ordering is validated
to be non-decreasing, and every delta carries its snapshot index as its sequence
number. :func:`book_digest` hashes the full delta set, so reading the same
capture twice yields the same digest; a test asserts exactly that.

Fail-closed policy
------------------
A level row that is not a two-element, positive, parseable price/size pair is
dropped and counted. A snapshot whose venue timestamp is absent, or one side of
which parses to zero levels, is dropped and counted. A missing venue clock is
never replaced with the local capture clock, because that would mix two clocks
inside one stream.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from nautilus_trader.model import (
    BookAction,
    BookOrder,
    InstrumentId,
    OrderBookDelta,
    OrderSide,
    Price,
    Quantity,
)

#: Top levels per side kept from each snapshot. See the module docstring for why
#: 10, and note that levels beyond it are counted, not hidden.
DEPTH_LEVEL_BOUND = 10


def load_depth_manifest(directory: Path | str) -> dict[str, Any]:
    """Load and sanity-check a depth capture manifest."""
    manifest_path = Path(directory) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("depth capture manifest is not an object")
    if manifest.get("claim_status") != "NO_ECONOMIC_CLAIM":
        raise ValueError("depth capture does not carry a NO_ECONOMIC_CLAIM status")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("depth capture manifest has no artifacts")
    return manifest


def _venue_time_ns(payload: dict[str, Any]) -> int | None:
    """Snapshot instant on the *venue's* clock, in ns, or None when absent.

    ``T`` (transaction time) is preferred over ``E`` (event time) because it is
    the venue's own matching timestamp. A payload carrying neither is returned as
    None so the caller can drop it instead of substituting a local clock.
    """
    for key in ("T", "E"):
        value = payload.get(key)
        if type(value) is int and value > 0:
            return value * 1_000_000
    return None


def _levels(side: Any, *, price_precision: int, size_precision: int) -> tuple[list[tuple[Price, Quantity, int]], int]:
    """Parse one side's level rows into (price, size, index) tuples.

    Returns ``(parsed, dropped)``. A row is dropped when it is not a 2-element
    sequence, when the price is not a finite positive number, or when the size is
    not a finite non-negative number; a zero size means the level is absent from
    the displayed book and is skipped, not emitted as an ``ADD`` of nothing.
    """
    parsed: list[tuple[Price, Quantity, int]] = []
    dropped = 0
    if not isinstance(side, list):
        return parsed, 1
    for index, row in enumerate(side):
        if not isinstance(row, list) or len(row) != 2:
            dropped += 1
            continue
        try:
            price = Decimal(str(row[0]))
            size = Decimal(str(row[1]))
        except (InvalidOperation, ValueError):
            dropped += 1
            continue
        if not price.is_finite() or price <= 0 or not size.is_finite() or size < 0:
            dropped += 1
            continue
        if size == 0:
            continue
        parsed.append(
            (
                Price(float(price), price_precision),
                Quantity(float(size), size_precision),
                index,
            )
        )
    return parsed, dropped


def depth_to_deltas(
    payload: dict[str, Any],
    instrument_id: InstrumentId,
    *,
    sequence_base: int,
    ts_ns: int,
    levels: int = DEPTH_LEVEL_BOUND,
    price_precision: int = 2,
    size_precision: int = 3,
) -> tuple[list[OrderBookDelta], dict[str, int]]:
    """Convert one raw snapshot payload into CLEAR + ADD deltas for the top levels.

    The CLEAR is what makes a snapshot series honest: each snapshot is a *full*
    displayed book, so without clearing first, levels that disappeared between two
    snapshots would linger in the engine's book as phantom liquidity.
    """
    bids, bids_dropped = _levels(
        payload.get("bids"), price_precision=price_precision, size_precision=size_precision
    )
    asks, asks_dropped = _levels(
        payload.get("asks"), price_precision=price_precision, size_precision=size_precision
    )
    stats = {
        "rows": (
            (len(payload["bids"]) if isinstance(payload.get("bids"), list) else 0)
            + (len(payload["asks"]) if isinstance(payload.get("asks"), list) else 0)
        ),
        "rows_unparsable": bids_dropped + asks_dropped,
        "levels_beyond_bound": max(0, len(bids) - levels) + max(0, len(asks) - levels),
        "clears": 1,
        "adds": min(len(bids), levels) + min(len(asks), levels),
    }
    if not bids or not asks:
        # A one-sided or empty snapshot cannot describe the book it claims to.
        stats["snapshots_empty_side"] = 1
        return [], stats
    seq = sequence_base
    deltas = [
        OrderBookDelta(
            instrument_id,
            BookAction.CLEAR,
            BookOrder(OrderSide.NO_ORDER_SIDE, Price(0, price_precision), Quantity(0, size_precision), 0),
            0,
            seq,
            ts_ns,
            ts_ns,
        )
    ]
    seq += 1
    for price, size, index in bids[:levels]:
        deltas.append(
            OrderBookDelta(
                instrument_id,
                BookAction.ADD,
                BookOrder(OrderSide.BUY, price, size, index + 1),
                0,
                seq,
                ts_ns,
                ts_ns,
            )
        )
        seq += 1
    for price, size, index in asks[:levels]:
        deltas.append(
            OrderBookDelta(
                instrument_id,
                BookAction.ADD,
                BookOrder(OrderSide.SELL, price, size, 1_000_000 + index + 1),
                0,
                seq,
                ts_ns,
                ts_ns,
            )
        )
        seq += 1
    return deltas, stats


def depth_snapshots(
    directory: Path | str,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(artifact, payload)`` pairs in manifest (i.e. capture) order."""
    d = Path(directory)
    manifest = load_depth_manifest(d)
    for artifact in manifest["artifacts"]:
        path = d / artifact["path"]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            yield artifact, payload


def deltas_from_depth(
    directory: Path | str,
    instrument_id: str,
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
    levels: int = DEPTH_LEVEL_BOUND,
    price_precision: int = 2,
    size_precision: int = 3,
) -> tuple[tuple[OrderBookDelta, ...], dict[str, int]]:
    """Stream a capture into a deterministic OrderBookDelta sequence.

    Snapshots outside ``[start_ns, end_ns]`` (venue clock) are counted, never
    extrapolated into the window. Counters in the returned stats make every
    drop visible, so a downstream claim can state exactly how much of the book
    it is built from.
    """
    if levels < 1:
        raise ValueError("levels must be >= 1")
    d = Path(directory)
    iid = InstrumentId.from_str(instrument_id) if isinstance(instrument_id, str) else instrument_id
    deltas: list[OrderBookDelta] = []
    stats = {
        "snapshots": 0,
        "snapshots_in_window": 0,
        "snapshots_out_of_window": 0,
        "snapshots_without_venue_time": 0,
        "snapshots_empty_side": 0,
        "rows": 0,
        "rows_unparsable": 0,
        "levels_beyond_bound": 0,
        # Accumulated from the per-snapshot stats below; without these keys the
        # accumulation raises instead of reporting the CLEAR/ADD accounting.
        "clears": 0,
        "adds": 0,
        "deltas": 0,
    }
    last_ts: int | None = None
    for _artifact, payload in depth_snapshots(d):
        stats["snapshots"] += 1
        ts_ns = _venue_time_ns(payload)
        if ts_ns is None:
            stats["snapshots_without_venue_time"] += 1
            continue
        if (start_ns is not None and ts_ns < start_ns) or (end_ns is not None and ts_ns > end_ns):
            stats["snapshots_out_of_window"] += 1
            continue
        if last_ts is not None and ts_ns < last_ts:
            # The capture clock contract is non-decreasing; a reordering here
            # would silently scramble the book series.
            raise ValueError("depth snapshots are not in non-decreasing time order")
        last_ts = ts_ns
        stats["snapshots_in_window"] += 1
        snapshot_deltas, snapshot_stats = depth_to_deltas(
            payload,
            iid,
            sequence_base=stats["deltas"],
            ts_ns=ts_ns,
            levels=levels,
            price_precision=price_precision,
            size_precision=size_precision,
        )
        for key in ("rows", "rows_unparsable", "levels_beyond_bound", "clears", "adds"):
            stats[key] += snapshot_stats[key]
        if snapshot_stats.get("snapshots_empty_side"):
            stats["snapshots_empty_side"] += 1
            continue
        deltas.extend(snapshot_deltas)
        stats["deltas"] += len(snapshot_deltas)
    return tuple(deltas), stats


def book_digest(deltas: tuple[OrderBookDelta, ...]) -> str:
    """Identity of a book feed: action/side/price/size/order-id/sequence/time.

    Sorted by ``(ts_event, sequence)`` so the digest is a pure function of the
    captured book content, independent of iteration artefacts.
    """
    rows = sorted(
        (
            str(d.instrument_id),
            str(d.action),
            str(d.order.side),
            str(d.order.price),
            str(d.order.size),
            int(d.order.order_id),
            int(d.sequence),
            int(d.ts_event),
        )
        for d in deltas
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
