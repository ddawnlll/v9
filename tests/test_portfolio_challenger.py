"""M3-05: constraint-aware shadow portfolio challenger.

Every acceptance claim is exercised here: cap binding, sector cap, precision
rounding down, existing open/reserved exposure, risk priority ordering, duplicate
exposure denial, shuffle determinism, fail-closed constraint inputs, and a
baseline comparison that reports accounting deltas only. Nothing produced by the
challenger is ever submitted.
"""

from __future__ import annotations

import ast
import inspect
import random
from dataclasses import fields
from decimal import Decimal as D
from pathlib import Path

import pytest

from v8_next.economics import portfolio_challenger as pc
from v8_next.economics.allocation import AllocationProposal
from v8_next.economics.controller import InstrumentConstraints
from v8_next.economics.decisions import Opportunity, Stance, StanceKind, UtilityInputs
from v8_next.economics.portfolio_challenger import (
    AllocationCandidate,
    BaselineAllocation,
    ChallengeOutcome,
    ChallengeReason,
    allocate_challenger,
    compare_to_baseline,
)
from v8_next.economics.protection import CampaignProtection
from v8_next.risk.admission import RiskLimits, RiskSnapshot

BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT-PERP.BINANCE"
SOL = "SOLUSDT-PERP.BINANCE"
XRP = "XRPUSDT-PERP.BINANCE"
DOGE = "DOGEUSDT-PERP.BINANCE"
ADA = "ADAUSDT-PERP.BINANCE"

EQUITY = D("10000")
STEP = D("0.1")
_UNSET = object()


def candidate(
    opportunity_id: str,
    instrument: str = BTC,
    *,
    requested: str | D = "1000",
    priority: int = 0,
    sector: str = "CRYPTO",
    correlation: str | None = None,
    price: str | D = "100",
    confidence: str | D = "0.5",
    step: str | D = "0.1",
    min_size: str | D = "0.1",
    max_size: str | D = "1000",
    tick: str | D = "0.01",
    min_notional: str | D = "10",
    direction: str = "LONG",
) -> AllocationCandidate:
    def dec(value: str | D) -> D:
        return value if isinstance(value, D) else D(value)

    return AllocationCandidate(
        opportunity_id=opportunity_id,
        instrument=instrument,
        direction=direction,
        requested_notional=dec(requested),
        risk_priority=priority,
        sector=sector,
        correlation_group=correlation or f"corr-{opportunity_id}",
        reference_price=dec(price),
        confidence=dec(confidence),
        step_size=dec(step),
        min_size=dec(min_size),
        max_size=dec(max_size),
        tick_size=dec(tick),
        min_notional=dec(min_notional),
    )


def allocate(
    candidates: list[AllocationCandidate],
    *,
    equity: D | None = EQUITY,
    committed: D | None = D("0"),
    max_gross: D | None = EQUITY,
    max_sector: D | None = EQUITY,
    risk_budget: D | None = EQUITY,
    step_sizes: dict[str, D] | None | object = _UNSET,
    instrument_committed: dict[str, D] | None = None,
    sector_committed: dict[str, D] | None = None,
) -> pc.ChallengeResult:
    resolved = (
        {c.instrument: c.step_size for c in candidates}
        if step_sizes is _UNSET
        else step_sizes
    )
    return allocate_challenger(
        candidates,
        equity=equity,
        committed=committed,
        max_gross_exposure=max_gross,
        max_per_sector=max_sector,
        risk_budget=risk_budget,
        step_sizes=resolved if isinstance(resolved, dict) else None,
        instrument_committed=instrument_committed,
        sector_committed=sector_committed,
    )


def admitted(result: pc.ChallengeResult) -> dict[str, tuple[D, D]]:
    return {
        outcome.opportunity_id: (outcome.admitted_quantity, outcome.admitted_notional)
        for outcome in result.outcomes
        if outcome.is_admitted
    }


# ---------------------------------------------------------------- gross cap


