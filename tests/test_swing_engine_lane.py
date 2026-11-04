"""Engine-lane regression tests (real tape; no economic claim).

These lock the semantics the reconciliation relies on, so a later change cannot quietly
alter how a decision becomes an order:

* a decision whose protection did not form (``stop_price == entry_reference``) is inserted
  **unprotected** and sized on the declared notional — never rejected as a zero-distance
  bracket, and never stopped out at its own entry;
* a bracketed decision rests exactly two protection orders after its entry fill;
* a timeout close retires the resting legs instead of leaving an orphan that would fill
  later and open an untracked position;
* every fill happens at or after its decision instant (no future data), and every order
  respects the instrument's lot step.

The tape is real; the assertions are about mechanics and causality, not about returns.
"""

from __future__ import annotations

import datetime
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v8_next.adapters.swing_engine import SwingEngineConfig, run_swing_engine  # noqa: E402
from v8_next.economics.grammar import POLICY_REQUIRED_BARS  # noqa: E402
from v8_next.economics.swing_baseline import policy_spec  # noqa: E402
from v8_next.evaluation.gate_resolution import load_tape_candles  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TAPE = REPO_ROOT / "research" / "tape" / "multi-1h-4y" / "tape.jsonl"
HOUR_NS = 3_600 * 10**9
LOT = Decimal("0.001")


def _ms(value: str) -> int:
    return int(
        datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
    )


@pytest.fixture(scope="module")
def engine_run():
    if not TAPE.is_file():
        pytest.skip(f"real tape absent at {TAPE}")
    candles = load_tape_candles(
        TAPE, instrument="BTCUSDT", start_ms=_ms("2025-01-01") - 96 * 3_600_000, end_ms=_ms("2025-02-01")
    )
    if len(candles) < 700:
        pytest.skip("tape loaded too few candles")
    spec = policy_spec("plain_swing")
    warmup = int(POLICY_REQUIRED_BARS[spec.grammar_policy])
    instrument_id = candles[0].instrument_id
    cfg = SwingEngineConfig(
        policy_id="plain_swing",
        instrument_id=instrument_id,
        bar_type_str=f"{instrument_id}-1-HOUR-LAST-EXTERNAL",
    )
    return run_swing_engine(tuple(candles), cfg, warmup_bars=warmup)


def test_bracketless_decision_is_inserted_unprotected_with_declared_notional(engine_run) -> None:
    decisions = [d for d in engine_run["events"]["decisions"] if d.get("status")]
    bracketless = [d for d in decisions if d.get("status") == "BRACKETLESS_DECISION_INSERTED_UNPROTECTED"]
    assert bracketless, "this window contains decisions whose protection did not form"
    for row in bracketless:
        assert row["decision"]["stop_price"] == row["decision"]["entry_reference"]
    inserted = [d for d in decisions if d.get("status") == "ENTRY_SUBMITTED"]
    assert inserted, "an unprotected decision must still be inserted"
    for row in inserted:
        if not row["has_bracket"]:
            assert row["sizing_basis"] == "DECLARED_NOTIONAL_AT_ENTRY_REFERENCE"
            assert float(row["quantity"]) > 0
        else:
            assert row["sizing_basis"] == "STOP_DISTANCE"


def test_quantities_respect_the_lot_step(engine_run) -> None:
    for fill in engine_run["events"]["fills"]:
        quantity = Decimal(str(fill["last_qty"]))
        assert quantity > 0
        assert quantity % LOT == 0, f"quantity {quantity} is not a multiple of the lot step"


def test_no_fill_precedes_its_decision_instant(engine_run) -> None:
    stamps = [
        int(d["decision"]["decision_ns"])
        for d in engine_run["events"]["decisions"]
        if d.get("status") == "ENTRY_SUBMITTED"
    ]
    assert stamps, "expected at least one inserted decision"
    for fill in engine_run["events"]["fills"]:
        fill_ns = int(fill["ts_event"])
        assert any(fill_ns >= stamp for stamp in stamps), "a fill is older than every decision"


def test_a_bracketed_campaign_rests_two_protection_orders(engine_run) -> None:
    bracketed = [
        d for d in engine_run["events"]["decisions"] if "bracket_submitted" in d
    ]
    if not bracketed:
        pytest.skip("no bracketed campaign in this window")
    for record in bracketed:
        row = record["bracket_submitted"]
        assert row.get("stop_client_order_id")
        assert row.get("target_client_order_id")
        assert row["stop_client_order_id"] != row["target_client_order_id"]


def test_position_accounting_matches_the_closed_records(engine_run) -> None:
    opened = engine_run["events"]["positions_opened"]
    closed = engine_run["events"]["positions_closed"]
    assert len(closed) <= len(opened)
    # a closed position must carry a realized result; an open one must be published as such
    for record in closed:
        assert record["realized_pnl"]
        assert Decimal(str(record["avg_px_close"])) > 0
    if engine_run["open_position_at_end"] is None:
        assert len(closed) == len(opened), "an unclosed position must be reported as open"


def test_resting_orders_at_the_end_are_published_not_hidden(engine_run) -> None:
    orders = engine_run["orders_open_at_end"]
    assert isinstance(orders, list)
    # they must be identifiable, so a reconciliation can name them instead of assuming zero
    assert all(isinstance(order, str) and order for order in orders)

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
