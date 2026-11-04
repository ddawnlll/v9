"""MECHANICS ONLY — parity and arithmetic tests for ``v8_next.analysis.phases``.

Every series in this file is synthetic and exists solely to exercise a derivation or to
reproduce a value captured from the frozen oracle (``tools/regret_phase*.py`` via the Rust
``v8-core/src/analysis`` tests). They carry **no evaluative weight**: no test here asserts
economic performance, makes a discoverability claim, or reads a synthetic verdict as a finding.
The one real-tape check (``test_real_tape_smoke``) skips when the tape is absent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.analysis import phases as P

TAPE = tape_path("multi-1h-4y", "tape.jsonl")

KEY = "trend_pullback|BTCUSDT|LONG|mean_legal_hindsight_gap"
KEY2 = "trend_pullback|BTCUSDT|LONG|mean_actual_vs_no_trade"


# ---------------------------------------------------------------------------
# Phase 1 fixtures (the frozen oracle's synthetic fixture; phase1.rs tests)
# ---------------------------------------------------------------------------


def _identity(expert: str, direction: str, birth_time: int) -> P.CandidateIdentity:
    return P.CandidateIdentity(expert, direction, birth_time)


def _gap(
    cid: str,
    aid: str | None,
    status: str,
    gap: float | None,
    actual_u: float | None,
    best_u: float | None,
    tie: int,
) -> P.GapRecord:
    return P.GapRecord(cid, aid, actual_u, best_u, tie, gap, status)


def _cube(
    cid: str,
    aid: str,
    endpoint: str | None,
    label: str | None,
    horizon: int | None,
    cost_r: float | None,
    funding_r: float | None,
    mae_r: float | None,
    mfe_r: float | None,
    ambiguous: int | None,
) -> tuple[tuple[str, str], P.CubeAccumulators]:
    return (
        (cid, aid),
        P.CubeAccumulators(endpoint, label, horizon, cost_r, funding_r, mae_r, mfe_r, ambiguous),
    )


def _phase0_fixture() -> list[tuple[str, P.Phase0Output]]:
    btc_ident = {
        "c-btc-0001": _identity("trend_pullback", "LONG", 1_700_000_000),
        "c-btc-0002": _identity("failed_breakout", "SHORT", 1_700_000_100),
        "c-btc-0003": _identity("liquidity_sweep_reclaim", "LONG", 1_700_000_200),
        "c-btc-0004": _identity("trend_pullback", "LONG", 1_700_000_300),
        "c-btc-0005": _identity("failed_breakout", "SHORT", 1_700_000_400),
        # c-btc-0006 deliberately has no DETECTED transition.
    }
    btc_gaps = [
        _gap("c-btc-0001", "btc-act-1", "COMPUTED", 0.6771358024691356, -0.38701234567890123, 0.29012345678901234, 1),
        _gap("c-btc-0002", "btc-act-2", "COMPUTED", 1.142, -0.09, 1.052, 3),
        _gap("c-btc-0003", "btc-act-3", "ABSTAINED_CENSORED", None, -0.2, None, 0),
        _gap("c-btc-0004", "btc-act-4-missing-cube", "COMPUTED", 0.001, -0.001, 0.0, 1),
        _gap("c-btc-0005", None, "NOT_APPLICABLE_NO_ACTUAL_ACTION", None, None, None, 0),
        _gap("c-btc-0006", "btc-act-6", "COMPUTED", 0.222, 0.111, 0.333, 2),
    ]
    btc_cubes = dict(
        [
            _cube("c-btc-0001", "btc-act-1", "TARGET", "MATURE", 47, 0.053111111111111116, 0.0, -0.8812345678901234, 0.9234567890123457, 2),
            _cube("c-btc-0002", "btc-act-2", "TARGET", "MATURE", 8, 0.05, 0.0, -0.4, 1.6, 0),
            _cube("c-btc-0003", "btc-act-3", "STOP", "RIGHT_CENSORED", 24, 0.05, None, -1.2, 0.7, 5),
            _cube("c-btc-0006", "btc-act-6", "TARGET", "MATURE", 16, 0.0, 0.0, -0.5, 0.9, 0),
        ]
    )
    sol_ident = {
        "c-sol-0001": _identity("liquidity_sweep_reclaim", "LONG", 1_700_000_500),
        "c-sol-0002": _identity("trend_pullback", "SHORT", 1_700_000_600),
    }
    sol_gaps = [
        _gap("c-sol-0001", "sol-act-1", "COMPUTED", 0.28, 0.05, 0.33, 2),
        _gap("c-sol-0002", None, "ABSTAINED_UNDEFINED", None, None, None, 0),
    ]
    sol_cubes = dict(
        [
            _cube("c-sol-0001", "sol-act-1", "TARGET", "MATURE", 48, 0.07777777777777778, 0.0, -0.6543210987654321, 1.2345678901234567, 0),
        ]
    )
    return [
        ("SOLUSDT", P.Phase0Output(sol_ident, sol_gaps, sol_cubes)),
        ("BTCUSDT", P.Phase0Output(btc_ident, btc_gaps, btc_cubes)),
    ]


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------


def test_join_dataset_is_sorted_and_keeps_none_for_missing() -> None:
    rows = P.join_dataset(_phase0_fixture())
    assert len(rows) == 8
    assert [r.symbol for r in rows] == ["BTCUSDT"] * 6 + ["SOLUSDT"] * 2
    first = rows[0]
    assert (first.candidate_id, first.expert_id, first.epistemic_class) == (
        "c-btc-0001",
        "trend_pullback",
        "MODEL_DERIVED",
    )
    assert first.endpoint == "TARGET" and first.horizon_bars == 47
    # c-btc-0006 has no DETECTED identity: defaults, never a fabricated expert id.
    missing_ident = next(r for r in rows if r.candidate_id == "c-btc-0006")
    assert (missing_ident.expert_id, missing_ident.direction, missing_ident.birth_time) == ("", "", 0)
    # c-btc-0004's action is absent from the cube table: None cube fields, never 0.
    missing_cube = next(r for r in rows if r.candidate_id == "c-btc-0004")
    assert missing_cube.endpoint is None and missing_cube.horizon_bars is None
    assert missing_cube.cost_r is None and missing_cube.mfe_r is None


def test_joined_row_field_count_and_label_are_frozen() -> None:
    assert P.JoinedCandidateRow.FIELD_COUNT == 19
    assert len(P.JoinedCandidateRow.__dataclass_fields__) == 19
    assert P.LABEL == "MODEL_DERIVED_DESCRIPTIVE_NOT_YET_GATED"


class _Manifest:
    def __init__(self, actions: Sequence[P.LegalAction]) -> None:
        self.actions = actions


def test_opportunity_set_projects_utilities() -> None:
    manifest = _Manifest(
        [
            P.LegalAction("NO_TRADE", "NO_TRADE", "DECLARED_VARIANT"),
            P.LegalAction("act-1", "GEOMETRY_VARIANT", "ACTUAL"),
        ]
    )
    cells = P.opportunity_set(manifest, {"act-1": 0.5})
    assert [(c.action_id, c.utility) for c in cells] == [("NO_TRADE", None), ("act-1", 0.5)]
    assert cells[1].provenance == "ACTUAL"


# ---------------------------------------------------------------------------
# statistics ports — frozen oracle parity
# ---------------------------------------------------------------------------


def test_seed_for_matches_frozen_constants() -> None:
    assert P.seed_for(KEY) == 3_159_536_366
    assert P.seed_for(f"{KEY}|confirmation") == 2_126_530_160
    assert P.seed_for(KEY2) == 3_687_838_827
    assert P.seed_for(f"{KEY}|phase3") == 3_216_311_728
    assert (
        P.seed_for("failed_breakout|SOLUSDT|SHORT|mean_legal_hindsight_gap|phase3")
        == 674_598_930
    )


def test_seed_for_is_deterministic_and_clock_free() -> None:
    # Same key => same seed, regardless of when it is called.
    assert P.seed_for(KEY) == P.seed_for(KEY)
    assert P.seed_for(KEY) != P.seed_for(KEY2)


def test_format_g6_matches_cpython_percent_g() -> None:
    battery = [
        (-0.5, "-0.5"),
        (0.0001, "0.0001"),
        (0.3333333333333333, "0.333333"),
        (0.73456, "0.73456"),
        (0.9999999, "1"),
        (1.0, "1"),
        (1.23456789012345, "1.23457"),
        (1.23456789e-05, "1.23457e-05"),
        (1_000_000.0, "1e+06"),
        (12.345678, "12.3457"),
        (1234.5, "1234.5"),
        (123_456_789.0, "1.23457e+08"),
        (1e-05, "1e-05"),
        (1e-08, "1e-08"),
        (42.0, "42"),
        (68.5, "68.5"),
        (999_999.0, "999999"),
        (1.234565, "1.23456"),
        (1.234575, "1.23457"),
        (1_234_567.0, "1.23457e+06"),
        (-0.0, "-0"),
        (0.0, "0"),
        (999_999.95, "1e+06"),
        (0.30000000000000004, "0.3"),
        (123_456.5, "123456"),
    ]
    for value, want in battery:
        assert P.format_g6(value) == want, value


def test_select_block_size_and_bootstrap_ci_match_oracle() -> None:
    deltas = [0.3, 0.0, 0.5, 0.0, 0.1, 0.0, 0.0, 0.0]
    assert P.select_block_size(deltas) == 4
    assert P.bootstrap_ci(deltas, 4, P.N_RESAMPLES, 3_216_311_728, P.CI) == (0.025, 0.2)
    assert P.select_block_size([0.5]) == 1
    assert P.select_block_size([0.3, -0.1, 0.2]) == 1
    assert P.select_block_size([0.25] * 27) == 3


def test_bootstrap_tail_is_99_not_100() -> None:
    assert int(P.N_RESAMPLES * (1.0 - P.CI) / 2.0) == 99


def test_bootstrap_is_a_real_resampling() -> None:
    series = [0.1, 0.4, -0.2, 0.3, 0.05, -0.15, 0.25, 0.3, -0.05, 0.2]
    means = P.block_bootstrap_means(series, 2, 500, 12345)
    assert len(means) == 500
    assert len(set(means)) > 50  # a real draw varies; a fabricated constant would not
    assert P.block_bootstrap_means(series, 2, 500, 12345) == means  # same seed => bit-identical
    assert P.block_bootstrap_means(series, 2, 500, 54321) != means


def test_bootstrap_fails_closed_on_degenerate_inputs() -> None:
    with pytest.raises(ValueError, match="degenerate block bootstrap"):
        P._block_bootstrap_indices(10, 10, __import__("random").Random(1))
    with pytest.raises(ValueError, match="degenerate block bootstrap"):
        P.bootstrap_ci([0.1, 0.2], 2, 100, 7, 0.9)
    with pytest.raises(ValueError, match="block_size must be positive"):
        P._block_bootstrap_indices(10, 0, __import__("random").Random(1))
    with pytest.raises(ValueError, match="ci must be in"):
        P.bootstrap_ci([0.1, 0.2, 0.3], 1, 100, 7, 1.0)
    with pytest.raises(ValueError, match="positive"):
        P.block_bootstrap_means([0.1], 1, 0, 7)
    assert P.bootstrap_ci([], 1, 100, 7, 0.9) == (0.0, 0.0)


def test_support_helpers() -> None:
    assert P.effective_independent_episodes(0, 5) == 0.0
    assert P.effective_independent_episodes(40, 2) == 20.0
    with pytest.raises(ValueError):
        P.effective_independent_episodes(10, 0)
    meets, note = P.practical_significance([0.2] * 30, P.MIN_NET_R, P.MIN_TRADES_MATERIALITY)
    assert meets is True
    assert "meets" in note and "0.2000" in note
    below, _ = P.practical_significance([0.01] * 30, P.MIN_NET_R, P.MIN_TRADES_MATERIALITY)
    assert below is False
    assert P.expected_false_positives(72, P.ALPHA_FAMILY) == 3.6
    assert P.effective_search_size(72, 72) == 72
    with pytest.raises(ValueError, match="D-046"):
        P.effective_search_size(73, 72)


# ---------------------------------------------------------------------------
# Phase 2 — discovery / confirmation
# ---------------------------------------------------------------------------


def _row(
    expert: str,
    symbol: str,
    direction: str,
    status: str,
    gap: float | None,
    util: float | None,
    horizon: int | None,
) -> P.SliceRow:
    return P.SliceRow(expert, symbol, direction, status, gap, util, horizon)


def _discovery_rows() -> list[P.SliceRow]:
    rows = [
        _row("trend_pullback", "BTCUSDT", "LONG", "COMPUTED", 0.10 + 0.013 * (i % 9), None, 2 if i % 11 == 0 else 1)
        for i in range(40)
    ]
    rows += [_row("trend_pullback", "BTCUSDT", "LONG", "REJECTED", None, None, 3) for _ in range(5)]
    rows += [
        _row("failed_breakout", "BTCUSDT", "LONG", "COMPUTED", 9.9, None, 1),
        _row("trend_pullback", "ETHUSDT", "LONG", "COMPUTED", 8.8, None, 1),
        _row("trend_pullback", "BTCUSDT", "SHORT", "COMPUTED", 7.7, None, 1),
    ]
    return rows


def test_declare_slices_is_the_declared_72() -> None:
    slices = P.declare_slices()
    assert len(slices) == 72
    assert slices[0][0] == KEY
    assert slices[1][0] == KEY2
    assert slices[2][0] == "trend_pullback|BTCUSDT|SHORT|mean_legal_hindsight_gap"
    assert slices[71][0] == "liquidity_sweep_reclaim|DOGEUSDT|SHORT|mean_actual_vs_no_trade"
    for expert in P.EXPERTS:
        assert sum(1 for s in slices if s[1] == expert) == 24
    assert len({s[0] for s in slices}) == 72


def test_discovery_slice_matches_oracle_bit_for_bit() -> None:
    r = P.score_slice(KEY, "trend_pullback", "BTCUSDT", "LONG", "mean_legal_hindsight_gap", _discovery_rows())
    assert r.n_total_in_slice == 45  # REJECTED rows count in the slice total
    assert r.n_computed == 40
    assert r.effective_independent_episodes == 20.0
    assert r.mean == 0.14875
    assert (r.ci_lower, r.ci_upper) == (0.139325, 0.158175)
    assert r.ci_lower is not None and r.ci_upper is not None and r.ci_lower < r.ci_upper
    assert r.block_size == 6
    assert r.alpha_slate == 0.0006944444444444445
    assert r.practically_significant is True
    assert r.discovery_verdict == "CANDIDATE_SYSTEMATIC"
    assert r.confirmation_verdict is None


def test_actual_vs_no_trade_slice_matches_oracle() -> None:
    rows = [
        _row("trend_pullback", "BTCUSDT", "LONG", "COMPUTED", None, 0.04 + 0.021 * (i % 6), 1)
        for i in range(38)
    ]
    r = P.score_slice(KEY2, "trend_pullback", "BTCUSDT", "LONG", "mean_actual_vs_no_trade", rows)
    assert r.mean == 0.09028947368421053
    assert (r.ci_lower, r.ci_upper) == (0.08476315789473685, 0.09471052631578948)
    assert r.block_size == 6
    assert r.discovery_verdict == "CANDIDATE_SYSTEMATIC"


def test_insufficient_support_and_excluded_empty_are_named() -> None:
    short = [
        _row("trend_pullback", "ETHUSDT", "SHORT", "COMPUTED", 0.05 + 0.01 * i, None, 1)
        for i in range(12)
    ]
    r = P.score_slice(
        "trend_pullback|ETHUSDT|SHORT|mean_legal_hindsight_gap",
        "trend_pullback",
        "ETHUSDT",
        "SHORT",
        "mean_legal_hindsight_gap",
        short,
    )
    assert r.mean is None and r.block_size is None
    assert r.discovery_verdict == "INSUFFICIENT_SUPPORT"
    assert "need >=30" in r.materiality_note

    empty = P.score_slice(
        "liquidity_sweep_reclaim|DOGEUSDT|LONG|mean_actual_vs_no_trade",
        "liquidity_sweep_reclaim",
        "DOGEUSDT",
        "LONG",
        "mean_actual_vs_no_trade",
        [_row("trend_pullback", "BTCUSDT", "LONG", "COMPUTED", 0.5, None, 1)],
    )
    assert empty.n_total_in_slice == 0
    assert empty.discovery_verdict == "EXCLUDED_EMPTY"
    assert empty.materiality_note == "no candidates in this slice"


def test_estimand_series_never_coerces_non_computed() -> None:
    rows = [
        _row("trend_pullback", "BTCUSDT", "LONG", "REJECTED", 9.9, 8.8, 1),
        _row("trend_pullback", "BTCUSDT", "LONG", "COMPUTED", 0.5, 0.4, 1),
    ]
    series, computed = P.estimand_series(rows, "mean_legal_hindsight_gap")
    assert series == [0.5] and len(computed) == 1
    with pytest.raises(ValueError, match="unknown estimand"):
        P.estimand_series(rows, "nope")


def test_confirmation_matches_oracle_and_second_query_is_refused() -> None:
    candidate = P.score_slice(KEY, "trend_pullback", "BTCUSDT", "LONG", "mean_legal_hindsight_gap", _discovery_rows())
    assert candidate.discovery_verdict == "CANDIDATE_SYSTEMATIC"

    conf_a = [
        _row("trend_pullback", "BTCUSDT", "LONG", "COMPUTED", 0.09 + 0.017 * (i % 7), None, 2 if i % 13 == 0 else 1)
        for i in range(36)
    ]
    conf_a += [_row("trend_pullback", "BTCUSDT", "LONG", "CENSORED", None, None, 2) for _ in range(4)]

    ledger = P.ConfirmationLedger()
    cr = ledger.query(candidate, conf_a)
    assert cr.confirmation_verdict == "SYSTEMATIC_FINDING"
    assert cr.confirmation_mean == 0.13958333333333334
    assert (cr.confirmation_ci_lower, cr.confirmation_ci_upper) == (
        0.1343888888888889,
        0.14430555555555558,
    )
    assert cr.confirmation_n_computed == 36
    with pytest.raises(ValueError, match="FCR-V8RR-007 AP002"):
        ledger.query(candidate, conf_a)


def test_confirmation_below_support_floor_fails_closed() -> None:
    candidate = P.score_slice(KEY, "trend_pullback", "BTCUSDT", "LONG", "mean_legal_hindsight_gap", _discovery_rows())
    ledger = P.ConfirmationLedger()
    cr = ledger.query(candidate, [])
    assert cr.confirmation_verdict == "FAILED_CONFIRMATION"
    assert cr.confirmation_mean is None and cr.confirmation_ci_lower is None
    assert cr.confirmation_reason == P.REASON_NO_CONFIRMATION_ROWS


def test_discovery_summary_counts_are_exact() -> None:
    rows = _discovery_rows()
    candidate = P.score_slice(KEY, "trend_pullback", "BTCUSDT", "LONG", "mean_legal_hindsight_gap", rows)
    empty = P.score_slice(
        "liquidity_sweep_reclaim|DOGEUSDT|LONG|mean_legal_hindsight_gap",
        "liquidity_sweep_reclaim",
        "DOGEUSDT",
        "LONG",
        "mean_legal_hindsight_gap",
        rows,
    )
    insufficient = P.score_slice(
        "trend_pullback|ETHUSDT|SHORT|mean_legal_hindsight_gap",
        "trend_pullback",
        "ETHUSDT",
        "SHORT",
        "mean_legal_hindsight_gap",
        [_row("trend_pullback", "ETHUSDT", "SHORT", "COMPUTED", 0.05, None, 1)],
    )
    summary = P.discovery_summary([candidate, empty, insufficient])
    assert summary.n_slices_declared == 72
    assert summary.n_candidate_systematic == 1
    assert summary.expected_false_positives_at_family_alpha == 3.6
    assert summary.alpha_slate_bonferroni == 0.0006944444444444445
    assert summary.candidate_systematic_slices == [KEY]
    assert len(summary.discovery_verdict_distribution) == 5
    assert dict(summary.discovery_verdict_distribution)["EXCLUDED_EMPTY"] == 1


# ---------------------------------------------------------------------------
# Phase 3 — recoverability
# ---------------------------------------------------------------------------


def _p3_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    disc1 = [0.5, -0.2, 0.3, 0.1, -0.4, 0.6, -0.1, 0.25]
    conf1 = [-0.3, 0.2, -0.5, 0.4, -0.1, 0.35, 0.05, -0.25]
    disc2 = [0.2, -0.3, 0.4, -0.1, 0.1]
    conf2 = [0.0] * 5

    def row(cid: str, expert: str, symbol: str, direction: str, u: float) -> dict[str, object]:
        return {
            "expert_id": expert,
            "symbol": symbol,
            "direction": direction,
            "gap_status": "COMPUTED",
            "actual_utility": u,
            "candidate_id": cid,
        }

    disc = [row(f"btc-s{i}", "trend_pullback", "BTCUSDT", "LONG", u) for i, u in enumerate(disc1)]
    conf = [row(f"btc-c{i}", "trend_pullback", "BTCUSDT", "LONG", u) for i, u in enumerate(conf1)]
    disc += [row(f"sol-s{i}", "failed_breakout", "SOLUSDT", "SHORT", u) for i, u in enumerate(disc2)]
    conf += [row(f"sol-t{i}", "failed_breakout", "SOLUSDT", "SHORT", u) for i, u in enumerate(conf2)]
    return disc, conf


def _birth(symbol: str, states: Sequence[tuple[str, float | None, float | None, float | None]]) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for cid, rsi, bb, adx in states:
        out[cid] = {"rsi14": rsi, "bb_pct_b": bb, "adx14": adx}
    return out


# The frozen oracle's birth-state fixture (phase3.rs tests): 8 discovery + 8 confirmation
# candidates per symbol, with the exact features the captures were taken on.
_BTC_STATES: list[tuple[str, float | None, float | None, float | None]] = [
    ("btc-s0", 68.5, 0.73, 25.5),
    ("btc-s1", 42.0, 0.45, 18.0),
    ("btc-s2", 70.25, 0.9, 40.0),
    ("btc-s3", 55.0, 0.2, 12.0),
    ("btc-s4", 30.0, 0.6, 30.0),
    ("btc-s5", 65.0, 0.73456, 22.0),
    ("btc-s6", None, 0.3, 15.5),
    ("btc-s7", 58.5, 0.85, 35.0),
    ("btc-c0", 30.0, 0.2, 12.0),
    ("btc-c1", 65.0, 0.85, 35.0),
    ("btc-c2", 48.0, 0.3, 15.5),
    ("btc-c3", 70.25, 0.9, 40.0),
    ("btc-c4", 42.0, 0.45, None),
    ("btc-c5", 55.0, 0.6, 22.0),
    ("btc-c6", 58.5, 0.73456, 30.0),
    ("btc-c7", 68.5, 0.73, 25.5),
]
_SOL_STATES: list[tuple[str, float | None, float | None, float | None]] = [
    ("sol-s0", 50.0, 0.5, 20.0),
    ("sol-s1", 40.0, 0.3, 15.0),
    ("sol-s2", 60.0, 0.7, 25.0),
    ("sol-s3", 45.0, 0.4, 18.0),
    ("sol-s4", 55.0, 0.6, 22.0),
    ("sol-t0", 42.0, 0.35, 16.0),
    ("sol-t1", 58.0, 0.75, 28.0),
    ("sol-t2", 48.0, 0.45, 19.0),
    ("sol-t3", 63.0, 0.8, 30.0),
    ("sol-t4", 52.0, 0.55, 21.0),
]


def test_apply_policy_and_feature_value() -> None:
    gate = P.PolicySpec("THRESHOLD_GATE|rsi14|NO_TRADE_BELOW|55|q0.4", "THRESHOLD_GATE", "rsi14", "NO_TRADE_BELOW", 55.0)
    above = P.PolicySpec("THRESHOLD_GATE|rsi14|NO_TRADE_ABOVE|55|q0.4", "THRESHOLD_GATE", "rsi14", "NO_TRADE_ABOVE", 55.0)
    always = P.PolicySpec("ALWAYS_TRADE", "ALWAYS_TRADE")
    assert P.apply_policy(gate, {"rsi14": 30.0}, -0.4) == 0.0  # fires below -> NO_TRADE
    assert P.apply_policy(gate, {"rsi14": 68.5}, 0.5) == 0.5  # does not fire
    assert P.apply_policy(gate, {"rsi14": None}, 0.5) == 0.5  # feature absent -> replay utility
    assert P.apply_policy(above, {"rsi14": 68.5}, 0.5) == 0.0
    assert P.apply_policy(always, {"rsi14": None}, 0.5) == 0.5
    state = {"features": {"BTCUSDT.rsi14": {"value": 42.0}, "BTCUSDT.adx14": {"value": None}}}
    assert P.feature_value(state, "BTCUSDT", "rsi14") == 42.0
    assert P.feature_value(state, "BTCUSDT", "adx14") is None
    assert P.feature_value(state, "BTCUSDT", "bb_pct_b") is None


def test_phase3_recoverability_matches_oracle(tmp_path: Path) -> None:
    disc, conf = _p3_rows()
    _, result = P.evaluate_slice_recoverability_with_birth(
        KEY, "trend_pullback", "BTCUSDT", "LONG", _birth("BTCUSDT", _BTC_STATES), disc, conf
    )
    assert result.recoverability_verdict == P.RECOVERABLE_WITHIN_CLASS
    assert result.confirmation_g_r == 0.1125
    assert (result.confirmation_g_r_ci_lower, result.confirmation_g_r_ci_upper) == (0.025, 0.2)
    assert result.confirmation_v_a == -0.018749999999999996
    assert result.confirmation_v_r == 0.09375
    assert result.discovery_selection_mean_utility == 0.20625
    assert result.selected_policy is not None
    assert result.selected_policy.policy_id == "THRESHOLD_GATE|rsi14|NO_TRADE_BELOW|55|q0.4"
    assert result.recoverability_reason == P.REASON_RECOVERABLE

    # A constant confirmation series has no non-zero deltas: fail closed, no verdict inflation.
    _, flat = P.evaluate_slice_recoverability_with_birth(
        "failed_breakout|SOLUSDT|SHORT|mean_legal_hindsight_gap",
        "failed_breakout",
        "SOLUSDT",
        "SHORT",
        _birth("SOLUSDT", []),
        disc,
        conf,
    )
    assert flat.recoverability_verdict == P.NOT_RECOVERABLE_WITHIN_CLASS
    assert flat.recoverability_reason == P.REASON_NO_NONZERO_DELTAS


def test_declare_policies_has_25_and_stable_ids() -> None:
    series = {"rsi14": [42.0, 0.0, 0.0, 0.0], "bb_pct_b": [], "adx14": []}
    policies = P.declare_policies(series)
    assert policies[0].policy_id == "ALWAYS_TRADE"
    # An empty feature contributes no gates (no fabricated threshold).
    assert not any(p.feature in ("bb_pct_b", "adx14") for p in policies)
    assert policies[1].policy_id.startswith("THRESHOLD_GATE|rsi14|NO_TRADE_BELOW|")
    assert policies[1].threshold is not None


def test_phase3_refuses_an_interval_over_three_points() -> None:
    # Discovery: distinct rsi values so a gate improves on trading; confirmation: three rows
    # whose gate fires, giving three non-zero deltas -> below MIN_INTERVAL_POINTS.
    disc = [
        {"expert_id": "trend_pullback", "symbol": "BTCUSDT", "direction": "LONG", "gap_status": "COMPUTED", "actual_utility": -1.0, "candidate_id": f"d{i}"}
        for i in range(4)
    ]
    conf = [
        {"expert_id": "trend_pullback", "symbol": "BTCUSDT", "direction": "LONG", "gap_status": "COMPUTED", "actual_utility": -0.5, "candidate_id": f"c{i}"}
        for i in range(3)
    ]
    birth = {
        "d0": {"rsi14": 10.0, "bb_pct_b": None, "adx14": None},
        "d1": {"rsi14": 20.0, "bb_pct_b": None, "adx14": None},
        "d2": {"rsi14": 30.0, "bb_pct_b": None, "adx14": None},
        "d3": {"rsi14": 40.0, "bb_pct_b": None, "adx14": None},
        "c0": {"rsi14": 50.0, "bb_pct_b": None, "adx14": None},
        "c1": {"rsi14": 50.0, "bb_pct_b": None, "adx14": None},
        "c2": {"rsi14": 50.0, "bb_pct_b": None, "adx14": None},
    }
    _, result = P.evaluate_slice_recoverability_with_birth(
        KEY, "trend_pullback", "BTCUSDT", "LONG", birth, disc, conf
    )
    assert result.confirmation_g_r is not None and result.confirmation_g_r > 0.0
    assert result.confirmation_g_r_ci_lower is None
    assert result.confirmation_g_r_ci_upper is None
    assert result.recoverability_verdict == P.NOT_RECOVERABLE_WITHIN_CLASS
    assert result.recoverability_reason == P.REASON_INSUFFICIENT_CONFIRMATION_SUPPORT


def test_run_phase3_writes_artifacts_and_summary(tmp_path: Path) -> None:
    disc, conf = _p3_rows()
    birth_cache = {
        "BTCUSDT": _birth("BTCUSDT", _BTC_STATES),
        "SOLUSDT": _birth("SOLUSDT", _SOL_STATES),
    }
    keys = [KEY, "failed_breakout|SOLUSDT|SHORT|mean_legal_hindsight_gap"]
    summary = P.run_phase3_in_memory(keys, disc, conf, birth_cache, tmp_path)
    assert summary["n_slices_tested"] == 2
    assert summary["n_recoverable_within_class"] == 1
    assert summary["recoverable_slices"] == [KEY]
    attempts = (tmp_path / "recoverability_attempts.jsonl").read_text().splitlines()
    assert len(attempts) == 25 * 2 + 2  # 25 discovery attempts + 1 confirmation result per slice
    assert (tmp_path / "phase3_summary.json").is_file()
    written = json.loads((tmp_path / "phase3_summary.json").read_text())
    assert written["n_not_recoverable_within_class"] == 1


def test_load_birth_features_fails_loudly_on_missing_store(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        P.load_birth_features(tmp_path / "absent", "BTCUSDT")


def test_load_birth_features_from_memory_shape() -> None:
    candidates = [{"candidate_id": "c0", "state_id": "s0", "to_state": "DETECTED"}]
    states = [
        {"state_id": "s0", "features": {"BTCUSDT.rsi14": {"value": "nan-ish"}}},
    ]
    birth = P.load_birth_features_from_memory(candidates, states, "BTCUSDT")
    assert birth["c0"]["rsi14"] is None  # a non-numeric value is never coerced
    assert birth["c0"]["bb_pct_b"] is None


# ---------------------------------------------------------------------------
# Real-tape smoke (skipped when the tape is absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not TAPE.is_file(), reason="real multi-1h tape not present")
def test_real_tape_smoke() -> None:
    """Exercise the discovery path on real bars (no archetype claim, no economic weight).

    Bars come from the real tape; the episode series is the measured close-to-close return,
    so nothing here is fabricated. The assertion is mechanical: the verdict lands in the
    declared vocabulary and any interval is a genuine, non-degenerate range.
    """
    from v8_next.evaluation.multitape import load_multitape

    tape = load_multitape(TAPE, limit=4000)
    assert tape.n_bars >= P.MIN_N_COMPUTED
    closes = tape.candles[tape.instruments[0]]
    rows: list[P.SliceRow] = []
    for i in range(len(closes) - 1):
        prev = float(closes[i].close)
        cur = float(closes[i + 1].close)
        rows.append(
            P.SliceRow("trend_pullback", "BTCUSDT", "LONG", "COMPUTED", (cur - prev) / prev, None, 1)
        )
    result = P.score_slice(KEY, "trend_pullback", "BTCUSDT", "LONG", "mean_legal_hindsight_gap", rows)
    assert result.discovery_verdict in P.DISCOVERY_VERDICTS
    if result.mean is not None:
        assert result.ci_lower is not None and result.ci_upper is not None
        assert result.ci_lower < result.ci_upper  # never a zero-width interval

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