def test_gross_cap_binds_with_exact_admitted_set() -> None:
    candidates = [
        candidate("a", BTC, requested="5000", sector="A"),
        candidate("b", ETH, requested="5000", sector="B"),
        candidate("c", SOL, requested="5000", sector="C"),
    ]
    result = allocate(candidates, max_gross=EQUITY, max_sector=D("20000"))
    assert admitted(result) == {"a": (D("50"), D("5000")), "b": (D("50"), D("5000"))}
    assert result.outcome_for("c") is not None
    assert result.outcome_for("c").outcome is ChallengeOutcome.BLOCKED_CAP
    assert result.outcome_for("c").reason is ChallengeReason.NO_GROSS_CAPACITY
    assert result.total_committed_notional == D("10000")
    assert result.admitted_notional == D("10000")
    assert result.remaining_capacity == D("0")
    assert [note.opportunity_id for note in result.counterfactual] == ["c"]
    assert result.counterfactual[0].hypothetical_notional == D("5000")


def test_equity_ceiling_binds_below_gross_cap() -> None:
    result = allocate(
        [candidate("a", requested="9000", sector="A")],
        equity=D("5000"),
        max_gross=D("10000"),
        risk_budget=D("10000"),
    )
    assert admitted(result) == {"a": (D("50"), D("5000"))}
    assert result.outcome_for("a").binding is ChallengeReason.NO_GROSS_CAPACITY
    assert result.remaining_capacity == D("0")


# ---------------------------------------------------------------- sector cap


def test_sector_cap_binds_and_partially_admits() -> None:
    candidates = [
        candidate("a", BTC, requested="3000", sector="CRYPTO", correlation="a"),
        candidate("b", ETH, requested="3000", sector="CRYPTO", correlation="b"),
        candidate("c", SOL, requested="1000", sector="CRYPTO", correlation="c"),
    ]
    result = allocate(candidates, max_sector=D("4000"))
    assert admitted(result) == {"a": (D("30"), D("3000")), "b": (D("10"), D("1000"))}
    assert result.outcome_for("a").binding is None
    assert result.outcome_for("b").binding is ChallengeReason.SECTOR_CAP_REACHED
    assert result.outcome_for("c").outcome is ChallengeOutcome.BLOCKED_CAP
    assert result.outcome_for("c").reason is ChallengeReason.SECTOR_CAP_REACHED
    assert result.total_committed_notional == D("4000")


def test_correlation_group_cap_binds_across_sectors() -> None:
    candidates = [
        candidate("a", BTC, requested="3000", sector="A", correlation="cluster"),
        candidate("b", ETH, requested="3000", sector="B", correlation="cluster"),
    ]
    result = allocate(candidates, max_sector=D("3000"))
    assert admitted(result) == {"a": (D("30"), D("3000"))}
    assert result.outcome_for("b").outcome is ChallengeOutcome.BLOCKED_CAP
    assert result.outcome_for("b").reason is ChallengeReason.CORRELATION_CAP_REACHED


def test_risk_budget_binds() -> None:
    candidates = [
        candidate("a", BTC, requested="3000", sector="A"),
        candidate("b", ETH, requested="3000", sector="B"),
    ]
    result = allocate(candidates, max_sector=D("10000"), risk_budget=D("3500"))
    assert admitted(result) == {"a": (D("30"), D("3000")), "b": (D("5"), D("500"))}
    assert result.outcome_for("b").binding is ChallengeReason.RISK_BUDGET_EXHAUSTED
    assert result.total_committed_notional == D("3500")
    exhausted = allocate(
        [*candidates, candidate("c", SOL, requested="1000", sector="C")],
        max_sector=D("10000"),
        risk_budget=D("3500"),
    )
    assert exhausted.outcome_for("c").outcome is ChallengeOutcome.BLOCKED_RISK
    assert exhausted.outcome_for("c").reason is ChallengeReason.RISK_BUDGET_EXHAUSTED


# ----------------------------------------------------------------- precision


def test_size_precision_rounds_request_down_to_step() -> None:
    result = allocate([candidate("a", requested="1005.55")])
    assert admitted(result) == {"a": (D("10.0"), D("1000"))}
    assert result.remaining_capacity == D("9000")


