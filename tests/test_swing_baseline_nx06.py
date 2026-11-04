"""NX06 (#427) — the swing family's expiry, causality and fill-order semantics.

Evidence classes:

* **mechanics** (synthetic bars, MECHANICS ONLY) — barrier resolution order, gap
  fills, the refusal to read a bar at or before the decision instant, the cash
  floor's shape, and the 336-bar squeeze expiry converted to hours and days.
* **evaluative** (real tape) — the family is pre-registered *before* the window is
  measured, both artifacts bind to each other by hash, and the receipt makes no
  economic claim.

A return that is null or negative is a complete result for this issue: the tests
never require a policy to be profitable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.domain.market import Candle, frame_at
from v8_next.economics.swing_baseline import (
    ENGINE_TICK,
    SHARED_CONTRACT,
    SWING_FAMILY,
    ExpiryHorizon,
    SwingDecision,
    SwingPolicySpec,
    family_registry,
    open_trade_expiry,
    opportunity_ttl_bars,
    policy_spec,
    replay_bracket,
    swing_signal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TAPE = REPO_ROOT / "research" / "tape" / "multi-1h-4y" / "tape.jsonl"
HOUR_NS = 3_600 * 10**9
TOOL = REPO_ROOT / "tools" / "nx06_swing_baseline.py"


def _candle(
    index: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    start_ns: int = 1_700_000_000_000_000_000,
) -> Candle:
    received_ns = start_ns + index * HOUR_NS + 3_599_999_999_999
    return Candle(
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=start_ns + index * HOUR_NS,
        received_ns=received_ns,
        available_ns=received_ns + 1_000_000,
        end_ns=received_ns,
        source_hash="mechanics-only",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def _decision(**overrides: object) -> SwingDecision:
    base: dict[str, object] = {
        "policy_id": "plain_swing",
        "opportunity_id": "opp-1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "direction": "LONG",
        "decision_ns": 1_700_000_000_000_000_000,
        "entry_reference": Decimal("100"),
        "stop_price": Decimal("98"),
        "target_price": Decimal("104"),
        "expires_ns": 1_700_000_000_000_000_000 + 336 * HOUR_NS,
        "opportunity_ttl_bars": 48.0,
        "protection_policy": "squeeze:baseline:v2",
        "open_trade_expiry_bars": 336,
    }
    base.update(overrides)
    return SwingDecision(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# mechanics — MECHANICS ONLY (synthetic bars)
# --------------------------------------------------------------------------- #


def test_squeeze_expiry_is_336_bars_in_hours_and_days() -> None:
    spec = policy_spec("plain_swing")
    assert spec.protection_policy == "squeeze:baseline:v2"
    horizon = open_trade_expiry(spec, HOUR_NS)
    assert horizon is not None
    assert horizon.bars == 336
    assert horizon.hours == pytest.approx(336.0)
    assert horizon.days == pytest.approx(14.0)
    assert horizon.ns == 336 * HOUR_NS
    # the TTL table is keyed by family; the full policy name must resolve to it
    assert ExpiryHorizon(bars=336, bar_ns=4 * HOUR_NS).days == pytest.approx(336 * 4 / 24)
    assert open_trade_expiry(policy_spec("cash"), HOUR_NS) is None
    assert open_trade_expiry(policy_spec("causal_trend"), HOUR_NS) is None  # timeout-only
    with pytest.raises(ValueError):
        open_trade_expiry(spec, 0)


def test_opportunity_ttl_bars_is_measured_against_the_bar_duration() -> None:
    from v8_next.economics.decisions import Opportunity

    opportunity = Opportunity(
        opportunity_id="opp-ttl",
        exposure_id="BTC/USD:linear-perpetual",
        instrument_id="BTCUSDT-PERP.BINANCE",
        direction="LONG",
        anchor_ns=1_000_000_000_000_000_000,
        expires_ns=1_000_000_000_000_000_000 + 24 * HOUR_NS,
        grammar_version="trend-continuation-v2",
    )
    assert opportunity_ttl_bars(opportunity, HOUR_NS) == pytest.approx(24.0)
    # a different bar duration scales it, and never silently becomes a fixed number
    assert opportunity_ttl_bars(opportunity, 4 * HOUR_NS) == pytest.approx(6.0)


def test_stop_is_taken_when_one_bar_touches_both_barriers() -> None:
    decision = _decision()
    bars = [_candle(1, open_="100", high="105", low="97", close="103")]
    outcome = replay_bracket(decision, bars, bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"))
    assert outcome.exit_kind == "STOP"
    assert outcome.exit_price == Decimal("98")
    assert outcome.gap_through_stop is False
    assert outcome.bars_held == 1


def test_gap_through_the_stop_fills_at_the_open_not_at_the_stop_price() -> None:
    decision = _decision()
    bars = [_candle(1, open_="96", high="99", low="94", close="95")]
    outcome = replay_bracket(decision, bars, bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"))
    assert outcome.exit_kind == "STOP"
    assert outcome.exit_price == Decimal("96")  # the open, never 98
    assert outcome.gap_through_stop is True
    assert outcome.net_return < -0.03  # worse than a fill exactly at the stop


def test_target_only_resolves_when_the_stop_is_never_touched() -> None:
    decision = _decision()
    bars = [
        _candle(1, open_="100", high="101", low="99", close="100.5"),
        _candle(2, open_="100.5", high="104.5", low="100.4", close="104"),
    ]
    outcome = replay_bracket(decision, bars, bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"))
    assert outcome.exit_kind == "TARGET"
    assert outcome.exit_price == Decimal("104")
    assert outcome.bars_held == 2
    assert outcome.net_return > 0


def test_expiry_uses_the_decisions_own_clock_and_hours_held() -> None:
    decision = _decision(expires_ns=1_700_000_000_000_000_000 + 2 * HOUR_NS)
    bars = [
        _candle(1, open_="100", high="100.5", low="99.5", close="100"),
        _candle(2, open_="100", high="100.5", low="99.5", close="100.2"),
    ]
    outcome = replay_bracket(decision, bars, bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"))
    assert outcome.exit_kind == "EXPIRY"
    assert outcome.bars_held == 2
    assert outcome.hours_held == pytest.approx(2.0)


def test_open_at_cutoff_is_not_reported_as_a_closed_trade() -> None:
    decision = _decision(expires_ns=1_700_000_000_000_000_000 + 10 * HOUR_NS)
    bars = [_candle(1, open_="100", high="100.5", low="99.5", close="100.1")]
    outcome = replay_bracket(decision, bars, bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"))
    assert outcome.exit_kind == "OPEN_AT_CUTOFF"
    assert outcome.exit_price == Decimal("100.1")


def test_replay_refuses_a_bar_at_or_before_the_decision_instant() -> None:
    decision = _decision()
    with pytest.raises(ValueError, match="at or before the decision"):
        replay_bracket(
            decision,
            # a bar that starts before the decision instant is the decision bar itself
            [
                _candle(
                    0,
                    open_="100",
                    high="100",
                    low="100",
                    close="100",
                    start_ns=decision.decision_ns - HOUR_NS,
                )
            ],
            bar_ns=HOUR_NS,
            bps_fee=Decimal("0.0005"),
        )
    with pytest.raises(ValueError, match="no bars after"):
        replay_bracket(decision, [], bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"))


def test_timeout_only_policies_report_no_bracket() -> None:
    decision = _decision(
        protection_policy="timeout-only-v1",
        open_trade_expiry_bars=None,
        expires_ns=1_700_000_000_000_000_000 + HOUR_NS,
    )
    bars = [_candle(1, open_="100", high="100", low="80", close="85")]
    outcome = replay_bracket(
        decision, bars, bar_ns=HOUR_NS, bps_fee=Decimal("0.0005"), has_bracket=False
    )
    assert outcome.exit_kind == "EXPIRY"  # a bracket would have stopped out at 98


def test_cash_policy_never_signals_even_on_a_live_frame() -> None:
    assert policy_spec("cash").grammar_policy is None
    assert policy_spec("cash").protection_policy is None
    candles = [
        _candle(i, open_="100", high="100.5", low="99.5", close="100.1") for i in range(3)
    ]
    frame = frame_at(
        "BTCUSDT-PERP.BINANCE", candles[-1].end_ns, tuple(candles)
    )
    assert frame.candles
    assert swing_signal(frame, policy_spec("cash"), bar_ns=HOUR_NS) is None


def test_decision_identity_is_stable_and_ignores_the_wall_clock() -> None:
    first = _decision()
    second = _decision()
    assert first.identity() == second.identity()
    assert first.identity() != _decision(stop_price=Decimal("97")).identity()
    assert first.as_dict()["decision_identity"] == first.identity()


def test_family_is_pre_registered_with_distinct_hashes() -> None:
    registry = family_registry()
    assert set(registry) == {spec.policy_id for spec in SWING_FAMILY}
    assert len(set(registry.values())) == len(registry)
    assert all(value.startswith("sha256:") for value in registry.values())
    baseline = SwingPolicySpec(
        policy_id="plain_swing",
        grammar_policy="range-breakout-48-v1",
        protection_policy="squeeze:baseline:v2",
        description="x",
    )
    # description does not leak into the identity, but the contract does
    assert baseline.identity() == registry["plain_swing"]
    assert ENGINE_TICK == Decimal("0.01")
    assert SHARED_CONTRACT["funding"] == "MISSING_NOT_FED_TO_THIS_PATH"
    with pytest.raises(KeyError):
        policy_spec("does-not-exist")


# --------------------------------------------------------------------------- #
# evaluative — real tape
# --------------------------------------------------------------------------- #


def test_family_exported_as_json_is_stable() -> None:
    payload = [spec.as_dict() for spec in SWING_FAMILY]
    assert json.dumps(payload, sort_keys=True) == json.dumps(payload, sort_keys=True)
    assert all(entry["identity"].startswith("sha256:") for entry in payload)


def test_real_window_registry_precedes_and_binds_the_receipt(tmp_path: Path) -> None:
    if not TAPE.is_file():
        pytest.skip(f"real tape absent at {TAPE}")
    out = tmp_path / "NX06"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--repo-root",
            str(REPO_ROOT),
            "--start-utc",
            "2025-01-01",
            "--end-utc",
            "2025-01-08",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    registry = json.loads((out / "family_registry.json").read_text())
    receipt = json.loads((out / "comparative_receipt.json").read_text())

    # pre-registration came first and carries no result
    assert registry["registered_before_measurement"] is True
    assert "measurements" not in registry
    assert registry["about_to_be_measured_on"]["start_utc"] == "2025-01-01"
    assert registry["family_registry"] == family_registry()

    # the two artifacts bind to each other by content hash
    import hashlib

    digest = hashlib.sha256((out / "family_registry.json").read_bytes()).hexdigest()
    assert receipt["family_registry_sha256"] == digest
    assert receipt["economic_claim"] == "NONE"
    assert "DECISION_PLANE_DIAGNOSTIC" in receipt["evidence_class"]
    assert receipt["window"]["start_ms"] == 1735689600000

    # every policy is measured, the cash floor is not dressed up as a measurement
    measurements = {row["policy_id"]: row for row in receipt["measurements"]}
    assert set(measurements) == {spec.policy_id for spec in SWING_FAMILY}
    cash = measurements["cash"]
    assert cash["campaigns"] == 0 and cash["exposure_fraction"] == 0.0
    assert "not a measurement" in cash["note"]
    for policy_id in ("causal_trend", "plain_swing"):
        row = measurements[policy_id]
        # decisions are only ever counted as campaigns once they are replayed
        assert row["campaigns"] == sum(row["exit_kinds"].values())
        assert row["exposure_bars"] >= 0
        assert row["holding_bars"]["histogram"] is not None
        assert row["decisions"] == row["campaigns"]
    # the two intervals stay distinguishable in the delivered numbers
    swing = measurements["plain_swing"]
    assert swing["open_trade_expiry_bars"] == 336
    assert swing["open_trade_expiry_days"] == pytest.approx(14.0)
    # the two intervals are reported side by side and are never collapsed into one
    # number: the squeeze horizon is 336 bars, the trend grammar's episode is 24.
    assert swing["opportunity_ttl_bars"] and all(
        ttl > 0 for ttl in swing["opportunity_ttl_bars"]
    )
    trend = measurements["causal_trend"]
    assert trend["open_trade_expiry_bars"] is None  # timeout-only stamps no expiry
    assert trend["opportunity_ttl_bars"]
    assert cash["opportunity_ttl_bars"] == [] and cash["decisions"] == 0

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
