"""M3-06: PIT altcoin-perpetual universe eligibility and 20-USDT order feasibility.

The rule engine is exercised against synthetic metadata fixtures. No altcoin tape exists in
this repository, so *economic* qualification is NOT_RUN (M4-04 dependency) and every report
says so; nothing here is an economic or live-trading claim.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import v8_next.adapters.universe as universe_module
from v8_next.adapters.universe import (
    MAX_VOLUME_AGE_NS,
    CoverageState,
    EligibilityStatus,
    FeasibilityVerdict,
    InstrumentMetadata,
    ResolutionStatus,
    UniverseReason,
    UniverseReportStatus,
    eligibility,
    feasibility,
    qualify_universe,
    resolve_metadata,
)

T0 = 1_700_000_000_000_000_000
SECOND = 1_000_000_000
AS_OF = T0 + 2 * SECOND


def _meta(**overrides: Any) -> InstrumentMetadata:
    base: dict[str, Any] = {
        "symbol": "ALTUSDT",
        "venue": "BINANCE",
        "contract_type": "PERPETUAL",
        "listed_at": T0,
        "delisted_at": None,
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "min_qty": Decimal("0.001"),
        "quote_volume_24h": Decimal("2500000"),
        "quote_volume_as_of_ns": AS_OF,
        "leverage_max": Decimal("20"),
        "maintenance_margin_rate": Decimal("0.005"),
        "metadata_version": "v1",
        "available_at": T0 - 1_000_000,
    }
    base.update(overrides)
    return InstrumentMetadata(**base)


def _feasible_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "equity": Decimal("20"),
        "target_notional": Decimal("10"),
        "entry_price": Decimal("2.00"),
        "fee_bps": Decimal("2"),
        "slippage_bps": Decimal("1"),
        "stop_distance_bps": Decimal("100"),
        "leverage": Decimal("10"),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------------------
# Vocabulary and construction
# --------------------------------------------------------------------------------------------------


def test_eligibility_status_vocabulary_is_exact() -> None:
    assert {status.value for status in EligibilityStatus} == {
        "ELIGIBLE",
        "NOT_LISTED",
        "DELISTED",
        "STALE_VOLUME",
        "BELOW_MIN_NOTIONAL",
        "CONTRACT_CHANGED",
        "UNKNOWN_METADATA",
        "INSUFFICIENT_EQUITY",
        "SIZE_BELOW_MIN",
        "EXCESSIVE_FRICTION",
    }


def test_metadata_rejects_delisting_before_listing() -> None:
    with pytest.raises(ValidationError):
        _meta(delisted_at=T0 - SECOND)


def test_metadata_rejects_volume_without_snapshot_time() -> None:
    with pytest.raises(ValidationError):
        _meta(quote_volume_as_of_ns=None)


def test_metadata_terms_digest_ignores_decimal_exponent() -> None:
    a = _meta(tick_size=Decimal("0.010"), step_size=Decimal("0.100"))
    b = _meta(tick_size=Decimal("0.01"), step_size=Decimal("0.1"))
    assert a.contract_digest == b.contract_digest
    assert a.contract_digest != _meta(tick_size=Decimal("0.02")).contract_digest


# --------------------------------------------------------------------------------------------------
# Listing / delisting boundaries
# --------------------------------------------------------------------------------------------------


def test_before_listing_is_not_listed() -> None:
    verdict = eligibility(_meta(), as_of_ns=T0 - 1)
    assert verdict.status is EligibilityStatus.NOT_LISTED
    assert verdict.reasons == (UniverseReason.NOT_YET_LISTED,)
    assert not verdict.eligible


def test_exactly_at_listing_is_evaluable() -> None:
    metadata = _meta(quote_volume_as_of_ns=T0)
    verdict = eligibility(metadata, as_of_ns=T0)
    assert verdict.status is EligibilityStatus.ELIGIBLE
    assert verdict.reasons == (UniverseReason.ELIGIBLE,)
    assert verdict.volume_age_ns == 0


def test_exactly_at_delisting_is_delisted() -> None:
    delisted = T0 + 5 * SECOND
    metadata = _meta(delisted_at=delisted)
    assert eligibility(metadata, as_of_ns=delisted).status is EligibilityStatus.DELISTED
    assert eligibility(metadata, as_of_ns=delisted).reasons == (UniverseReason.DELISTED_AT_INSTANT,)
    still_trading = eligibility(metadata, as_of_ns=delisted - 1)
    assert still_trading.status is EligibilityStatus.ELIGIBLE


def test_metadata_that_is_not_yet_knowable_is_unknown() -> None:
    metadata = _meta(available_at=AS_OF + 1)
    verdict = eligibility(metadata, as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.UNKNOWN_METADATA
    assert verdict.reasons == (UniverseReason.METADATA_NOT_AVAILABLE_YET,)


def test_future_metadata_never_used_even_with_perfect_volume() -> None:
    metadata = _meta(available_at=AS_OF + SECOND, quote_volume_as_of_ns=AS_OF)
    verdict = eligibility(metadata, as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.UNKNOWN_METADATA
    assert not verdict.eligible


# --------------------------------------------------------------------------------------------------
# Volume fresheness and evidence absence
# --------------------------------------------------------------------------------------------------


def test_fresh_volume_snapshot_is_eligible() -> None:
    verdict = eligibility(_meta(quote_volume_as_of_ns=AS_OF - 1), as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.ELIGIBLE


def test_exactly_max_volume_age_is_still_eligible() -> None:
    verdict = eligibility(_meta(quote_volume_as_of_ns=AS_OF - MAX_VOLUME_AGE_NS), as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.ELIGIBLE
    assert verdict.volume_age_ns == MAX_VOLUME_AGE_NS


def test_stale_volume_snapshot_is_rejected() -> None:
    verdict = eligibility(
        _meta(quote_volume_as_of_ns=AS_OF - MAX_VOLUME_AGE_NS - 1), as_of_ns=AS_OF
    )
    assert verdict.status is EligibilityStatus.STALE_VOLUME
    assert verdict.reasons == (UniverseReason.VOLUME_SNAPSHOT_STALE,)


def test_missing_volume_snapshot_is_unknown_not_eligible() -> None:
    verdict = eligibility(_meta(quote_volume_24h=None, quote_volume_as_of_ns=None), as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.UNKNOWN_METADATA
    assert verdict.reasons == (UniverseReason.NO_VOLUME_SNAPSHOT,)
    assert verdict.quote_volume_24h is None


def test_future_volume_snapshot_is_unknown() -> None:
    verdict = eligibility(_meta(quote_volume_as_of_ns=AS_OF + 1), as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.UNKNOWN_METADATA
    assert verdict.reasons == (UniverseReason.VOLUME_SNAPSHOT_FUTURE,)


def test_non_perpetual_contract_is_unknown() -> None:
    verdict = eligibility(_meta(contract_type="SPOT"), as_of_ns=AS_OF)
    assert verdict.status is EligibilityStatus.UNKNOWN_METADATA
    assert verdict.reasons == (UniverseReason.UNSUPPORTED_CONTRACT_TYPE,)


# --------------------------------------------------------------------------------------------------
# Contract change / metadata version resolution
# --------------------------------------------------------------------------------------------------


def test_ambiguous_as_of_versions_are_contract_changed() -> None:
    v1 = _meta(metadata_version="v1", tick_size=Decimal("0.01"))
    v2 = _meta(metadata_version="v2", tick_size=Decimal("0.05"))
    verdict = eligibility(v1, as_of_ns=AS_OF, history=(v1, v2))
    assert verdict.status is EligibilityStatus.CONTRACT_CHANGED
    assert verdict.reasons == (UniverseReason.AMBIGUOUS_AS_OF_VERSION,)


def test_same_version_with_two_term_sets_is_contract_changed() -> None:
    first = _meta(metadata_version="v1", tick_size=Decimal("0.01"))
    second = _meta(metadata_version="v1", tick_size=Decimal("0.02"))
    verdict = eligibility(first, as_of_ns=AS_OF, history=(first, second))
    assert verdict.status is EligibilityStatus.CONTRACT_CHANGED
    assert verdict.reasons == (UniverseReason.CONTRADICTORY_METADATA_VERSION,)


def test_stale_version_supplied_against_history_is_contract_changed() -> None:
    v1 = _meta(metadata_version="v1", available_at=T0 - SECOND)
    v2 = _meta(metadata_version="v2", available_at=T0 + SECOND, tick_size=Decimal("0.02"))
    verdict = eligibility(v1, as_of_ns=AS_OF, history=(v1, v2))
    assert verdict.status is EligibilityStatus.CONTRACT_CHANGED
    assert verdict.reasons == (UniverseReason.SUPERSEDED_METADATA_VERSION,)
    assert eligibility(v2, as_of_ns=AS_OF, history=(v1, v2)).status is EligibilityStatus.ELIGIBLE


def test_announced_future_contract_change_is_not_applied_early() -> None:
    v1 = _meta(metadata_version="v1", available_at=T0 - SECOND)
    v2 = _meta(metadata_version="v2", available_at=T0 + 10 * SECOND, tick_size=Decimal("0.02"))
    verdict = eligibility(v1, as_of_ns=AS_OF, history=(v1, v2))
    assert verdict.status is EligibilityStatus.ELIGIBLE


def test_history_with_only_future_records_is_unknown() -> None:
    future = _meta(metadata_version="v2", available_at=AS_OF + SECOND)
    supplied = _meta(metadata_version="v1")
    verdict = eligibility(supplied, as_of_ns=AS_OF, history=(future,))
    assert verdict.status is EligibilityStatus.UNKNOWN_METADATA
    assert verdict.reasons == (UniverseReason.METADATA_NOT_AVAILABLE_YET,)


def test_resolve_metadata_is_deterministic_and_rejects_mixed_symbols() -> None:
    v1 = _meta(metadata_version="v1", available_at=T0 - SECOND)
    v2 = _meta(metadata_version="v2", available_at=T0)
    resolution = resolve_metadata((v2, v1), as_of_ns=AS_OF)
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.record is not None
    assert resolution.record.metadata_version == "v2"
    with pytest.raises(ValueError):
        resolve_metadata((v1, _meta(symbol="OTHERUSDT")), as_of_ns=AS_OF)


# --------------------------------------------------------------------------------------------------
# Order feasibility: quantization, minimums, equity, friction, liquidation
# --------------------------------------------------------------------------------------------------


def test_quantizes_down_to_tick_and_step() -> None:
    verdict = feasibility(
        _meta(step_size=Decimal("0.001")), **_feasible_kwargs(entry_price=Decimal("2.345"))
    )
    assert verdict.status is EligibilityStatus.ELIGIBLE
    assert verdict.feasible
    spec = verdict.spec
    assert spec is not None
    assert spec.price == Decimal("2.34")
    assert spec.quantity == Decimal("4.273")
    assert spec.notional == Decimal("9.99882")
    assert verdict.quantized_notional == Decimal("9.99882")
    assert verdict.initial_margin == Decimal("0.999882")
    assert spec.notional <= Decimal("10")
    assert spec.advisory_only is True


def test_target_below_min_notional_is_rejected() -> None:
    verdict = feasibility(
        _meta(min_notional=Decimal("5")), **_feasible_kwargs(target_notional=Decimal("3"))
    )
    assert verdict.status is EligibilityStatus.BELOW_MIN_NOTIONAL
    assert verdict.reasons == (UniverseReason.TARGET_BELOW_MIN_NOTIONAL,)
    assert verdict.spec is None


def test_twenty_usdt_equity_below_required_margin_is_insufficient() -> None:
    verdict = feasibility(
        _meta(min_notional=Decimal("5")),
        **_feasible_kwargs(
            equity=Decimal("20"), target_notional=Decimal("50"), leverage=Decimal("1")
        ),
    )
    assert verdict.status is EligibilityStatus.INSUFFICIENT_EQUITY
    assert verdict.reasons == (UniverseReason.EQUITY_BELOW_INITIAL_MARGIN,)
    assert verdict.spec is None


def test_requested_leverage_above_venue_max_is_insufficient_equity() -> None:
    verdict = feasibility(
        _meta(leverage_max=Decimal("20")), **_feasible_kwargs(leverage=Decimal("50"))
    )
    assert verdict.status is EligibilityStatus.INSUFFICIENT_EQUITY
    assert verdict.reasons == (UniverseReason.LEVERAGE_ABOVE_VENUE_MAX,)


def test_quantity_below_venue_minimum_is_size_below_min() -> None:
    verdict = feasibility(
        _meta(step_size=Decimal("1"), min_qty=Decimal("10"), tick_size=Decimal("0.01")),
        **_feasible_kwargs(entry_price=Decimal("2.00"), target_notional=Decimal("19")),
    )
    assert verdict.status is EligibilityStatus.SIZE_BELOW_MIN
    assert verdict.reasons == (UniverseReason.QUANTITY_BELOW_MIN_QTY,)
    assert verdict.quantized_quantity == Decimal("9")


def test_quantity_rounded_to_zero_is_size_below_min() -> None:
    verdict = feasibility(
        _meta(step_size=Decimal("1"), min_qty=Decimal("1"), min_notional=Decimal("5")),
        **_feasible_kwargs(entry_price=Decimal("100"), target_notional=Decimal("5")),
    )
    assert verdict.status is EligibilityStatus.SIZE_BELOW_MIN
    assert verdict.reasons == (UniverseReason.QUANTITY_ROUNDED_TO_ZERO,)


def test_quantized_notional_below_minimum_is_rejected() -> None:
    verdict = feasibility(
        _meta(tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_notional=Decimal("5")),
        **_feasible_kwargs(entry_price=Decimal("2.999"), target_notional=Decimal("5")),
    )
    assert verdict.status is EligibilityStatus.BELOW_MIN_NOTIONAL
    assert verdict.reasons == (UniverseReason.QUANTIZED_NOTIONAL_BELOW_MIN,)
    assert verdict.quantized_notional is not None
    assert verdict.quantized_notional < Decimal("5")


def test_adverse_fees_and_slippage_eroding_the_stop_are_rejected() -> None:
    verdict = feasibility(
        _meta(),
        **_feasible_kwargs(
            fee_bps=Decimal("20"),
            slippage_bps=Decimal("10"),
            stop_distance_bps=Decimal("40"),
        ),
    )
    assert verdict.status is EligibilityStatus.EXCESSIVE_FRICTION
    assert verdict.reasons == (UniverseReason.FRICTION_EXCEEDS_STOP_DISTANCE,)
    assert verdict.round_trip_friction_bps == Decimal("60")
    assert verdict.friction_to_stop_ratio == Decimal("1.5")
    assert verdict.spec is None


def test_moderate_friction_keeps_the_order_feasible() -> None:
    verdict = feasibility(_meta(), **_feasible_kwargs())
    assert verdict.status is EligibilityStatus.ELIGIBLE
    assert verdict.round_trip_friction_bps == Decimal("6")
    assert verdict.friction_to_stop_ratio < Decimal("1")
    assert verdict.liquidation_distance_bps == Decimal("950")


def test_stop_beyond_liquidation_is_rejected() -> None:
    verdict = feasibility(
        _meta(maintenance_margin_rate=Decimal("0.05")),
        **_feasible_kwargs(stop_distance_bps=Decimal("600")),
    )
    assert verdict.status is EligibilityStatus.INSUFFICIENT_EQUITY
    assert verdict.reasons == (UniverseReason.STOP_BEYOND_LIQUIDATION,)
    assert verdict.liquidation_distance_bps == Decimal("500")


def test_leverage_at_or_beyond_maintenance_margin_is_rejected() -> None:
    verdict = feasibility(
        _meta(leverage_max=Decimal("200"), maintenance_margin_rate=Decimal("0.02")),
        **_feasible_kwargs(leverage=Decimal("100")),
    )
    assert verdict.status is EligibilityStatus.INSUFFICIENT_EQUITY
    assert verdict.reasons == (UniverseReason.LEVERAGE_AT_OR_BEYOND_MMR,)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"stop_distance_bps": Decimal("0")}, ValueError),
        ({"equity": Decimal("-1")}, ValueError),
        ({"leverage": Decimal("0.5")}, ValueError),
        ({"fee_bps": 2.0}, TypeError),  # float/int is refused, not coerced
        ({"entry_price": Decimal("0.001")}, ValueError),  # below one tick
    ],
)
def test_invalid_feasibility_inputs_raise(kwargs: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        feasibility(_meta(), **_feasible_kwargs(**kwargs))


def test_feasibility_identity_is_deterministic() -> None:
    first = feasibility(_meta(), **_feasible_kwargs())
    second = feasibility(_meta(), **_feasible_kwargs())
    other = feasibility(_meta(), **_feasible_kwargs(target_notional=Decimal("9")))
    assert first.verdict_id == second.verdict_id
    assert first.verdict_id != other.verdict_id


# --------------------------------------------------------------------------------------------------
# Universe report: coverage, absent metadata, economic status
# --------------------------------------------------------------------------------------------------


def test_empty_universe_is_not_run() -> None:
    report = qualify_universe({}, AS_OF)
    assert report.status is UniverseReportStatus.NOT_RUN
    assert report.coverage.total == 0
    assert report.entries == ()
    assert report.economic_qualification == "NOT_RUN"
    assert report.economic_blocker == "M4-04:NO_ALTCOIN_TAPE_IN_REPO"


def test_absent_metadata_is_blocked_not_fabricated() -> None:
    report = qualify_universe({"ALTUSDT": None}, AS_OF, **_feasible_kwargs())
    assert report.status is UniverseReportStatus.BLOCKED
    entry = report.entry("ALTUSDT")
    assert entry is not None
    assert entry.coverage is CoverageState.BLOCKED
    assert entry.eligibility is None
    assert entry.feasibility is None
    assert entry.reasons == (UniverseReason.METADATA_ABSENT,)
    assert report.coverage.blocked == 1


def test_empty_history_is_blocked() -> None:
    report = qualify_universe({"ALTUSDT": ()}, AS_OF)
    entry = report.entry("ALTUSDT")
    assert entry is not None
    assert entry.coverage is CoverageState.BLOCKED
    assert entry.reasons == (UniverseReason.NO_METADATA_IN_HISTORY,)


def test_ambiguous_history_is_blocked_with_resolution_status() -> None:
    v1 = _meta(metadata_version="v1", tick_size=Decimal("0.01"))
    v2 = _meta(metadata_version="v2", tick_size=Decimal("0.05"))
    report = qualify_universe({"ALTUSDT": (v1, v2)}, AS_OF)
    entry = report.entry("ALTUSDT")
    assert entry is not None
    assert entry.coverage is CoverageState.BLOCKED
    assert entry.resolution is ResolutionStatus.CONTRACT_CHANGED
    assert entry.reasons == (UniverseReason.AMBIGUOUS_AS_OF_VERSION,)


def test_symbol_key_mismatch_is_blocked() -> None:
    report = qualify_universe({"WRONGUSDT": _meta(symbol="ALTUSDT")}, AS_OF)
    entry = report.entry("WRONGUSDT")
    assert entry is not None
    assert entry.coverage is CoverageState.BLOCKED
    assert entry.reasons == (UniverseReason.SYMBOL_MISMATCH,)


def test_mixed_universe_reports_partial_coverage() -> None:
    fresh = _meta(symbol="ALTUSDT")
    stale = _meta(
        symbol="OLDUSDT",
        quote_volume_as_of_ns=AS_OF - MAX_VOLUME_AGE_NS - 1,
    )
    report = qualify_universe(
        {"ALTUSDT": fresh, "OLDUSDT": stale, "UNKNOWNUSDT": None},
        AS_OF,
        **_feasible_kwargs(),
    )
    assert report.status is UniverseReportStatus.PARTIAL
    assert report.coverage.total == 3
    assert report.coverage.eligible == 1
    assert report.coverage.rejected == 1
    assert report.coverage.blocked == 1
    assert report.entry("OLDUSDT") is not None
    old = report.entry("OLDUSDT")
    assert old is not None and old.eligibility is not None
    assert old.eligibility.status is EligibilityStatus.STALE_VOLUME


def test_universe_is_sorted_and_identity_ignores_input_order() -> None:
    a = _meta(symbol="AAAUSDT")
    b = _meta(symbol="BBBUSDT")
    first = qualify_universe({"BBBUSDT": b, "AAAUSDT": a}, AS_OF, **_feasible_kwargs())
    second = qualify_universe({"AAAUSDT": a, "BBBUSDT": b}, AS_OF, **_feasible_kwargs())
    assert [entry.symbol for entry in first.entries] == ["AAAUSDT", "BBBUSDT"]
    assert first.report_id == second.report_id


def test_eligibility_only_report_does_not_claim_feasibility() -> None:
    report = qualify_universe({"ALTUSDT": _meta()}, AS_OF)
    entry = report.entry("ALTUSDT")
    assert entry is not None
    assert entry.coverage is CoverageState.NOT_RUN
    assert entry.eligibility is not None and entry.eligibility.eligible
    assert entry.feasibility is None
    assert entry.reasons == (UniverseReason.FEASIBILITY_NOT_RUN,)
    assert report.status is UniverseReportStatus.PARTIAL


def test_report_never_claims_economic_qualification() -> None:
    report = qualify_universe({"ALTUSDT": _meta()}, AS_OF, **_feasible_kwargs())
    assert report.economic_qualification == "NOT_RUN"
    with pytest.raises(ValidationError):
        type(report)(**{**report.model_dump(), "economic_qualification": "QUALIFIED"})


def test_feasibility_kwargs_are_validated_not_silently_ignored() -> None:
    with pytest.raises(ValidationError):
        qualify_universe({"ALTUSDT": _meta()}, AS_OF, equity=Decimal("20"))
    with pytest.raises(ValidationError):
        qualify_universe({"ALTUSDT": _meta()}, AS_OF, **_feasible_kwargs(), unexpected=Decimal("1"))
    with pytest.raises(TypeError):
        qualify_universe({"ALTUSDT": _meta()}, AS_OF, **{**_feasible_kwargs(), "fee_bps": 2.0})


def test_feasibility_verdict_rejects_spec_on_rejection() -> None:
    verdict = feasibility(
        _meta(min_notional=Decimal("5")), **_feasible_kwargs(target_notional=Decimal("3"))
    )
    assert verdict.spec is None
    with pytest.raises(ValidationError):
        FeasibilityVerdict(
            **{
                **verdict.model_dump(),
                "status": EligibilityStatus.ELIGIBLE,
                "spec": None,
                "verdict_id": "x",
            }
        )


# --------------------------------------------------------------------------------------------------
# Inertness
# --------------------------------------------------------------------------------------------------


def test_module_is_inert_no_execution_adapter() -> None:
    source = Path(universe_module.__file__).read_text()
    assert "nautilus_trader" not in source
    assert "submit_order" not in source
    assert "ExecutionAdapter" not in source