def test_request_rounded_down_never_exceeds_cap() -> None:
    result = allocate([candidate("a", requested="2000.05")], max_gross=D("2000"))
    assert admitted(result) == {"a": (D("20.0"), D("2000"))}
    assert result.total_committed_notional == D("2000")


def test_below_venue_minimum_is_blocked_precision() -> None:
    tiny = allocate([candidate("a", requested="0.5")])
    assert tiny.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_PRECISION
    assert tiny.outcome_for("a").reason is ChallengeReason.BELOW_VENUE_MINIMUM
    assert tiny.admitted_notional == D("0")

    below_min_notional = allocate([candidate("a", requested="15", min_notional=D("20"))])
    assert below_min_notional.outcome_for("a").reason is ChallengeReason.BELOW_VENUE_MINIMUM

    max_size = allocate([candidate("a", requested="1000", max_size=D("3"))])
    assert admitted(max_size) == {"a": (D("3"), D("300"))}


def test_reference_price_must_be_on_tick() -> None:
    result = allocate([candidate("a", price="100.005")])
    assert result.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_PRECISION
    assert result.outcome_for("a").reason is ChallengeReason.PRICE_NOT_ON_TICK


def test_declared_step_must_agree_with_authoritative_map() -> None:
    candidates = [candidate("a", step=D("0.1")), candidate("b", instrument=ETH, step=D("0.5"))]
    result = allocate(candidates, step_sizes={BTC: D("0.1"), ETH: D("0.01")})
    assert admitted(result) == {"a": (D("10.0"), D("1000"))}
    assert result.outcome_for("b").reason is ChallengeReason.STEP_SIZE_DISAGREEMENT


def test_unknown_instrument_step_fails_closed_for_that_candidate() -> None:
    result = allocate([candidate("a")], step_sizes={})
    assert result.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_PRECISION
    assert result.outcome_for("a").reason is ChallengeReason.UNKNOWN_STEP_SIZE


# -------------------------------------------------- existing / reserved exposure


def test_existing_exposure_reduces_capacity_and_blocks_same_instrument() -> None:
    candidates = [
        candidate("btc", instrument=BTC, requested="9000", sector="L1", priority=5),
        candidate("eth", instrument=ETH, requested="500", sector="L1", priority=9),
    ]
    result = allocate(
        candidates,
        committed=D("2000"),
        instrument_committed={ETH: D("2000")},
        sector_committed={"L1": D("2000")},
    )
    assert admitted(result) == {"btc": (D("80"), D("8000"))}
    assert result.outcome_for("eth").outcome is ChallengeOutcome.BLOCKED_EXPOSURE
    assert result.outcome_for("eth").reason is ChallengeReason.EXISTING_INSTRUMENT_EXPOSURE
    assert result.opening_committed_notional == D("2000")
    assert result.total_committed_notional == D("10000")
    assert result.remaining_capacity == D("0")


def test_unknown_exposure_detail_fails_closed() -> None:
    result = allocate([candidate("a")], committed=D("1000"))
    assert result.admitted_notional == D("0")
    assert result.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_EXPOSURE
    assert result.outcome_for("a").reason is ChallengeReason.UNKNOWN_EXPOSURE_DETAIL
    assert result.total_committed_notional == D("1000")


def test_inconsistent_exposure_attribution_fails_closed() -> None:
    result = allocate(
        [candidate("a")],
        committed=D("1000"),
        instrument_committed={BTC: D("1000")},
        sector_committed={"CRYPTO": D("500")},
    )
    assert result.admitted_notional == D("0")
    assert result.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_RISK
    assert result.outcome_for("a").reason is ChallengeReason.INCONSISTENT_EXPOSURE_INPUT

    zero_committed_with_exposure = allocate(
        [candidate("a")],
        instrument_committed={BTC: D("5")},
        sector_committed={"CRYPTO": D("5")},
    )
    assert (
        zero_committed_with_exposure.outcome_for("a").reason
        is ChallengeReason.INCONSISTENT_EXPOSURE_INPUT
    )

    negative = allocate(
        [candidate("a")],
        committed=D("1000"),
        instrument_committed={BTC: D("1200")},
        sector_committed={"CRYPTO": D("-200")},
    )
    assert negative.outcome_for("a").reason is ChallengeReason.INCONSISTENT_EXPOSURE_INPUT


