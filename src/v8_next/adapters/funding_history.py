"""Raw bounded funding pagination; receipt clocks belong to individual pages.

Quad funding stream lives in research/tape/quad-1h-12m/tape.jsonl (channel=funding)
and is fed to the Nautilus engine as MarkPriceUpdate + FundingRateUpdate per
in-window settlement. See docs/contracts/SHADOW_LIVE_DATA_SPEC.md and
v8_next/evaluation/multitape.py for the quad tape format.
"""

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


def capture_pages(
    destination: Path, symbol: str, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= end_ms:
        # Bound work, not evidence: reaching this cap fails without a manifest.
        if len(artifacts) >= 100:
            raise ValueError("funding pagination request budget exhausted")
        url = "https://fapi.binance.com/fapi/v1/fundingRate?" + urlencode(
            dict(symbol=symbol, startTime=cursor, endTime=end_ms, limit=1000)
        )
        requested = time.time_ns()
        with urlopen(url, timeout=30) as response:
            raw = response.read()
        received = time.time_ns()
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("invalid funding page")
        times = [row["fundingTime"] for row in rows]
        if (
            any(type(t) is not int for t in times)
            or times != sorted(set(times))
            or any(
                row["symbol"] != symbol or not cursor <= t <= end_ms
                for row, t in zip(rows, times, strict=True)
            )
        ):
            raise ValueError("funding page outside requested chronology")
        name = "funding.json" if not artifacts else f"funding-page-{len(artifacts):03d}.json"
        (destination / name).write_bytes(raw)
        artifacts.append(
            dict(
                path=name,
                sha256=hashlib.sha256(raw).hexdigest(),
                source_url=url,
                request_time_ns=requested,
                received_time_ns=received,
                historical_available_time_ns=None,
                historical_pit_status="UNKNOWN",
            )
        )
        if len(rows) < 1000:
            break
        cursor = times[-1] + 1
    return artifacts


def funding_artifacts(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        a
        for a in metadata["artifacts"]
        if a["path"] == "funding.json"
        or (a["path"].startswith("funding-page-") and a["path"].endswith(".json"))
    ]


def validate_page_chain(root: Path, metadata: dict[str, Any]) -> None:
    """Validate multi-page query continuity; hashes are checked by the caller."""
    from urllib.parse import parse_qs, urlsplit

    pages = funding_artifacts(metadata)
    if len(pages) <= 1:
        return  # Legacy single responses retain their existing validation.
    cursor = None
    final_end = None
    last_received = 0
    for i, page in enumerate(pages):
        expected = "funding.json" if i == 0 else f"funding-page-{i:03d}.json"
        url = urlsplit(page["source_url"])
        query = parse_qs(url.query)
        if (
            page["path"] != expected
            or (url.scheme, url.netloc, url.path)
            != ("https", "fapi.binance.com", "/fapi/v1/fundingRate")
            or query.get("symbol") != [metadata["symbol"]]
        ):
            raise ValueError("invalid funding page source sequence")
        if any(len(query.get(k, [])) != 1 for k in ("startTime", "endTime", "limit")):
            raise ValueError("invalid funding pagination query")
        start, end, limit = (int(query[k][0]) for k in ("startTime", "endTime", "limit"))
        requested, received = page["request_time_ns"], page["received_time_ns"]
        if not 0 <= start <= end or limit != 1000 or not end * 10**6 <= requested <= received:
            raise ValueError("invalid funding pagination bounds or clocks")
        if i and (start != cursor or end != final_end or requested < last_received):
            raise ValueError("discontinuous funding pagination")
        rows = json.loads((root / expected).read_text())
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("invalid funding page payload")
        times = [row["fundingTime"] for row in rows]
        if (
            any(type(t) is not int for t in times)
            or times != sorted(set(times))
            or any(
                row["symbol"] != metadata["symbol"] or not start <= t <= end
                for row, t in zip(rows, times, strict=True)
            )
        ):
            raise ValueError("invalid funding page chronology")
        if i < len(pages) - 1 and (len(rows) != limit or times[-1] == end):
            raise ValueError("funding pagination continued after terminal page")
        if i == len(pages) - 1 and len(rows) == limit and times[-1] < end:
            raise ValueError("incomplete funding pagination")
        cursor = times[-1] + 1 if times else None
        final_end, last_received = end, received


# ── Quad tape funding (real venue records bundled in quad-1h-12m) ──────────

def _scan_quad_funding(p: Path) -> tuple[list[dict[str, Any]], int, int]:
    """Shared scan: (parsed rows, dropped count, raw funding records)."""
    import polars as pl

    from v8_next.evaluation.multitape import funding_interval_hours

    df = pl.read_ndjson(p)
    if "funding" not in df.get_column("channel").unique().to_list():
        return [], 0, 0
    sub = df.filter(pl.col("channel") == "funding")
    instruments = sub.get_column("instrument").to_list()
    payloads = sub.get_column("payload").to_list()
    raw_count = len(payloads)
    dropped = 0
    out: list[dict[str, Any]] = []
    for inst, pay in zip(instruments, payloads, strict=True):
        try:
            rate = pay.get("funding_rate") if "funding_rate" in pay else pay.get("fundingRate")
            t_ms = pay.get("funding_time_ms") if "funding_time_ms" in pay else pay.get("fundingTime")
            if rate is None or t_ms is None:
                dropped += 1
                continue
            out.append(
                {
                    "instrument": str(inst or pay.get("instrument") or ""),
                    "funding_time_ms": int(t_ms),
                    "funding_rate": Decimal(str(rate)),
                    "interval_hours": funding_interval_hours(pay),
                    "mark_price": pay.get("markPrice"),
                    "payload_hash": pay.get("payload_hash"),
                }
            )
        except (ValueError, TypeError):
            # Never silently free: every skipped record is counted.
            dropped += 1
    return out, dropped, raw_count


def quad_funding_rows(
    tape_path: Path | str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read real funding records from the quad tape.

    Format: research/tape/quad-1h-12m/tape.jsonl, JSONL with channel==funding.
    Source: Binance USD-M GET /fapi/v1/fundingRate (fundingRate + markPrice)
    bundled per bar by the capture job. Not paginated REST here.

    Malformed records are skipped but COUNTED (see quad_funding_summary);
    accounting consumers must check the dropped count, never assume zero.
    """
    p = Path(tape_path)
    if p.is_dir():
        p = p / "tape.jsonl"
    if not p.is_file():
        raise FileNotFoundError(f"quad tape not found at {p}")

    rows, _dropped, _raw = _scan_quad_funding(p)
    if limit is not None:
        rows = rows[:limit]
    return rows


def quad_funding_summary(
    tape_path: Path | str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Summarise quad funding: counts, span, per-instrument sample.

    Returns dict with mode + source documentation for inaccessible case.
    """
    p = Path(tape_path)
    if p.is_dir():
        p = p / "tape.jsonl"
    if not p.is_file():
        return {
            "mode": "FUNDING_TAPE_MISSING",
            "reason": f"no file at {p}",
            "format": "tape.jsonl channel==funding {funding_rate, funding_time_ms, interval_hours}",
            "source": "Binance USD-M GET /fapi/v1/fundingRate bundled in research/tape/quad-1h-12m/tape.jsonl",
            "command": f"cat {p} | grep '\"channel\":\"funding\"' | wc -l  # public; no auth",
            "expected_command": f"python3 -c \"from v8_next.adapters.funding_history import quad_funding_summary; print(quad_funding_summary('{p}'))\"",
        }
    rows_all, dropped, raw_count = _scan_quad_funding(p)
    rows = rows_all[:limit] if limit is not None else rows_all
    by_inst: dict[str, int] = {}
    for r in rows:
        by_inst[r["instrument"]] = by_inst.get(r["instrument"], 0) + 1
    times = sorted(r["funding_time_ms"] for r in rows) if rows else []
    return {
        "mode": "QUAD_FUNDING_PRESENT",
        "tape_path": str(p),
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16] + "...",
        "total_rows": len(rows),
        "raw_funding_records": raw_count,
        "dropped_malformed": dropped,
        "instruments": by_inst,
        "time_span_ms": [times[0], times[-1]] if times else [],
        "interval_hours": sorted({r["interval_hours"] for r in rows}),
        "sample": rows[:2] if rows else [],
        "format": "tape.jsonl channel==funding {instrument, funding_time_ms, funding_rate, interval_hours, markPrice}",
        "source": "Binance USD-M GET /fapi/v1/fundingRate (public, per 8h settlement)",
        "command": "cat research/tape/quad-1h-12m/tape.jsonl | grep '\"channel\":\"funding\"' | wc -l",
        "engine_feed": "FundingRow -> MarkPriceUpdate+FundingRateUpdate per in-window settlement (see portfolio_backtest.run_portfolio_backtest and evaluation/multitape.load_multitape)",
        "limit": limit,
    }


def quad_funding_to_tape_rows(tape_path: Path | str) -> tuple[Any, ...]:
    """Convert quad tape funding records to evaluation.FundingRow for engine feed."""
    from v8_next.evaluation.multitape import FundingRow

    rows = quad_funding_rows(tape_path)
    return tuple(
        FundingRow(
            instrument=r["instrument"],
            funding_time_ms=r["funding_time_ms"],
            funding_rate=Decimal(str(r["funding_rate"])),
            interval_hours=float(r["interval_hours"]),
        )
        for r in rows
        if r["instrument"]
    )
