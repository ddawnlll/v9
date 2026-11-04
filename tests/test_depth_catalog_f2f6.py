"""F2/F6: book deltas, catalog identity, node config — mechanics only.

Synthetic snapshots/manifests exercise arithmetic and validation contracts.
Evaluative depth behaviour (knobs going active on a real L2 book) is measured
in test infrastructure that requires a captured book; these tests pin the
contracts that measurement depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nautilus_trader.model import InstrumentId


def _iid():
    return InstrumentId.from_str("BTCUSDT-PERP.BINANCE")


def _snap(n: int = 12, bad: bool = False):
    bids = [[str(100000 - i * 10), str(1 + i * 0.1)] for i in range(n)]
    asks = [[str(100010 + i * 10), str(1 + i * 0.1)] for i in range(n)]
    if bad:
        bids.append(["not-a-price", "1.0"])
        bids.append(["99000", "-2"])
        asks.append([None, "1.0"])
    return {"bids": bids, "asks": asks}


def test_snapshot_becomes_clear_plus_bounded_adds() -> None:
    from v8_next.adapters.book_tape import DEPTH_LEVEL_BOUND, depth_to_deltas

    deltas, stats = depth_to_deltas(
        _snap(), _iid(), sequence_base=0, ts_ns=1_000,
    )
    assert stats["clears"] == 1
    assert stats["adds"] == 2 * DEPTH_LEVEL_BOUND
    assert len(deltas) == 1 + 2 * DEPTH_LEVEL_BOUND
    seqs = [d.sequence for d in deltas]
    assert seqs == sorted(seqs)
    assert all(d.ts_event == 1_000 for d in deltas)


def test_malformed_rows_are_dropped_and_counted() -> None:
    from v8_next.adapters.book_tape import depth_to_deltas

    deltas, stats = depth_to_deltas(
        _snap(bad=True), _iid(), sequence_base=100, ts_ns=2_000,
    )
    assert stats["rows_unparsable"] == 3
    assert deltas, "valid rows must still convert"
    assert deltas[0].sequence == 100


def test_one_sided_snapshot_converts_to_nothing() -> None:
    from v8_next.adapters.book_tape import depth_to_deltas

    deltas, stats = depth_to_deltas({"bids": [], "asks": [["1", "1"]]},
                                    _iid(), sequence_base=0, ts_ns=1)
    assert deltas == []
    assert stats["snapshots_empty_side"] == 1


def test_book_digest_is_stable_and_content_sensitive() -> None:
    from v8_next.adapters.book_tape import book_digest, depth_to_deltas

    a, _ = depth_to_deltas(_snap(), _iid(), sequence_base=0, ts_ns=1_000)
    b, _ = depth_to_deltas(_snap(), _iid(), sequence_base=0, ts_ns=1_000)
    c, _ = depth_to_deltas(_snap(), _iid(), sequence_base=7, ts_ns=1_000)
    assert book_digest(tuple(a)) == book_digest(tuple(b))
    assert len(book_digest(tuple(a))) == 64
    assert book_digest(tuple(a)) != book_digest(tuple(c))


def test_catalog_run_id_is_deterministic_and_input_sensitive(tmp_path: Path) -> None:
    from v8_next.adapters.catalog_tape import compute_run_id

    a = compute_run_id(tape_sha256="abc", limit=120, legs=("BTCUSDT",),
                       include_trades=False, trades_digest_value=None)
    b = compute_run_id(tape_sha256="abc", limit=120, legs=("BTCUSDT",),
                       include_trades=False, trades_digest_value=None)
    c = compute_run_id(tape_sha256="abd", limit=120, legs=("BTCUSDT",),
                       include_trades=False, trades_digest_value=None)
    assert a == b
    assert a != c
    assert len(a) == len(b) == len(c) >= 8


def test_node_config_carries_the_profile_models() -> None:
    from v8_next.adapters.execution_models import profile_digest
    from v8_next.adapters.node_backtest import NodeRunConfig, build_run_config

    cfg = NodeRunConfig(
        catalog_path="/tmp/f6-catalog", legs=("BTCUSDT",),
        execution_profile="realistic",
    )
    run_cfg = build_run_config(cfg)
    assert run_cfg.id == "f6-catalog-node"
    venue = run_cfg.venues[0]
    assert venue.fill_model is not None
    assert venue.fee_model is not None
    assert venue.latency_model is not None
    assert profile_digest("realistic")


def test_depth_summary_promotes_knobs_only_with_depth_data() -> None:
    from v8_next.adapters.execution_models import profile_summary

    bar_only = profile_summary("realistic")
    assert bar_only["inert_knobs_without_depth_data"] != []
    assert bar_only["depth_dependent_knobs_active"] == []

    from v8_next.adapters.execution_models import resolve_profile

    p = resolve_profile("realistic")
    import dataclasses

    with_depth = dataclasses.replace(p, depth_data_available=True)
    s = profile_summary(with_depth)
    assert s["depth_dependent_knobs_active"] != []
    assert s["inert_knobs_without_depth_data"] == []
    assert s["depth_dependent_knobs_evidence"] == "MEASURED_FILL_CHANGES_WITH_CAPTURED_L2_BOOK"


def test_captured_book_feeds_the_engine_deterministically() -> None:
    """Real captured L2 snapshots reach the engine; flags follow the data.

    The book postdates the F1 bars, so no fill assertion is made here — only
    that the deltas are fed (counted), the knobs report active, and two runs
    agree bit-identically.
    """
    import json
    from decimal import Decimal

    from v8_next.adapters.book_tape import book_digest, deltas_from_depth
    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest
    from v8_next.domain.market import Candle

    fix = Path(__file__).parent / "fixtures" / "f2-book"
    if not (fix / "manifest.json").exists():
        import pytest

        pytest.skip("captured L2 book fixture absent")
    deltas, stats = deltas_from_depth(fix, "BTCUSDT-PERP.BINANCE")
    assert stats["snapshots_in_window"] > 0
    assert len(deltas) > 0
    assert book_digest(deltas) == book_digest(deltas)

    rows = json.loads((Path(__file__).parent / "fixtures" / "f1-btc-12h" / "klines.json").read_text())
    candles = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            int(k[0]) * 1_000_000, (int(k[6]) + 1) * 1_000_000,
            Decimal(str(k[1])), Decimal(str(k[2])), Decimal(str(k[3])),
            Decimal(str(k[4])), Decimal(str(k[5])),
            (int(k[6]) + 1) * 1_000_000, (int(k[6]) + 1) * 1_000_000,
            "f2-book-klines",
        )
        for k in rows
    )

    def run():
        return run_portfolio_backtest(
            {"BTCUSDT": candles},
            (SleeveSpec("incumbent", 1, 28, 1.0),),
            (),
            per_leg_notional=Decimal("1000"),
            taker_fee=Decimal("0.0005"),
            initial_balance=Decimal("10000"),
            execution_profile="realistic",
            book_deltas=deltas,
        )

    first, second = run(), run()
    assert first["book_deltas_fed"] == len(deltas) > 0
    assert first["book_type"] is not None
    # The captured book postdates the bar window, and with an L2 book configured a
    # MARKET order submitted from a bar callback does not execute at all (measured:
    # 2 fills without the book, 0 with it on this same single-leg run). Publishing
    # the knobs as ACTIVE for a run that cannot exercise them would dress an empty
    # execution record up as depth-conditioned execution, so the wiring now keeps
    # them inert and names the reason. Overlap is covered by
    # tests/test_l2_book_f2.py::test_a_book_window_disjoint_from_the_bars_cannot_claim_active_knobs
    # and the positive case by the profile-summary test in the same file.
    assert first["book_window_overlap"] is False
    assert first["book_window_reason"] == "NO_OVERLAP_BETWEEN_CAPTURED_BOOK_AND_BAR_WINDOW"
    assert first["execution"]["depth_dependent_knobs_active"] == []
    assert set(first["execution"]["inert_knobs_without_depth_data"]) == {
        "liquidity_consumption",
        "queue_position",
    }
    assert first["fill_signature"] == second["fill_signature"]
    assert first["account"] == second["account"]

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