# ------------------------------------------------------------- priority / dupes


def test_risk_priority_beats_input_order() -> None:
    low = candidate("low", BTC, requested="3000", sector="A", priority=1)
    high = candidate("high", ETH, requested="3000", sector="B", priority=9)
    result = allocate([low, high], max_gross=D("5000"))
    assert admitted(result) == {"high": (D("30"), D("3000")), "low": (D("20"), D("2000"))}
    assert result.outcome_for("low").binding is ChallengeReason.NO_GROSS_CAPACITY
    reversed_result = allocate([high, low], max_gross=D("5000"))
    assert admitted(reversed_result) == admitted(result)


def test_confidence_breaks_priority_ties() -> None:
    cautious = candidate("cautious", BTC, requested="3000", sector="A", confidence=D("0.2"))
    bold = candidate("bold", ETH, requested="3000", sector="B", confidence=D("0.9"))
    result = allocate([cautious, bold], max_gross=D("4000"))
    assert admitted(result)["bold"] == (D("30"), D("3000"))
    assert result.outcome_for("cautious").admitted_notional == D("1000")


def test_two_candidates_same_instrument_cannot_both_be_admitted() -> None:
    candidates = [
        candidate("a", instrument=BTC, requested="3000", sector="A"),
        candidate("b", instrument=BTC, requested="3000", sector="B"),
    ]
    result = allocate(candidates)
    assert admitted(result) == {"a": (D("30"), D("3000"))}
    assert result.outcome_for("b").outcome is ChallengeOutcome.BLOCKED_DUPLICATE_EXPOSURE
    assert result.outcome_for("b").reason is ChallengeReason.INSTRUMENT_ALREADY_ADMITTED
    assert result.total_committed_notional <= EQUITY


def test_duplicate_opportunity_id_cannot_be_admitted_twice() -> None:
    candidates = [
        candidate("dup", requested="3000", sector="A"),
        candidate("dup", requested="3000", sector="A"),
    ]
    result = allocate(candidates)
    admitted_rows = [o for o in result.outcomes if o.is_admitted]
    assert len(admitted_rows) == 1
    assert result.admitted_notional == D("3000")
    assert any(o.reason is ChallengeReason.DUPLICATE_OPPORTUNITY for o in result.outcomes)


# ------------------------------------------------------------- multi-asset fixture


def test_multi_asset_fixture_exact_admitted_set() -> None:
    candidates = [
        candidate("btc", BTC, requested="2000.05", priority=9, sector="LAYER1"),
        candidate("eth", ETH, requested="500", priority=8, sector="LAYER1"),
        candidate("sol", SOL, requested="1500", priority=7, sector="LAYER1"),
        candidate("xrp", XRP, requested="2000", priority=5, sector="PAYMENTS"),
        candidate("doge", DOGE, requested="1500", priority=3, sector="MEME"),
        candidate("ada", ADA, requested="0.5", priority=1, sector="MEME"),
    ]
    result = allocate(
        candidates,
        equity=D("6000"),
        committed=D("1000"),
        max_gross=D("6000"),
        max_sector=D("3000"),
        risk_budget=D("5000"),
        instrument_committed={ETH: D("1000")},
        sector_committed={"LAYER1": D("1000")},
    )
    assert admitted(result) == {
        "btc": (D("20.0"), D("2000")),
        "xrp": (D("20"), D("2000")),
    }
    assert result.outcome_for("btc").binding is ChallengeReason.SECTOR_CAP_REACHED
    assert result.outcome_for("eth").reason is ChallengeReason.EXISTING_INSTRUMENT_EXPOSURE
    assert result.outcome_for("sol").reason is ChallengeReason.SECTOR_CAP_REACHED
    assert result.outcome_for("doge").reason is ChallengeReason.RISK_BUDGET_EXHAUSTED
    assert result.outcome_for("ada").reason is ChallengeReason.BELOW_VENUE_MINIMUM
    assert result.opening_committed_notional == D("1000")
    assert result.admitted_notional == D("4000")
    assert result.total_committed_notional == D("5000")
    assert result.remaining_capacity == D("1000")
    # Hard caps hold exactly for every bucket.
    sector_totals: dict[str, D] = {}
    for outcome in result.outcomes:
        if outcome.is_admitted:
            found = next(c for c in candidates if c.opportunity_id == outcome.opportunity_id)
            sector_totals[found.sector] = (
                sector_totals.get(found.sector, D("0")) + outcome.admitted_notional
            )
    assert all(total <= D("3000") for total in sector_totals.values())
    assert result.total_committed_notional <= D("6000")
    assert result.total_committed_notional <= D("5000")


