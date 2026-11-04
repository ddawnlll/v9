"""The analysis plane over a real window: admission equivalence + conservation.

These are evaluative tests: they run over REAL tape bars and carry weight, so they SKIP when
the multi-year tape is absent from the checkout rather than falling back to synthetic candles
(which would make the assertion meaningless).

What they pin:

* the plane's admitted campaign stream is exactly the stream the frozen four-year artifact
  produced — the same signals, in the same order, with the campaigns the artifact silently
  skipped now recorded as refusals. A second, independent implementation of the admission
  rule (skip-ahead, as the reported artifact does it) is the discriminator;
* attribution conservation holds, and the failure count is not zero (a run that charges
  nothing to a domain must not read as a clean conservation).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from v8_next.adapters.swing_engine import SwingEngineConfig, SwingEngineStrategy
from v8_next.app.analysis import analyse_window
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    policy_spec,
    replay_bracket,
    swing_signal,
)
from v8_next.evaluation.multitape import load_multitape
from v8_next.system_proving.attribution import classify_exit_failure

TAPE_REL = "research/tape/multi-1h-4y/tape.jsonl"
REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOW_BARS = 2500
POLICY = "causal_trend"
HOUR_NS = 3_600 * 10**9


pytestmark = [
    pytest.mark.skipif(
        not (REPO_ROOT / TAPE_REL).is_file(),
        reason=f"real tape {TAPE_REL} is absent; the analysis plane is not evaluated on synthetic bars",
    ),
    pytest.mark.slow,  # #469: real-tape file (multi-1h-4y loads take minutes)
]


def _skip_ahead_admitted(repo_root: Path, policy_id: str, instrument: str, bars: int) -> list[str]:
    """The reported artifact's own admission rule: take a signal, then jump past the campaign.

    Deliberately the *other* algorithm — it never inspects the bars inside an open campaign,
    which is why the artifact cannot report what it refused.
    """
    tape = load_multitape(repo_root / TAPE_REL, limit=bars)
    series = list(tape.candles[instrument])
    instrument_id = series[0].instrument_id
    spec = policy_spec(policy_id)
    grammar_policy = spec.grammar_policy
    assert grammar_policy is not None
    warmup = int(POLICY_REQUIRED_BARS[grammar_policy])
    has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
    cfg = SwingEngineConfig(
        policy_id="plain_swing",
        instrument_id=instrument_id,
        bar_type_str=f"{instrument_id}-1-HOUR-LAST-EXTERNAL",
    )
    framing = SwingEngineStrategy(cfg, {i: c for i, c in enumerate(series)}, warmup_bars=10**9)
    frames = []
    for position, candle in enumerate(series):
        framing.on_bar(SimpleNamespace(ts_event=position))  # type: ignore[arg-type]
        frames.append(framing._decision_frame(candle))

    identities: list[str] = []
    index = warmup
    while index < len(series) - 1:
        decision = swing_signal(frames[index], spec, bar_ns=HOUR_NS)
        if decision is None:
            index += 1
            continue
        outcome = replay_bracket(
            decision,
            series[index + 1 :],
            bar_ns=HOUR_NS,
            bps_fee=Decimal(SHARED_CONTRACT["taker_fee"]),
            has_bracket=has_bracket,
        )
        identities.append(decision.identity())
        index += max(1, outcome.bars_held) + 1
    return identities


def test_admitted_stream_is_the_frozen_artifact_stream_and_the_rest_is_refused() -> None:
    report = analyse_window(
        repo_root=REPO_ROOT,
        tape_rel=TAPE_REL,
        policy_id=POLICY,
        instrument="BTCUSDT",
        limit_bars=WINDOW_BARS,
        window_label=f"equivalence_window_{WINDOW_BARS}",
    )
    expected = _skip_ahead_admitted(REPO_ROOT, POLICY, "BTCUSDT", WINDOW_BARS)

    assert report["admitted_campaigns"] == len(expected)
    assert report["admitted_campaigns"] > 0
    assert report["conservation_verified"] is True
    assert report["attribution"]["total_failures"] > 0, "a zero-failure window pins nothing"
    assert report["attribution"]["total_failures"] == sum(
        report["attribution"]["counts_by_domain"].values()
    )
    # The refusals are exactly the signals the skip-ahead artifact walked past.
    assert report["refusals"]["total"] > report["admitted_campaigns"]
    assert set(report["refusals"]["by_reason"]) == {"EXISTING_EXPOSURE_CONFLICT"}
    assert report["refusals"]["summary"]["claim"] == "NO_ECONOMIC_CLAIM"


def test_refused_counterfactuals_are_replayed_and_the_money_column_stays_absent() -> None:
    report = analyse_window(
        repo_root=REPO_ROOT,
        tape_rel=TAPE_REL,
        policy_id=POLICY,
        instrument="BTCUSDT",
        limit_bars=WINDOW_BARS,
    )
    summary = report["refusals"]["summary"]
    assert summary["counterfactual_rows"] == summary["total_candidates_vetoed"]
    assert summary["avoided_loss_return_units"] is not None
    assert summary["missed_profit_return_units"] is not None
    # No position size exists in the decision plane: the USDT value is absent, not zero.
    assert summary["total_avoided_loss_usdt"] is None
    assert summary["total_missed_profit_usdt"] is None
    assert summary["gate_defensive_efficiency_ratio"] is None
    assert any("unmeasured_rows" in factor for factor in summary["missing_factors"])
    assert summary["authority_distribution"] == {"IDENTIFIED": summary["total_candidates_vetoed"]}


def test_reconciliation_names_the_fields_the_decision_plane_cannot_bind() -> None:
    report = analyse_window(
        repo_root=REPO_ROOT,
        tape_rel=TAPE_REL,
        policy_id=POLICY,
        instrument="BTCUSDT",
        limit_bars=WINDOW_BARS,
    )
    reconciliation = report["reconciliation"]
    assert reconciliation["fields_compared"] == 10
    assert reconciliation["status"] == "RECONCILIATION_PARTIAL"
    assert reconciliation["unbound"], "an unbound field must be named, never compared as zero"
    assert reconciliation["unbound_reason"] == "MISSING_FIELD_IN_DECISION_PLANE_ARTIFACT"
    for name in reconciliation["unbound"]:
        assert name not in reconciliation["bound"]


def test_the_three_domain_classifier_still_separates_exit_selection_and_execution() -> None:
    """The plane charges EXIT/SELECTION/EXECUTION from what a real run reports (spec §6)."""
    from v8_next.system_proving import FailureDomain

    assert (
        classify_exit_failure(exit_kind="EXPIRY", net_return=-0.01, has_bracket=False)
        is FailureDomain.EXIT
    )
    assert (
        classify_exit_failure(exit_kind="EXPIRY", net_return=-0.01, has_bracket=True)
        is FailureDomain.SELECTION
    )
    assert (
        classify_exit_failure(exit_kind="STOP", net_return=-0.01, has_bracket=True)
        is FailureDomain.EXECUTION
    )


def test_explain_attaches_a_verifiable_decision_trail_and_names_the_unobserved_stages() -> None:
    """R3: the trail is structured, hash-bound, and readable off the explanation itself."""
    from v8_next.adapters.execution_telemetry import verify_decision_trail
    from v8_next.app.explain import explain_trade

    record = explain_trade(
        repo_root=REPO_ROOT, policy_id=POLICY, index=0, tape_rel=TAPE_REL
    )
    trail = record["decision_trail"]["decision_trail"]
    assert verify_decision_trail(trail) == trail["trail_digest"]
    assert [span["stage"] for span in trail["spans"]] == [
        "MarketState",
        "OpportunityDetection",
        "CampaignAdmission",
    ]
    assert len(trail["counterfactual_branches"]) == 1
    # The engine-plane stages were never observed here and say so.
    assert record["decision_trail"]["stages_not_observed"] == [
        "OrderDispatch",
        "ExecutionFill",
        "PositionManagement",
        "CashflowSettlement",
    ]
    assert record["decision_trail_digest"] == trail["trail_digest"]

    # Same decision, same content, same identity: no wall clock enters the digest.
    repeat = explain_trade(
        repo_root=REPO_ROOT, policy_id=POLICY, index=0, tape_rel=TAPE_REL
    )
    assert repeat["decision_trail_digest"] == record["decision_trail_digest"]
