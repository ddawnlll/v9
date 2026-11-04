"""Binance aggTrades pages -> Nautilus TradeTick with aggressor side.

Venue field ``m`` (isBuyerMaker): true means the buyer was resting, so the
aggressor was the SELLER. False means the buyer took liquidity: aggressor BUY.
Records with a missing/invalid ``m`` are dropped and counted, never defaulted,
because a wrong aggressor side is worse than a missing trade.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)


def aggressor_side(is_buyer_maker: Any) -> AggressorSide | None:
    """Map the venue ``m`` flag onto an aggressor side, or None when unknown."""
    if is_buyer_maker is True:
        return AggressorSide.SELL
    if is_buyer_maker is False:
        return AggressorSide.BUY
    return None


def trades_from_pages(
    directory: Path | str,
    instrument_id: str,
    price_precision: int = 2,
    size_precision: int = 3,
) -> tuple[tuple[TradeTick, ...], dict[str, int]]:
    """Load ``trades-*.json`` pages into chronological TradeTicks.

    Returns (ticks, stats) where stats counts loaded/dropped records. Page files
    are read in name order; ticks sort by (ts_event, trade id) so engine input
    is deterministic regardless of page boundaries.
    """
    d = Path(directory)
    iid = InstrumentId.from_str(instrument_id)
    ticks: list[TradeTick] = []
    stats = {"pages": 0, "records": 0, "loaded": 0, "dropped": 0}
    for page in sorted(d.glob("trades-*.json")):
        stats["pages"] += 1
        payload = json.loads(page.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for row in payload:
            stats["records"] += 1
            try:
                side = aggressor_side(row.get("m"))
                if side is None:
                    stats["dropped"] += 1
                    continue
                price = Price(float(row["p"]), price_precision)
                qty = Quantity(float(row["q"]), size_precision)
                ts_ms = int(row["T"])
                trade_id = TradeId(str(row["a"]))
            except (KeyError, TypeError, ValueError):
                stats["dropped"] += 1
                continue
            ticks.append(
                TradeTick(
                    iid,
                    price,
                    qty,
                    side,
                    trade_id,
                    ts_ms * 1_000_000,
                    ts_ms * 1_000_000,
                )
            )
            stats["loaded"] += 1
    ticks.sort(key=lambda t: (t.ts_event, t.trade_id.value))
    return tuple(ticks), stats


def trades_in_window(
    ticks: tuple[TradeTick, ...], start_ns: int, end_ns: int
) -> tuple[TradeTick, ...]:
    """Restrict ticks to [start_ns, end_ns]. No extrapolation, no filling."""
    return tuple(t for t in ticks if start_ns <= t.ts_event <= end_ns)


#: Column order of the public daily aggTrades archive dumps
#: (``data.binance.vision/data/futures/um/daily/aggTrades``). The first field is
#: also the header detection token: dumps written before 2025-01-01 have no
#: header row, and a reader that assumes one loses its first trade.
DUMP_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)
DUMP_HEADER_TOKEN = "agg_trade_id"


def _dump_rows(handle: Any) -> Iterator[Sequence[str]]:
    """Yield CSV rows of a dump, tolerating both headered and headerless files."""
    reader = csv.reader(handle)
    first = next(reader, None)
    if first is None:
        return
    if first and first[0].strip() == DUMP_HEADER_TOKEN:
        yield from reader
    else:
        yield first
        yield from reader


def _dump_flag(value: str) -> bool | None:
    """Parse the archive's ``is_buyer_maker`` text, or None when unknown."""
    text = value.strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def trades_from_dump(
    directory: Path | str,
    instrument_id: str,
    *,
    start_ns: int,
    end_ns: int,
    price_precision: int = 2,
    size_precision: int = 3,
) -> tuple[tuple[TradeTick, ...], dict[str, int]]:
    """Stream public daily aggTrades archive zips into TradeTicks in a window.

    The archive dumps are the only source that carries *historical* aggressor
    evidence (the REST endpoint returns recent trades only), so they are what a
    bar+trade run over an older tape must be measured on. Rows outside
    ``[start_ns, end_ns]`` are counted, never extrapolated into the window. A row
    whose price, quantity, id, time, or ``is_buyer_maker`` flag cannot be parsed
    exactly is dropped and counted: a guessed aggressor side would corrupt the
    matching it is supposed to drive.
    """
    d = Path(directory)
    iid = InstrumentId.from_str(instrument_id)
    ticks: list[TradeTick] = []
    stats = {
        "archives": 0,
        "files": 0,
        "rows": 0,
        "rows_in_window": 0,
        "loaded": 0,
        "dropped_unparsable": 0,
        "dropped_unknown_aggressor": 0,
        "out_of_window": 0,
    }
    for archive in sorted(d.glob("*-aggTrades-*.zip")):
        stats["archives"] += 1
        with zipfile.ZipFile(archive) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith("/") or not name.endswith(".csv"):
                    continue
                stats["files"] += 1
                with zf.open(name) as raw:
                    handle = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                    for row in _dump_rows(handle):
                        stats["rows"] += 1
                        if len(row) < len(DUMP_COLUMNS):
                            stats["dropped_unparsable"] += 1
                            continue
                        try:
                            ts_ms = int(row[5])
                        except ValueError:
                            stats["dropped_unparsable"] += 1
                            continue
                        ts_ns = ts_ms * 1_000_000
                        if ts_ns < start_ns or ts_ns > end_ns:
                            stats["out_of_window"] += 1
                            continue
                        stats["rows_in_window"] += 1
                        side = aggressor_side(_dump_flag(row[6]))
                        if side is None:
                            stats["dropped_unknown_aggressor"] += 1
                            continue
                        try:
                            price = Price(float(row[1]), price_precision)
                            qty = Quantity(float(row[2]), size_precision)
                            trade_id = TradeId(str(int(row[0])))
                        except (TypeError, ValueError):
                            stats["dropped_unparsable"] += 1
                            continue
                        ticks.append(TradeTick(iid, price, qty, side, trade_id, ts_ns, ts_ns))
                        stats["loaded"] += 1
    ticks.sort(key=lambda t: (t.ts_event, t.trade_id.value))
    return tuple(ticks), stats


def trades_digest(ticks: tuple[TradeTick, ...]) -> str:
    """Identity of a trade feed: price/qty/side/id/time per tick, sorted."""
    import hashlib as _hl
    import json as _js

    rows = sorted(
        (
            str(t.instrument_id),
            str(t.aggressor_side),
            str(t.size),
            str(t.price),
            str(t.trade_id),
            int(t.ts_event),
        )
        for t in ticks
    )
    return _hl.sha256(_js.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))