# -------------------------------------------------------------------- determinism


def test_shuffled_input_order_yields_identical_result() -> None:
    candidates = [
        candidate("a", BTC, requested="3000", priority=5, sector="A", correlation="a"),
        candidate("b", ETH, requested="2500", priority=9, sector="B", correlation="b"),
        candidate("c", SOL, requested="1800", priority=7, sector="A", correlation="c"),
        candidate("d", XRP, requested="1200", priority=3, sector="C", correlation="d"),
        candidate("e", DOGE, requested="900", priority=5, sector="B", correlation="e"),
        candidate("f", ADA, requested="4000", priority=1, sector="C", correlation="f"),
    ]
    kwargs = {"max_gross": D("7000"), "max_sector": D("4000"), "risk_budget": D("6000")}
    expected = allocate(candidates, **kwargs)
    expected_set = admitted(expected)

    rng = random.Random(7)
    for _ in range(10):
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        result = allocate(shuffled, **kwargs)
        assert admitted(result) == expected_set
        assert result.digest == expected.digest
        assert result.total_committed_notional == expected.total_committed_notional
        assert [o.opportunity_id for o in result.outcomes] == [
            o.opportunity_id for o in expected.outcomes
        ]
    reversed_result = allocate(list(reversed(candidates)), **kwargs)
    assert admitted(reversed_result) == expected_set
    assert reversed_result.digest == expected.digest


# ------------------------------------------------------------------- fail closed


def test_missing_constraints_fail_closed() -> None:
    for kwargs, name in (
        ({"equity": None}, "equity"),
        ({"committed": None}, "committed"),
        ({"max_gross": None}, "max_gross_exposure"),
        ({"max_sector": None}, "max_per_sector"),
        ({"risk_budget": None}, "risk_budget"),
        ({"step_sizes": None}, "step_sizes"),
    ):
        result = allocate([candidate("a")], **kwargs)
        assert result.fail_closed
        assert result.blocked_inputs == (name,)
        assert result.admitted_notional == D("0")
        assert result.remaining_capacity == D("0")
        outcome = result.outcome_for("a")
        assert outcome is not None
        assert outcome.outcome is ChallengeOutcome.BLOCKED_RISK
        assert outcome.reason is ChallengeReason.UNKNOWN_CONSTRAINTS
        assert outcome.admitted_quantity == D("0")
        assert result.counterfactual == ()


def test_malformed_constraints_fail_closed() -> None:
    for kwargs in (
        {"equity": D("-1")},
        {"equity": D("NaN")},
        {"max_gross": D("Infinity")},
        {"risk_budget": D("-0.01")},
        {"step_sizes": {BTC: D("0")}},
        {"step_sizes": {BTC: D("NaN")}},
    ):
        result = allocate([candidate("a")], **kwargs)
        assert result.fail_closed
        assert result.admitted_notional == D("0")
        assert result.total_committed_notional == D("0")


