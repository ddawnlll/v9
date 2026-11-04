"""F6 (#405): parquet catalog + BacktestNode parity + a second venue's data.

Evidence classes:

* **mechanics** — run-id determinism, the explicit OKX -> Nautilus instrument
  mapping, and the raw-payload row accounting, on synthetic payloads written in
  ``tmp_path``. No tape, no evaluation.
* **evaluative** — the real quad tape is written to a ``ParquetDataCatalog`` and
  the same portfolio backtest is then executed through ``BacktestNode`` +
  ``BacktestRunConfig`` + ``BacktestDataConfig``. The assertion is bit-level
  parity of the fills against the direct-engine path (``run_portfolio_backtest``),
  not "it ran without error". Skips when the tape is absent.

The parity claim is the whole point of the phase: if the node path cannot
reproduce the direct path, the divergence must be reported, never papered over.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from v8_next.adapters.catalog_tape import (
    build_catalog,
    compute_run_id,
    load_bars,
)
from v8_next.adapters.node_backtest import NodeRunConfig, run_node_backtest
from v8_next.adapters.okx_capture import (
    candle_stats,
    capture_summary,
    instrument_mapping,
    nautilus_instrument_id,
    trade_stats,
)
from v8_next.adapters.okx_capture import (
    validate_capture as validate_okx_capture,
)
from v8_next.adapters.portfolio_backtest import (
    SleeveSpec,
    run_portfolio_backtest,
    trade_signature,
)
from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.multitape import load_multitape

QUAD_TAPE = tape_path("quad-1h-12m")
OKX_CAPTURE = tape_path("okx-btcusdt-swap-rest-20260911T001813Z")
BARS = 120
SLEEVES = (SleeveSpec("incumbent", 1, 28, 1.0),)
CATALOG_ROOT = Path("artifacts/catalog-f6")


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_run_id_is_deterministic_and_sensitive_to_its_inputs() -> None:
    base = dict(
        tape_sha256="a" * 64,
        limit=120,
        legs=("BTCUSDT", "ETHUSDT"),
        include_trades=False,
        trades_digest_value=None,
    )
    first = compute_run_id(**base)  # type: ignore[arg-type]
    assert compute_run_id(**base) == first  # type: ignore[arg-type]
    assert compute_run_id(**{**base, "limit": 121}) != first  # type: ignore[arg-type]
    assert compute_run_id(**{**base, "legs": ("BTCUSDT",)}) != first  # type: ignore[arg-type]
    assert (
        compute_run_id(**{**base, "include_trades": True, "trades_digest_value": "w:1:2"}) != first
    )  # type: ignore[arg-type]


def test_okx_instrument_mapping_is_explicit_and_rejects_unknown_shapes() -> None:
    assert nautilus_instrument_id("BTC-USDT-SWAP") == "BTCUSDT-PERP.OKX"
    assert nautilus_instrument_id("ETH-USDT-SWAP") == "ETHUSDT-PERP.OKX"
    with pytest.raises(ValueError):
        nautilus_instrument_id("BTC-USDT")
    mapping = instrument_mapping(
        {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "instType": "SWAP",
                    "ctType": "linear",
                    "ctVal": "0.01",
                    "ctValCcy": "BTC",
                    "ctMult": "1",
                    "tickSz": "0.1",
                    "lotSz": "0.01",
                    "minSz": "0.01",
                }
            ],
        }
    )
    assert mapping == [
        {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "inst_type": "SWAP",
            "ct_type": "linear",
            "ct_val": "0.01",
            "ct_val_ccy": "BTC",
            "ct_mult": "1",
            "tick_sz": "0.1",
            "lot_sz": "0.01",
            "min_sz": "0.01",
            "nautilus_instrument_id": "BTCUSDT-PERP.OKX",
        }
    ]


def test_okx_payload_stats_count_rows_without_inventing_them() -> None:
    candles = candle_stats(
        {"data": [[3000, "1", "2", "0.5", "1.5", "10"], [1000, "1", "2", "0.5", "1.5", "10"]]}
    )
    assert candles == {"rows": 2, "first_ts_ms": 1000, "last_ts_ms": 3000}
    trades = trade_stats({"data": [{"tradeId": "9", "ts": "500"}, {"tradeId": "3", "ts": "100"}]})
    assert trades["rows"] == 2 and trades["min_trade_id"] == 3 and trades["max_trade_id"] == 9
    with pytest.raises(ValueError):
        candle_stats({"nope": []})
    with pytest.raises(ValueError):
        candle_stats({"data": ["not-a-row"]})


# --------------------------------------------------------------------------- #
# evaluative: real tape through the catalog and the node
# --------------------------------------------------------------------------- #
def _tape():
    if not (QUAD_TAPE / "tape.jsonl").exists():
        pytest.skip(f"real quad tape absent at {QUAD_TAPE}")
    return load_multitape(QUAD_TAPE, limit=BARS)


def _direct(tape: Any) -> dict[str, Any]:
    return run_portfolio_backtest(
        tape.candles,
        SLEEVES,
        tape.funding,
        per_leg_notional=Decimal("1000"),
        taker_fee=Decimal("0.0005"),
        initial_balance=Decimal("10000"),
        funding_dropped=tape.funding_dropped,
        execution_profile="realistic",
    )


def _node(build: Any, tape: Any, run_config_id: str = "f6-test") -> dict[str, Any]:
    legs = tuple(sorted(tape.candles))
    cfg = NodeRunConfig(
        catalog_path=build.catalog_path,
        run_config_id=run_config_id,
        legs=legs,
        venue="BINANCE",
        execution_profile="realistic",
    )
    return run_node_backtest(cfg, {raw: tuple(tape.candles[raw]) for raw in legs}, SLEEVES)


def test_catalog_inventory_matches_the_source_tape() -> None:
    tape = _tape()
    build = build_catalog(CATALOG_ROOT, tape_path=QUAD_TAPE, limit=BARS)
    legs = tuple(sorted(tape.candles))
    bars = build.inventory["per_type"]["bars"]
    assert bars["rows"] == BARS * len(legs)
    # bars are written with their CLOSE time as ts_event (the instant the bar is
    # knowable), so the catalog window is the tape's end_ns span.
    expected_first = min(c.end_ns for leg in tape.candles.values() for c in leg)
    expected_last = max(c.end_ns for leg in tape.candles.values() for c in leg)
    assert bars["first_ts_event_ns"] == expected_first
    assert bars["last_ts_event_ns"] == expected_last
    assert set(bars["identifiers"]) == {f"{raw}-PERP.BINANCE-1-HOUR-LAST-EXTERNAL" for raw in legs}
    # and the catalog really answers queries for what it claims to hold
    read_back = load_bars(
        build.catalog_path,
        tuple(f"{raw}-PERP.BINANCE-1-HOUR-LAST-EXTERNAL" for raw in legs),
    )
    assert len(read_back) == BARS * len(legs)
    print(
        f"\n[F6] catalog run_id={build.run_id} bars={bars['rows']} "
        f"build_wall_s={build.wall_time_s} types={build.inventory['data_types']}"
    )


def test_catalog_build_is_deterministic_for_the_same_run_id() -> None:
    first = build_catalog(CATALOG_ROOT, tape_path=QUAD_TAPE, limit=BARS)
    second = build_catalog(CATALOG_ROOT, tape_path=QUAD_TAPE, limit=BARS)
    assert first.run_id == second.run_id
    assert first.inventory == second.inventory


def test_node_run_reproduces_the_direct_engine_fills() -> None:
    """The catalog/node path must not silently change the result it reproduces."""
    tape = _tape()
    build = build_catalog(CATALOG_ROOT, tape_path=QUAD_TAPE, limit=BARS)
    direct = _direct(tape)
    node = _node(build, tape)

    assert direct["execution"]["fills_count"] > 0, "window produced no fills to compare"
    assert node["fill_signature"] == direct["execution"]["fill_signature"]
    assert len(node["fill_records"]) == direct["execution"]["fills_count"]
    assert node["opened_positions"] == direct["opened_positions"]
    print(
        f"\n[F6] node_wall_s={node['wall_time_s']} direct_fills="
        f"{direct['execution']['fills_count']} signature={node['fill_signature'][:16]} "
        f"trade_signature={trade_signature(direct)[:16]} "
        f"attachment={node['strategy_attachment']} "
        f"funding_support={node['funding_catalog_support']}"
    )


def test_node_run_is_deterministic_across_two_executions() -> None:
    tape = _tape()
    build = build_catalog(CATALOG_ROOT, tape_path=QUAD_TAPE, limit=BARS)
    first = _node(build, tape, "f6-det-a")
    second = _node(build, tape, "f6-det-b")
    assert first["fill_signature"] == second["fill_signature"]
    assert first["opened_positions"] == second["opened_positions"]
    assert first["catalog_inventory_digest"] == second["catalog_inventory_digest"]


def test_second_venue_capture_is_verifiable() -> None:
    if not (OKX_CAPTURE / "manifest.json").exists():
        pytest.skip(f"venue #2 capture absent at {OKX_CAPTURE}")
    validate_okx_capture(OKX_CAPTURE / "manifest.json")
    summary = capture_summary(OKX_CAPTURE / "manifest.json")
    assert summary["venue"] == "OKX"
    assert summary["claim_status"] == "NO_ECONOMIC_CLAIM"
    assert summary["candle_rows"] > 0 and summary["trade_rows"] > 0
    assert summary["nautilus_instrument_id"] == "BTCUSDT-PERP.OKX"
    assert summary["candles_first_ts_ms"] < summary["candles_last_ts_ms"]
    print(
        f"\n[F6] venue2=OKX inst={summary['nautilus_instrument_id']} "
        f"candles={summary['candle_rows']} trades={summary['trade_rows']} "
        f"window={summary['candles_first_ts_ms']}..{summary['candles_last_ts_ms']}"
    )

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
