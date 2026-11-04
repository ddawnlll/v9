"""M0-07: same-bar metrics fail closed; audit verdicts are independently earned.

Every assertion here is about what the tape can prove. The entry bar's high/low
were produced before the position existed, so mutating them must not move any
measured excursion, and an ambiguous same-bar stop/target must be reported as
``NOT_IDENTIFIABLE`` even though the STOP-FIRST contract labels the path.
"""

from __future__ import annotations

import pytest

from v8_next.audit.trade_metrics import (
    POST_ENTRY_ELIGIBILITY,
    TradeFacts,
    compute_trade_metrics,
    entry_eligibility_boundary,
    post_entry_window,
    stop_first_path,
)
from v8_next.audit.verdicts import (
    AUDIT_COMPLETE,
    REQUIRED_SOURCE_IDS,
    AuditVerdict,
    AuditVerdictError,
    VerdictState,
    audit_label,
    derive_verdict,
    require_audit_complete,
    verdict_from_trade_metrics,
)

HOUR = 3_600_000_000_000


class Candle:
    """Bar with an explicit start; the entry bar's future extrema stay inside it."""

    def __init__(
        self,
        start_ns: int,
        end_ns: int,
        o: float,
        h: float,
        lo: float,
        c: float,
    ) -> None:
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.open = o
        self.high = h
        self.low = lo
        self.close = c


class BareCandle:
    """Tape fixture with no ``start_ns``, as older replay tapes provide."""

    def __init__(self, end_ns: int, o: float, h: float, lo: float, c: float) -> None:
        self.end_ns = end_ns
        self.open = o
        self.high = h
        self.low = lo
        self.close = c