def test_unknown_sector_blocks_only_that_candidate() -> None:
    candidates = [
        candidate("known", BTC, requested="1000", sector="A", priority=9),
        candidate("unknown", ETH, requested="1000", sector="", priority=8),
    ]
    result = allocate(candidates)
    assert admitted(result) == {"known": (D("10"), D("1000"))}
    assert result.outcome_for("unknown").outcome is ChallengeOutcome.BLOCKED_RISK
    assert result.outcome_for("unknown").reason is ChallengeReason.UNKNOWN_SECTOR
    assert not result.fail_closed


def test_zero_caps_are_known_and_block_everything() -> None:
    result = allocate(
        [candidate("a", requested="1000")],
        max_gross=D("0"),
        max_sector=D("0"),
        risk_budget=D("0"),
    )
    assert not result.fail_closed
    assert result.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_CAP
    assert result.outcome_for("a").reason is ChallengeReason.NO_GROSS_CAPACITY
    assert result.admitted_notional == D("0")


def test_committed_above_gross_cap_blocks_new_allocation() -> None:
    result = allocate(
        [candidate("a", requested="1000")],
        committed=D("5000"),
        max_gross=D("4000"),
        instrument_committed={ETH: D("5000")},
        sector_committed={"CRYPTO": D("5000")},
    )
    assert result.outcome_for("a").outcome is ChallengeOutcome.BLOCKED_CAP
    assert result.total_committed_notional == D("5000")
    assert result.remaining_capacity == D("0")


# ------------------------------------------------------------------ comparison


def proposal(
    identity: str,
    instrument: str,
    exposure: str = "crypto",
    requested: D = D("600"),
) -> AllocationProposal:
    return AllocationProposal(
        Opportunity(identity, exposure, instrument, "LONG", 1, 100),
        (Stance("observer", "group", StanceKind.SUPPORT, "setup", identity, 10),),
        UtilityInputs(D(10), D(1), D(1), D(1), D(1), D(1), "test-only"),
        True,
        InstrumentConstraints(D("0.1"), D("0.1"), D(100), D("10")),
        D("100"),
        requested,
        CampaignProtection(
            "pandf:a:v2", identity, instrument, "LONG", 10, 50, D("90"), D("110")
        ),
    )


def baseline_allocation() -> BaselineAllocation:
    snapshot = RiskSnapshot(D("1000"), D("0"), D("0"), D("0"), 10, True)
    return BaselineAllocation(
        proposals=(proposal("a", BTC), proposal("b", ETH)),
        snapshots={"crypto": snapshot},
        limits=RiskLimits(D("1"), D("1"), D("1000"), 10),
        decision_ns=10,
    )


def comparison_candidates() -> list[AllocationCandidate]:
    return [
        candidate("a", BTC, requested="600", sector="A"),
        candidate("b", ETH, requested="600", sector="B"),
    ]


def test_baseline_comparison_reports_deltas_without_claiming_superiority() -> None:
    comparison = compare_to_baseline(
        comparison_candidates(),
        baseline_allocation(),
        equity=D("1000"),
        committed=D("0"),
        max_gross_exposure=D("1000"),
        max_per_sector=D("1000"),
        risk_budget=D("1000"),
        step_sizes={BTC: D("0.1"), ETH: D("0.1")},
    )
    assert [row.opportunity_id for row in comparison.rows] == ["a", "b"]
    rows = {row.opportunity_id: row for row in comparison.rows}
    # V8 books the stop/target band (110) for each campaign, the challenger the
    # entry notional (100): an accounting difference, not a preference.
    assert rows["a"].baseline_quantity == D("5.4")
    assert rows["a"].baseline_notional == D("540")
    assert rows["a"].challenger_notional == D("600")
    assert rows["a"].delta_notional == D("60")
    assert rows["b"].baseline_quantity == D("3.6")
    assert rows["b"].baseline_notional == D("360")
    assert rows["b"].challenger_notional == D("400")
    assert rows["b"].delta_notional == D("40")
    assert comparison.baseline_total_notional == D("900")
    assert comparison.challenger_total_notional == D("1000")
    assert comparison.delta_notional == D("100")
    assert "no economic superiority" in comparison.note
    assert not hasattr(comparison.rows[0], "campaign")
    assert not hasattr(comparison.rows[0], "order")


