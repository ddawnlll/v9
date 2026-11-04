"""F6 (#405): live-capture sandbox replay and its data-match report.

The upstream sandbox execution client cannot be started on this build (see
``app/sandbox_replay.SANDBOX_ENV_BLOCKER``), so the live-data + simulated-execution
deliverable is met by recording a bounded window of live public venue data and
replaying exactly those bytes through the simulated engine. These tests cover:

* **mechanics** — `data_match_report` on constructed counts: exact match, a
  declared (unclosed-bar) reduction, an undeclared reduction, over-consumption, a
  span violation and missing accounting. No data needed.
* **evaluative** — the real recorded session: what the venue sent is what the
  engine consumed, the probe order exercised the path, and any fill price is a
  price that actually printed in the recorded tape. Skips when absent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.tape_paths import tape_root
from v8_next.adapters.trade_tape import trades_from_pages
from v8_next.app.sandbox_replay import (
    SANDBOX_ENV_STATUS,
    data_match_report,
)

TAPE_ROOT = tape_root()


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def _report(**overrides: object) -> dict:
    base: dict[str, object] = {
        "captured": {"trades": 10, "deltas": 20, "bars": 5},
        "consumed": {"trades": 10, "deltas": 20, "bars": 5},
        "captured_span_ns": (100, 200),
        "consumed_span_ns": (110, 190),
    }
    base.update(overrides)
    return data_match_report(**base)  # type: ignore[arg-type]


def test_exact_match_is_reported_as_matched() -> None:
    report = _report()
    assert report["matched"] is True
    assert report["mismatches"] == []


def test_a_declared_unclosed_bar_reduction_counts_as_explained() -> None:
    report = _report(
        captured={"trades": 10, "deltas": 20, "bars": 5, "bars_unclosed_dropped": 1},
        consumed={"trades": 10, "deltas": 20, "bars": 4},
    )
    assert report["matched"] is True


def test_an_undeclared_reduction_is_a_named_mismatch() -> None:
    report = _report(consumed={"trades": 9, "deltas": 20, "bars": 5})
    assert report["matched"] is False
    assert "CONSUMED_LESS_TRADES_THAN_CAPTURED" in report["mismatches"]


def test_over_consumption_and_span_violations_are_named() -> None:
    over = _report(consumed={"trades": 11, "deltas": 20, "bars": 5})
    assert "CONSUMED_MORE_TRADES_THAN_CAPTURED" in over["mismatches"]
    span = _report(consumed_span_ns=(90, 190))
    assert "CONSUMED_SPAN_OUTSIDE_CAPTURED_SPAN" in span["mismatches"]
    missing = _report(consumed={"trades": 10, "deltas": 20})
    assert "MISSING_BARS_ACCOUNTING" in missing["mismatches"]


# --------------------------------------------------------------------------- #
# evaluative
# --------------------------------------------------------------------------- #
def _session() -> Path:
    candidates = sorted(
        p
        for p in TAPE_ROOT.glob("sandbox-replay-*")
        if (p / "replay.json").exists() and (p / "session.json").exists()
    )
    if not candidates:
        pytest.skip(f"no recorded sandbox-replay session under {TAPE_ROOT}")
    return candidates[-1]


def test_recorded_replay_consumed_exactly_what_the_venue_sent() -> None:
    root = _session()
    result = json.loads((root / "replay.json").read_text())
    session = json.loads((root / "session.json").read_text())
    match = result["data_match"]
    assert match["matched"] is True, match["mismatches"]
    assert match["consumed"]["trades"] == match["captured"]["trades"]
    assert match["consumed"]["deltas"] == match["captured"]["deltas"]
    assert (
        match["consumed"]["bars"] + match["captured"]["bars_unclosed_dropped"]
        == match["captured"]["bars"]
    )
    assert result["trades_fed"] == match["captured"]["trades"] > 0
    assert result["book_deltas_fed"] == match["captured"]["deltas"] > 0
    # The upstream sandbox client is unusable here; the artifact must say so.
    assert result["sandbox_env_status"] == SANDBOX_ENV_STATUS
    assert session["sandbox_env_status"] == SANDBOX_ENV_STATUS
    assert result["claim_status"] == "NO_ECONOMIC_CLAIM"
    print(
        f"\n[F6-replay] {root.name} bars={result['bars_fed']} "
        f"(unclosed dropped {result['bars_unclosed_dropped']}) "
        f"trades={result['trades_fed']} deltas={result['book_deltas_fed']} "
        f"fills={result['fills']} status={result['status']} matched={match['matched']}"
    )


def test_probe_order_exercised_the_simulated_execution_path() -> None:
    root = _session()
    result = json.loads((root / "replay.json").read_text())
    assert result["probe_order_sent"] is True
    assert result["status"] in {
        "REPLAY_COMPLETED_WITH_FILLS",
        "REPLAY_COMPLETED_NO_FILLS",
        "REPLAY_COMPLETED_WITH_MISMATCHES",
    }
    if not result["fills"]:
        pytest.skip("no fill on this window; fill-price identity cannot be asserted")
    printed = {str(t.price) for t in trades_from_pages(root / "trades", result["instrument_id"])[0]}
    for fill in result["fill_rows"]:
        px = Decimal(str(fill.get("px") or fill.get("avg_px") or "0"))
        inner = min(
            (abs(px - Decimal(p)) for p in printed),
            default=None,
        )
        # A simulated fill must be a price that actually printed (or one tick from
        # it, the declared slippage model) — never a price nobody traded at.
        assert inner is not None and inner <= Decimal("0.05"), (fill, sorted(printed)[:3])
    print(
        f"\n[F6-replay] fills={[(f.get('px'), f.get('liquidity_side')) for f in result['fill_rows']]} "
        f"engine_report_rows={result['engine_fill_report_rows']} "
        f"reason={result['engine_fill_report_reason']}"
    )

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