def _bar(index: int, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(index * HOUR, (index + 1) * HOUR, o, h, lo, c)


def _facts(**overrides: object) -> TradeFacts:
    values: dict[str, object] = {
        "position_id": "P1",
        "instrument_id": "X",
        "side": "LONG",
        "quantity": 1.0,
        "entry_price": 100.0,
        "opened_ns": HOUR,
        "closed_ns": 2 * HOUR,
        "exit_price": 102.0,
        "initial_stop_price": 90.0,
        "initial_target_price": 120.0,
        "opportunity_id": "OPP1",
        "decision_id": "D1",
        "extra": {
            "campaign_id": "C0001",
            "position_orders": [{"type": "STOP_MARKET", "status": "FILLED"}],
        },
    }
    values.update(overrides)
    return TradeFacts(**values)  # type: ignore[arg-type]


def _closed_row() -> dict[str, object]:
    candles = [_bar(0, 100.0, 150.0, 40.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    facts = _facts(native_realized_pnl=1.0)
    return compute_trade_metrics(facts=facts, candles=candles)


# --------------------------------------------------------------------------- #
# 1. post-entry eligibility lock
# --------------------------------------------------------------------------- #
def test_post_entry_window_excludes_entry_bar_and_honours_exit() -> None:
    candles = [
        _bar(0, 100.0, 150.0, 40.0, 100.0),
        _bar(1, 100.0, 104.0, 96.0, 102.0),
        _bar(2, 102.0, 105.0, 99.0, 104.0),
    ]
    assert entry_eligibility_boundary(HOUR) == HOUR
    window = post_entry_window(candles, opened_ns=HOUR)
    assert [c.end_ns for c in window] == [2 * HOUR, 3 * HOUR]
    assert post_entry_window(candles, opened_ns=HOUR, exit_ns=2 * HOUR) == [candles[1]]
    # Pure helper: the tape itself is untouched.
    assert len(candles) == 3
    assert candles[0].high == 150.0


def test_post_entry_window_excludes_a_bar_containing_the_entry_instant() -> None:
    wide = Candle(0, 2 * HOUR, 100.0, 150.0, 40.0, 100.0)
    following = Candle(2 * HOUR, 3 * HOUR, 100.0, 104.0, 96.0, 102.0)
    window = post_entry_window([wide, following], opened_ns=HOUR)  # entry mid-bar
    assert window == [following]


def test_post_entry_window_fails_closed_without_a_known_bar_start() -> None:
    bare = [BareCandle(HOUR, 100.0, 150.0, 40.0, 100.0), BareCandle(2 * HOUR, 100.0, 104.0, 96.0, 102.0)]
    assert post_entry_window(bare, opened_ns=HOUR) == [bare[1]]


def test_entry_bar_future_high_low_mutation_cannot_change_metrics() -> None:
    candles = [_bar(0, 100.0, 150.0, 40.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    facts = _facts(native_realized_pnl=1.0)
    baseline = compute_trade_metrics(facts=facts, candles=candles)

    mutated = [_bar(0, 100.0, 10_000.0, 1.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    after = compute_trade_metrics(facts=facts, candles=mutated)

    assert after == baseline
    # The pre-entry information itself is untouched and the excursion is the
    # post-entry one only (bar 0's 150/40 never become MFE/MAE).
    assert baseline["entry_price"] == 100.0
    assert baseline["opened_ns"] == HOUR
    assert baseline["mfe_price"] == 4.0
    assert baseline["mae_price"] == -4.0
    assert baseline["entry_bars_excluded"] == 1
    assert baseline["entry_eligibility_rule"] == POST_ENTRY_ELIGIBILITY


def test_mid_bar_entry_mutation_cannot_change_metrics() -> None:
    facts = _facts(opened_ns=HOUR, closed_ns=3 * HOUR, exit_price=102.0, native_realized_pnl=1.0)
    candles = [
        Candle(0, 2 * HOUR, 100.0, 150.0, 40.0, 100.0),  # contains the entry instant
        Candle(2 * HOUR, 3 * HOUR, 100.0, 104.0, 96.0, 102.0),
    ]
    baseline = compute_trade_metrics(facts=facts, candles=candles)
    mutated = [
        Candle(0, 2 * HOUR, 100.0, 9_999.0, 2.0, 100.0),
        Candle(2 * HOUR, 3 * HOUR, 100.0, 104.0, 96.0, 102.0),
    ]
    assert compute_trade_metrics(facts=facts, candles=mutated) == baseline
    assert baseline["mfe_price"] == 4.0
    assert baseline["mae_price"] == -4.0
    assert baseline["entry_bars_excluded"] == 1


def test_no_stop_path_cannot_borrow_entry_bar_extrema() -> None:
    candles = [_bar(0, 100.0, 150.0, 40.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    row = compute_trade_metrics(facts=_facts(initial_stop_price=None), candles=candles)
    assert row["path_model_applied"] == "NONE_NO_STOP"
    assert row["mfe_price"] == 4.0
    assert row["mae_price"] == -4.0
    assert row["risk_invalid_reason"] == "MISSING_INITIAL_STOP"


def test_single_bar_campaign_without_post_entry_bar_is_not_identifiable() -> None:
    candles = [_bar(0, 100.0, 150.0, 40.0, 100.0)]
    facts = _facts(
        initial_stop_price=None,
        closed_ns=HOUR,
        exit_price=101.0,
        initial_target_price=None,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["mfe_price"] is None
    assert row["mae_price"] is None
    assert row["metric_unavailable_reason"] == "NO_POST_ENTRY_BAR"
    assert row["measured_window_bars"] == 0
    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.mechanical is VerdictState.NOT_IDENTIFIABLE
    assert "ENTRY_BAR_ONLY" in verdict.labels


def test_existing_fully_evidenced_semantics_are_preserved() -> None:
    candles = [_bar(0, 100.0, 110.0, 95.0, 105.0), _bar(1, 105.0, 120.0, 100.0, 118.0)]
    facts = _facts(
        closed_ns=2 * HOUR,
        exit_price=115.0,
        initial_stop_price=90.0,
        initial_target_price=120.0,
        opportunity_id=None,
        decision_id=None,
        extra={"campaign_id": "C0001"},
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    # Same numbers the pre-M0-07 suite asserts in tests/test_audit_native.py.
    assert row["mfe_price"] == 20.0
    assert row["mae_price"] == 0.0
    assert row["mfe_r"] == 2.0
    assert row["realized_gross_r"] == 1.5
    assert row["realized_net_r"] == 1.5
    assert row["measured_window_bars"] == 1


# --------------------------------------------------------------------------- #
# 2. same-bar ties: STOP_FIRST label, NOT_IDENTIFIABLE verdict
# --------------------------------------------------------------------------- #
def test_ambiguous_same_bar_stop_and_target_keeps_stop_first_label_but_fails_identifiability() -> None:
    candles = [_bar(0, 100.0, 100.0, 100.0, 100.0), _bar(1, 100.0, 115.0, 80.0, 112.0)]
    facts = _facts(
        closed_ns=2 * HOUR,
        exit_price=95.0,
        initial_stop_price=95.0,
        initial_target_price=110.0,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["path_model_outcome"] == "STOP_FIRST"
    assert row["tie_resolution"] == "STOP_FIRST_DECLARED"
    assert row["same_bar_stop_and_target"] is True
    assert row["mae_r"] == -1.0
    assert row["mfe_r"] == 0.0

    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.mechanical is VerdictState.NOT_IDENTIFIABLE
    assert "STOP_FIRST" in verdict.labels
    assert "MECHANICAL_SAME_BAR_STOP_AND_TARGET_UNRESOLVED" in verdict.reasons


def test_unambiguous_stop_bar_is_identifiable_and_labelled_stop_first() -> None:
    candles = [_bar(0, 100.0, 100.0, 100.0, 100.0), _bar(1, 100.0, 104.0, 94.0, 96.0)]
    facts = _facts(
        closed_ns=2 * HOUR,
        exit_price=95.0,
        initial_stop_price=95.0,
        initial_target_price=110.0,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["path_model_outcome"] == "STOP_FIRST"
    assert row["same_bar_stop_and_target"] is False
    assert row["tie_resolution"] is None
    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.mechanical is VerdictState.PASS


def test_stop_first_path_reports_the_eligibility_boundary() -> None:
    candles = [_bar(0, 100.0, 100.0, 100.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    path = stop_first_path(
        candles=candles,
        direction=1,
        entry=100.0,
        stop=90.0,
        target=120.0,
        opened_ns=HOUR,
        exit_ns=2 * HOUR,
    )
    assert path is not None
    assert path["eligibility_boundary_ns"] == HOUR
    assert path["bars_measured"] == 1
    assert path["same_bar_stop_and_target"] is False


# --------------------------------------------------------------------------- #
# 3. audit completeness cannot be claimed without source identities
# --------------------------------------------------------------------------- #
def test_missing_source_ids_can_never_yield_audit_complete() -> None:
    row = _closed_row()
    verdict = verdict_from_trade_metrics(
        row,
        source_ids={"position_id": "P1", "campaign_id": None, "opportunity_id": None,
                    "decision_id": None},
        pit=VerdictState.PASS,
    )
    assert verdict.completeness is VerdictState.INCOMPLETE
    assert verdict.audit_complete is False
    assert audit_label(verdict) != AUDIT_COMPLETE
    assert any(reason.startswith("MISSING_SOURCE_ID:") for reason in verdict.reasons)
    with pytest.raises(AuditVerdictError) as exc:
        require_audit_complete(verdict)
    assert exc.value.code == "AUDIT_NOT_COMPLETE"


def test_constructing_a_complete_verdict_without_source_ids_is_rejected() -> None:
    with pytest.raises(AuditVerdictError) as exc:
        AuditVerdict(
            mechanical=VerdictState.PASS,
            economic=VerdictState.PASS,
            pit=VerdictState.PASS,
            completeness=VerdictState.PASS,
            source_ids=(("position_id", "P1"),),
        )
    assert exc.value.code == "COMPLETENESS_WITHOUT_SOURCE_IDS"


def test_complete_verdict_requires_every_dimension_to_have_run() -> None:
    complete_ids = tuple((key, "value") for key in REQUIRED_SOURCE_IDS)
    with pytest.raises(AuditVerdictError) as exc:
        AuditVerdict(
            mechanical=VerdictState.PASS,
            economic=VerdictState.PASS,
            pit=VerdictState.NOT_RUN,
            completeness=VerdictState.PASS,
            source_ids=complete_ids,
        )
    assert exc.value.code == "COMPLETENESS_WITH_UNRESOLVED_DIMENSION"


def test_fully_evidenced_campaign_reaches_audit_complete() -> None:
    row = _closed_row()
    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.mechanical is VerdictState.PASS
    assert verdict.economic is VerdictState.PASS
    assert verdict.completeness is VerdictState.PASS
    assert verdict.audit_complete is True
    assert audit_label(verdict) == AUDIT_COMPLETE
    assert require_audit_complete(verdict) is verdict


def test_pit_not_run_blocks_completion_by_default() -> None:
    row = _closed_row()
    verdict = verdict_from_trade_metrics(row)  # pit defaults to NOT_RUN
    assert verdict.pit is VerdictState.NOT_RUN
    assert verdict.completeness is VerdictState.NOT_RUN
    assert audit_label(verdict) == "AUDIT_NOT_RUN"


# --------------------------------------------------------------------------- #
# 4. negative cases yield INVALID / NOT_IDENTIFIABLE, never PASS
# --------------------------------------------------------------------------- #
def test_zero_stop_distance_is_invalid_not_pass() -> None:
    candles = [_bar(0, 100.0, 101.0, 99.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    row = compute_trade_metrics(facts=_facts(initial_stop_price=100.0), candles=candles)
    assert row["risk_denominator_status"] == "INVALID"
    assert row["risk_invalid_reason"] == "ZERO_STOP_DISTANCE"
    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.mechanical is VerdictState.INVALID
    assert verdict.completeness is VerdictState.INVALID
    assert verdict.audit_complete is False


def test_missing_entry_fields_are_invalid_not_pass() -> None:
    candles = [_bar(0, 100.0, 101.0, 99.0, 100.0)]
    row = compute_trade_metrics(
        facts=_facts(entry_price=None, extra={"campaign_id": "C0001"}),
        candles=candles,
    )
    assert row["metric_unavailable_reason"] == "MISSING_ENTRY_OR_QUANTITY"
    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.mechanical is VerdictState.INVALID
    assert verdict.completeness is VerdictState.INVALID


def test_no_protective_order_evidence_blocks_the_economic_claim() -> None:
    row = _closed_row()
    unprotected = dict(row)
    unprotected["protection_evidence"] = "NO_NATIVE_PROTECTIVE_ORDER"
    verdict = verdict_from_trade_metrics(unprotected, pit=VerdictState.PASS)
    assert verdict.economic is VerdictState.INCOMPLETE
    assert "ECONOMIC_PROTECTIVE_EVIDENCE_MISSING" in verdict.reasons
    assert verdict.completeness is VerdictState.INCOMPLETE


def test_open_campaign_is_not_economic_proof() -> None:
    candles = [_bar(0, 100.0, 101.0, 99.0, 100.0), _bar(1, 100.0, 104.0, 96.0, 102.0)]
    row = compute_trade_metrics(
        facts=_facts(closed_ns=None, exit_price=None), candles=candles
    )
    assert row["realized_net_pnl_usdt"] is None
    verdict = verdict_from_trade_metrics(row, pit=VerdictState.PASS)
    assert verdict.economic is VerdictState.INCOMPLETE
    assert "ECONOMIC_OUTCOME_NOT_REALIZED" in verdict.reasons


def test_derive_verdict_completeness_precedence_is_fail_closed() -> None:
    ids = {key: "v" for key in REQUIRED_SOURCE_IDS}
    blocked = derive_verdict(
        mechanical=VerdictState.PASS,
        economic=VerdictState.PASS,
        pit=VerdictState.BLOCKED,
        source_ids=ids,
    )
    assert blocked.completeness is VerdictState.BLOCKED
    assert blocked.audit_complete is False

    unidentifiable = derive_verdict(
        mechanical=VerdictState.NOT_IDENTIFIABLE,
        economic=VerdictState.PASS,
        pit=VerdictState.PASS,
        source_ids=ids,
    )
    # The audit itself ran and is traceable; the path fact is simply not identified.
    assert unidentifiable.completeness is VerdictState.PASS
    assert unidentifiable.mechanical is VerdictState.NOT_IDENTIFIABLE

    missing = derive_verdict(
        mechanical=VerdictState.PASS,
        economic=VerdictState.PASS,
        pit=VerdictState.PASS,
        source_ids={"position_id": "P1"},
    )
    assert missing.completeness is VerdictState.INCOMPLETE