def test_baseline_comparison_is_repeatable() -> None:
    first = compare_to_baseline(
        comparison_candidates(),
        baseline_allocation(),
        equity=D("1000"),
        committed=D("0"),
        max_gross_exposure=D("1000"),
        max_per_sector=D("1000"),
        risk_budget=D("1000"),
        step_sizes={BTC: D("0.1"), ETH: D("0.1")},
    )
    second = compare_to_baseline(
        list(reversed(comparison_candidates())),
        baseline_allocation(),
        equity=D("1000"),
        committed=D("0"),
        max_gross_exposure=D("1000"),
        max_per_sector=D("1000"),
        risk_budget=D("1000"),
        step_sizes={BTC: D("0.1"), ETH: D("0.1")},
    )
    assert first.rows == second.rows
    assert first.delta_notional == second.delta_notional


def test_baseline_comparison_requires_identical_opportunities() -> None:
    kwargs = {
        "equity": D("1000"),
        "committed": D("0"),
        "max_gross_exposure": D("1000"),
        "max_per_sector": D("1000"),
        "risk_budget": D("1000"),
        "step_sizes": {BTC: D("0.1"), ETH: D("0.1")},
    }
    with pytest.raises(ValueError, match="identical opportunity sets"):
        compare_to_baseline(
            [candidate("a", BTC, requested="600")], baseline_allocation(), **kwargs
        )
    with pytest.raises(ValueError, match="instrument disagrees"):
        compare_to_baseline(
            [
                candidate("a", ETH, requested="600", sector="A"),
                candidate("b", ETH, requested="600", sector="B"),
            ],
            baseline_allocation(),
            **kwargs,
        )
    with pytest.raises(ValueError, match="share equity"):
        compare_to_baseline(
            comparison_candidates(),
            baseline_allocation(),
            **{**kwargs, "equity": D("2000")},
        )


# -------------------------------------------------------- no native submission


def test_module_carries_no_native_submission_surface() -> None:
    source = inspect.getsource(pc)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden_prefixes = ("v8_next.adapters", "nautilus_trader", "v8_next.execution")
    assert not [name for name in imported if name.startswith(forbidden_prefixes)]
    assert not [name for name in imported if name.startswith("v8_next.app")]
    lowered = source.lower()
    for token in ("submit_order", "orderfactory", "executionclient", "place_order"):
        assert token not in lowered.replace(" ", "")

    for dataclass_type in (pc.ChallengeResult, pc.AllocationOutcome, pc.ComparisonRow):
        names = {field.name for field in fields(dataclass_type)}
        assert not [name for name in names if "order" in name or "campaign" in name]


def test_results_never_carry_campaign_or_order_objects() -> None:
    result = allocate([candidate("a", requested="1000")])
    for outcome in result.outcomes:
        assert not hasattr(outcome, "campaign")
        assert not hasattr(outcome, "order")
        assert isinstance(outcome.admitted_quantity, D)
        assert isinstance(outcome.admitted_notional, D)


# ------------------------------------------------- counterfactual separation


def test_counterfactual_notes_stay_out_of_committed_money() -> None:
    result = allocate(
        [
            candidate("a", BTC, requested="3000", sector="A", priority=9),
            candidate("b", ETH, requested="3000", sector="B", priority=8),
        ],
        max_gross=D("3000"),
    )
    assert result.admitted_notional == D("3000")
    assert result.total_committed_notional == D("3000")
    assert result.counterfactual_hypothetical_notional == D("3000")
    note = result.counterfactual[0]
    assert note.opportunity_id == "b"
    assert note.hypothetical_notional == D("3000")
    assert note.label == "COUNTERFACTUAL_NOT_COMMITTED"
    assert result.total_committed_notional == result.opening_committed_notional + (
        result.admitted_notional
    )
    outcome_names = {field.name for field in fields(pc.ChallengeResult)}
    assert not [name for name in outcome_names if "pnl" in name or "realized" in name]


def test_module_location_is_the_owned_file() -> None:
    path = Path(inspect.getsourcefile(pc) or "")
    assert path.name == "portfolio_challenger.py"
