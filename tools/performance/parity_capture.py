"""Capture decision-relevant replay output as canonical, comparable digests.

This is the baseline side of the parity gate. It replays the *unchanged*
production adapters and records everything a speed optimisation must not touch:
per-bar decisions (including every stance), order events, fills, position
lifecycle, account state, execution telemetry, native reports, the
OpportunityBook, research lifecycle records and the forensic audit stream.

Output is a manifest of SHA-256 digests — whole-section plus fixed-size chunk
digests, so a divergence localises to a bar range without committing megabytes —
with the full canonical records optionally written as JSONL alongside it.

Volatile fields (wall-clock audit stamps) are removed by
``parity_canon.VOLATILE_KEYS``; nothing decision-relevant is.

    python tools/performance/parity_capture.py --tape /tape/jan --bars 744 \
        --workload B0 --manifest evidence/parity/baseline/B0.json --details /tmp/B0
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from parity_canon import canonical, count_volatile, digest, digest_chunks, dumps  # noqa: E402

CHUNK = 64
SCHEMA = 1


def _chunked(name: str, records: list, sections: dict, chunks: dict, details: Path | None) -> None:
    """Record a per-item section as chunk digests, optionally dumping the items."""
    text = [dumps(record) for record in records]
    sections[name] = {"count": len(records), "sha256": digest(records), "chunked": True}
    chunks[name] = digest_chunks(records, CHUNK)
    if details is not None:
        target = details / f"{name}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(text) + ("\n" if text else ""), encoding="utf-8")


def _whole(name: str, value: object, sections: dict) -> None:
    sections[name] = {
        "count": len(value) if isinstance(value, (list, tuple, dict)) else None,
        "sha256": digest(value),
        "chunked": False,
    }


def _book_records(book: object) -> list:
    all_records = getattr(book, "all", None)
    if callable(all_records):
        return [canonical(record) for record in all_records()]
    return []


def _audit_events(events_path: Path) -> list:
    if not events_path.is_file():
        return []
    events = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _single(tape: Path, bars: int, instrument: str, audit_path: Path) -> tuple[dict, list]:
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
    from v8_next.evaluation.gate_resolution import load_tape_candles

    candles = tuple(load_tape_candles(tape, limit=bars, instrument=instrument))
    if not candles:
        raise ValueError(f"no candles for {instrument} in {tape}")
    iid = f"{instrument}-PERP.BINANCE"
    config = ExpertStrategyConfig(instrument_id=iid, bar_type_str=f"{iid}-1-HOUR-LAST-EXTERNAL")
    from v8_next.audit.forensic import AuditRecorder

    recorder = AuditRecorder(run_id="parity", events_path=audit_path)
    try:
        result = run_expert_strategy_backtest(candles, config, audit_recorder=recorder)
    finally:
        recorder.close()
    return {"candles": len(candles), "result": result}, _audit_events(audit_path)


def _portfolio(tape: Path, bars: int, symbols: list[str], with_funding: bool) -> tuple[dict, list]:
    from v8_next.adapters import portfolio_backtest
    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest
    from v8_next.evaluation.gate_resolution import load_tape_candles

    legs: dict[str, tuple] = {}
    for symbol in symbols:
        leg = tuple(load_tape_candles(tape, limit=bars, instrument=symbol))
        if not leg:
            raise ValueError(f"no candles for {symbol} in {tape}")
        legs[symbol] = leg
    funding: tuple = ()
    if with_funding:
        from portfolio_tape import _load_funding

        first = min(leg[0].start_ns for leg in legs.values())
        last = max(leg[-1].end_ns for leg in legs.values())
        funding = tuple(_load_funding(tape, symbols, first // 1_000_000, last // 1_000_000)[0])
    # The portfolio adapter returns aggregated counts, not the per-leg decision
    # stream: it consumes ``strat.decisions`` for telemetry and drops the rows.
    # Capture registers the strategy instances the adapter builds (observation
    # only -- no behaviour is touched, the same object is constructed) so the
    # parity gate can compare every stance of every leg.
    strategy_cls = portfolio_backtest.ExpertEnsembleStrategy
    original_init = strategy_cls.__init__
    instances: list = []

    def _recording_init(self, *init_args, **init_kwargs):
        original_init(self, *init_args, **init_kwargs)
        instances.append(self)

    strategy_cls.__init__ = _recording_init
    try:
        result = run_portfolio_backtest(
            legs,
            (SleeveSpec("parity-replay", 1, 28, 1.0),),
            funding,
            execution_profile="baseline",
        )
    finally:
        strategy_cls.__init__ = original_init

    ordered = sorted(instances, key=lambda strat: str(strat.instrument_id))
    result["decisions"] = [
        {**decision, "instrument_id": str(strat.instrument_id)}
        for strat in ordered
        for decision in strat.decisions
    ]
    result["opportunity_book"] = [
        record
        for strat in ordered
        for record in strat.opportunity_book.all()
    ]
    return {
        "candles": sum(len(leg) for leg in legs.values()),
        "per_leg": {symbol: len(leg) for symbol, leg in legs.items()},
        "funding_rows": len(funding),
        "result": result,
    }, []


def build_sections(
    result: dict, audit: list, details: Path | None = None
) -> tuple[dict, dict]:
    """Split a replay result into digestible sections (chunked where per-bar)."""
    sections: dict[str, dict] = {}
    chunks: dict[str, list[str]] = {}
    for name in ("decisions", "order_events", "opened_positions", "closed_positions", "fill_records"):
        if isinstance(result.get(name), list):
            _chunked(name, result[name], sections, chunks, details)
    handled = {"decisions", "order_events", "opened_positions", "closed_positions", "fill_records"}
    for name, value in result.items():
        if name in handled:
            continue
        if name == "opportunity_book":
            _chunked("opportunity_book", _book_records(value), sections, chunks, details)
        else:
            _whole(name, canonical(value), sections)
    if audit:
        _chunked("audit_events", audit, sections, chunks, details)
    return sections, chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", type=Path, required=True, help="isolated monthly-archive directory")
    parser.add_argument("--bars", type=int, required=True, help="candles per instrument")
    parser.add_argument("--workload", required=True, help="label recorded in the manifest, e.g. B1")
    parser.add_argument("--instrument", default="BTCUSDT", help="single-instrument symbol")
    parser.add_argument("--symbols", help="comma-separated symbols; selects the portfolio path")
    parser.add_argument("--with-funding", action="store_true", help="portfolio path only")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--details", type=Path, help="write canonical JSONL for chunked sections")
    args = parser.parse_args()
    if args.bars < 1:
        parser.error("--bars must be positive")

    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "events.jsonl"
        if args.symbols:
            symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
            if not symbols:
                parser.error("--symbols named no instruments")
            meta, audit = _portfolio(args.tape, args.bars, symbols, args.with_funding)
            mode = "portfolio"
        else:
            meta, audit = _single(args.tape, args.bars, args.instrument, audit_path)
            symbols, mode = [args.instrument], "single"
    replayed = time.perf_counter()

    result = meta.pop("result")
    sections, chunks = build_sections(result, audit, args.details)

    usage = resource.getrusage(resource.RUSAGE_SELF)
    manifest = {
        "schema": SCHEMA,
        "workload": args.workload,
        "mode": mode,
        "symbols": symbols,
        "bars_requested": args.bars,
        "chunk_size": CHUNK,
        "meta": canonical(meta),
        "sections": sections,
        "chunks": chunks,
        "volatile_keys": count_volatile({"result": result, "audit": audit}),
        "capture": {
            "replay_s": round(replayed - started, 4),
            "peak_rss_bytes": usage.ru_maxrss,
        },
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {name: section["sha256"][:16] for name, section in sections.items()}
    print(json.dumps({"workload": args.workload, "sections": summary,
                      "replay_s": manifest["capture"]["replay_s"]}, indent=2))


if __name__ == "__main__":
    main()
