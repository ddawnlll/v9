"""NX03 (#424) — the historical walk-forward plan, separate from the forward freeze.

Evidence classes:

* **mechanics** — plan invariants (train<scored, warmup inside the past, purge from
  the measured contract, disjoint folds), measured warmup/purge derivation, store
  immutability + read-back, and the proof that the historical path neither writes
  nor relaxes the prospective freeze tables.
* **evaluative** — the plan is built from the real NX01 calendar on the real tape:
  the 24/12/12 windows, the closed final (no protected OOS) and the measured tail
  role. Skips when the tape is absent.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from v8_next.economics.grammar import POLICY_HORIZON_BARS, POLICY_REQUIRED_BARS
from v8_next.economics.protection import PROTECTION_TTL_BARS
from v8_next.evaluation.historical_plan import (
    HISTORICAL_PLAN_VERSION,
    build_historical_plan,
    calendar_digest,
    derive_history_requirements,
    freeze_historical_plan,
    readback_historical_plan,
)
from v8_next.evaluation.store import ResearchStore
from v8_next.evaluation.tape_identity import (
    BurnSegment,
    TapeInventory,
    _add_months,
    burn_segments,
    inventory_tape,
    measure_covering_tape,
    policy_lineage_burn_table,
    swing_calendar,
)

REPO_ROOT = Path("/Users/hootie/src/v8")
TAPE_DIR = REPO_ROOT / "research" / "tape" / "multi-1h-4y"
TAPE = TAPE_DIR / "tape.jsonl"
TAIL_TAPES = (
    REPO_ROOT / "research" / "tape" / "quad-1h-12m",
    REPO_ROOT / "research" / "tape" / "btcusdt-1h-12m",
    REPO_ROOT / "research" / "tape" / "sol-dev-solusdt-2025-07-2026-07",
)
HOUR_NS = 3_600 * 10**9
POLICIES = ("range-breakout-48-v1", "trend-continuation-v2", "mean-reversion-v2")


def _segment(segment_id: str, role: str, start_ms: int, end_ms: int) -> BurnSegment:
    return BurnSegment(
        segment_id=segment_id,
        role=role,  # type: ignore[arg-type]
        start_ms=start_ms,
        end_ms=end_ms,
        instruments=("BTCUSDT",),
        evidence=("nx03 mechanics fixture",),
    )


def _inventory_stub(start_ms: int, end_ms: int) -> TapeInventory:
    return TapeInventory(
        tape_path="mechanics-only",
        tape_sha256="0" * 64,
        tape_bytes=0,
        total_rows=0,
        channel_counts={},
        symbols=("BTCUSDT",),
        interval="1h",
        schema_versions=("mechanics-only",),
        legs=(),
        mark_price_channels=(),
        absence_notes=(),
        unparsable_rows=0,
        unclosed_kline_rows=0,
        window_start_ms=start_ms,
        window_end_ms=end_ms - 1,
    )


def _mechanics_calendar(final_start_ms: int, *, protected: bool = False):
    start = _add_months(final_start_ms, -36)
    end = _add_months(final_start_ms, 12)
    segments = (
        _segment("DEV", "BURNED_DIAGNOSTIC", start, final_start_ms - 1),
        _segment(
            "TAIL",
            "PROTECTED_OOS" if protected else "USAGE_UNKNOWN",
            final_start_ms,
            end - 1,
        ),
    )
    calendar = swing_calendar(inventory=_inventory_stub(start, end), segments=segments)
    assert calendar.final.start_ms == final_start_ms
    return calendar


def _plan(calendar, **overrides):
    kwargs = dict(
        calendar=calendar,
        dataset_id="sha256:mechanics",
        instrument="BTCUSDT-PERP.BINANCE",
        policies=POLICIES,
        baseline=POLICIES[0],
        initial_balance_usdt="10000",
        maker_fee="0.0002",
        taker_fee="0.0005",
        bar_ns=HOUR_NS,
        aggregation="INDEPENDENT_FOLD_RESET",
        plan_id="NX03-MECH-01",
        code_and_lock_hash="lock-mechanics",
    )
    kwargs.update(overrides)
    return build_historical_plan(**kwargs)


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_history_requirements_come_from_the_real_contracts() -> None:
    requirements = derive_history_requirements(POLICIES)
    assert requirements.required_bars == {
        policy: POLICY_REQUIRED_BARS[policy] for policy in POLICIES
    }
    assert requirements.warmup_bars == max(
        max(POLICY_REQUIRED_BARS[p] for p in POLICIES), requirements.session_bars
    )
    assert requirements.purge_bars == max(
        max(POLICY_HORIZON_BARS[p] for p in POLICIES),
        PROTECTION_TTL_BARS["squeeze"],
    )
    # the range-breakout policy opens a 336-bar interval, so the purge is 336
    assert requirements.purge_bars == 336
    # and its 49-bar range is the longest history this policy set reads
    assert requirements.warmup_bars == 49


def test_unknown_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown grammar policy"):
        derive_history_requirements(("not-a-policy",))


def test_every_fold_separates_training_warmup_and_scoring() -> None:
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar)
    assert plan.version == HISTORICAL_PLAN_VERSION
    assert plan.final_eligible is False
    assert plan.final_eligibility_reason is not None
    assert "NO_PROTECTED_FINAL" in plan.final_eligibility_reason
    assert [f.fold_id for f in plan.folds] == ["FOLD_1", "FOLD_2", "FOLD_3", "FOLD_4"]
    for window in plan.folds:
        assert window.train_end_ns < window.scored_start_ns
        assert window.train_end_ns < window.warmup_start_ns <= window.warmup_end_ns
        assert window.warmup_end_ns < window.scored_start_ns
        assert window.purge_bars == plan.history.purge_bars
        assert (
            window.scored_start_ns - window.train_end_ns
        ) == window.purge_bars * HOUR_NS
    # folds are chronological and disjoint
    for earlier, later in zip(plan.folds, plan.folds[1:], strict=False):
        assert earlier.scored_end_ns < later.scored_start_ns


def test_verify_rejects_a_warmup_that_leaks_into_training() -> None:
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar)
    first = plan.folds[0]
    leaking = replace(first, warmup_start_ns=first.train_end_ns - 1)
    broken = replace(plan, folds=(leaking, *plan.folds[1:]))
    ok, reason = broken.verify()
    assert not ok
    assert reason.startswith("WARMUP_OUTSIDE_PAST")


def test_verify_rejects_a_train_window_reaching_into_scoring() -> None:
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar)
    first = plan.folds[0]
    broken = replace(
        plan, folds=(replace(first, train_end_ns=first.scored_start_ns), *plan.folds[1:])
    )
    ok, reason = broken.verify()
    assert not ok
    assert reason.startswith("TRAIN_OVERLAPS_SCORED")


def test_final_is_built_only_when_the_tape_role_is_protected() -> None:
    protected = _mechanics_calendar(2_000_000_000_000, protected=True)
    plan = _plan(protected)
    assert plan.final_eligible is True
    final = plan.fold("FINAL")
    assert final.tape_role == "PROTECTED_OOS"
    assert final.train_end_ns < final.scored_start_ns
    # the same plan on a burned tail refuses the final outright
    burned = _mechanics_calendar(2_000_000_000_000, protected=False)
    assert _plan(burned).final_eligible is False
    assert not any(f.fold_id == "FINAL" for f in _plan(burned).folds)


def test_final_claim_without_a_protected_role_is_refused() -> None:
    calendar = _mechanics_calendar(2_000_000_000_000, protected=True)
    plan = _plan(calendar)
    forged = replace(plan, folds=tuple(replace(f, tape_role="BURNED") for f in plan.folds))
    ok, reason = forged.verify()
    assert not ok
    assert reason == "FINAL_CLAIMED_WITHOUT_PROTECTED_TAPE_ROLE"


def test_freezing_is_immutable_and_readback_rederives_the_digest(tmp_path: Path) -> None:
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar)
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        digest = freeze_historical_plan(store, plan, registered_ns=1)
        assert digest == plan.digest()
        # idempotent retry
        assert freeze_historical_plan(store, plan, registered_ns=2) == digest
        re_read = readback_historical_plan(store, plan.plan_id)
        assert re_read.as_dict() == plan.as_dict()
        assert re_read.digest() == digest

        other = _plan(calendar, plan_id="NX03-MECH-02", baseline=POLICIES[1])
        frozen = freeze_historical_plan(store, other, registered_ns=3)
        assert frozen == other.digest()
        assert readback_historical_plan(store, "NX03-MECH-02").baseline == POLICIES[1]
    finally:
        store.close()


def test_historical_path_never_touches_the_forward_tables(tmp_path: Path) -> None:
    """The prospective freeze keeps its own clock rule; history uses another table."""
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar)
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        freeze_historical_plan(store, plan, registered_ns=1)
        assert store.db.execute("SELECT COUNT(*) FROM forward_plans").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM forward_bindings").fetchone()[0] == 0
        assert store.get_historical_plan(plan.plan_id) is not None
        assert store.get_historical_plan("absent") is None
    finally:
        store.close()


def test_aggregation_and_train_window_are_explicit() -> None:
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar, aggregation="CONTINUOUS_PORTFOLIO")
    payload = plan.as_dict()
    assert payload["aggregation"] == "CONTINUOUS_PORTFOLIO"
    assert payload["train_window"] == "EXPANDING_FROM_DATASET_START"
    assert payload["plan_digest"] == plan.digest()
    other = _plan(calendar, aggregation="INDEPENDENT_FOLD_RESET")
    assert other.digest() != plan.digest()


def test_unsupported_plan_version_is_refused() -> None:
    calendar = _mechanics_calendar(2_000_000_000_000)
    plan = _plan(calendar)
    ok, reason = replace(plan, version="v87-historical-plan-v0").verify()
    assert not ok
    assert reason.startswith("UNSUPPORTED_PLAN_VERSION")


# --------------------------------------------------------------------------- #
# evaluative — real NX01 calendar on the real tape
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def real_plan():
    if not TAPE.is_file():
        pytest.skip(f"four-year tape absent at {TAPE}")
    inventory = inventory_tape(TAPE)
    tail = [measure_covering_tape(p) for p in TAIL_TAPES if (p / "tape.jsonl").is_file()]
    verified = policy_lineage_burn_table(repo_root=REPO_ROOT, tape_path=TAPE)
    segments = burn_segments(
        inventory=inventory,
        verified_accesses=verified,
        tail_burned_from_ms=min(t.window_start_ms for t in tail),
        tail_evidence=tuple(t.path for t in tail),
    )
    calendar = swing_calendar(inventory=inventory, segments=segments)
    plan = build_historical_plan(
        calendar=calendar,
        dataset_id=inventory.data_id,
        instrument="BTCUSDT-PERP.BINANCE",
        policies=("range-breakout-48-v1", "trend-continuation-v2", "mean-reversion-v2"),
        baseline="range-breakout-48-v1",
        initial_balance_usdt="10000",
        maker_fee="0.0002",
        taker_fee="0.0005",
        bar_ns=HOUR_NS,
        aggregation="INDEPENDENT_FOLD_RESET",
        plan_id="NX03-REAL-01",
    )
    return plan, calendar, inventory


def test_real_plan_windows_are_the_nx01_calendar(real_plan) -> None:
    plan, calendar, inventory = real_plan
    assert plan.dataset_id == inventory.data_id
    assert plan.calendar_digest == calendar_digest(calendar)
    assert plan.folds[0].scored_start_ns == calendar.folds[0].start_ms * 1_000_000
    assert plan.folds[-1].scored_end_ns == (calendar.folds[-1].end_ms + 1) * 1_000_000 - 1
    assert plan.as_dict()["folds"][0]["scored_start_utc"] == "2024-07-01T00:00:00Z"
    assert plan.as_dict()["folds"][-1]["scored_end_utc"] == "2025-06-30T23:59:59.999000Z"


def test_real_plan_closes_the_final_and_records_the_measured_tail_role(real_plan) -> None:
    plan, _, _ = real_plan
    assert plan.final_eligible is False
    assert plan.final_eligibility_reason is not None
    assert "NO_PROTECTED_FINAL" in plan.final_eligibility_reason
    assert "TAIL_BURNED" in plan.final_eligibility_reason
    assert not any(f.fold_id == "FINAL" for f in plan.folds)
    assert plan.history.warmup_bars == 49
    assert plan.history.purge_bars == 336

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
