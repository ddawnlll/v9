"""Context-aware 28-expert evidence contracts (M1-05 / #14).

Mechanics (synthetic, zero evaluative weight): 28/28 canonical coverage, alias
and duplicate collapse, warmup and missing-input abstention, dependency-group
skip, context binding/rejection, deterministic ids, evidence models that carry no
authority, and the baseline-off equality with the plain registry (kind *and*
reason, asserted exactly).

Real tape (slow): the same 28 ids and the same equality on
``research/tape/btcusdt-1h-2025Q1`` with an actual opportunity in the frame, plus
a context-binding change across bars.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.adapters.tape_paths import tape_path
from v8_next.belief import advance
from v8_next.belief.state import BeliefIntegrityError
from v8_next.domain.horizons import HorizonContext, Timeframe, assemble_context
from v8_next.domain.market import (
    Candle,
    CausalFrame,
    build_candle_dataframe,
    extend_causal_frame,
)
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    ObservationView,
    QualityState,
    build_view,
)
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import StanceKind, opportunity_at
from v8_next.experts import evidence as evidence_module
from v8_next.experts.cache import FeatureCache
from v8_next.experts.evidence import (
    EVIDENCE_VERSION,
    EvidenceBundle,
    EvidenceReason,
    ExpertEvidenceV1,
    produce_evidence,
)
from v8_next.experts.registry import CANONICAL_28_EXPERTS, observe_all_28

NS = 1_000_000_000
MIN = 60 * NS
H1 = 60 * MIN
IID = "BTCUSDT-PERP.BINANCE"
BASE = 1_600_000_000_000_000_000
BTC_TAPE = tape_path("btcusdt-1h-2025Q1", "tape.jsonl")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _candles(n: int, *, start_px: str = "100", drift: str = "1") -> tuple[Candle, ...]:
    """Contiguous 1h bars with a monotone drift (so a breakout opportunity exists)."""
    out: list[Candle] = []
    price = Decimal(start_px)
    step = Decimal(drift)
    for i in range(n):
        start = BASE + i * H1
        end = start + H1
        close = price + step * i
        out.append(
            Candle(
                instrument_id=IID,
                start_ns=start,
                end_ns=end,
                open=close,
                high=close + Decimal("0.05"),
                low=close - Decimal("0.05"),
                close=close,
                volume=Decimal("10") + Decimal(i % 5),
                received_ns=end,
                available_ns=end,
                source_hash=f"syn-{i}",
            )
        )
    return tuple(out)


def _observations(candles: Sequence[Candle]) -> list[Observation]:
    """BAR observations carrying exactly the same candles the frame holds."""
    out: list[Observation] = []
    for index, candle in enumerate(candles):
        available = candle.available_ns if candle.available_ns is not None else candle.end_ns
        out.append(
            Observation(
                instrument_id=candle.instrument_id,
                kind=ObservationKind.BAR,
                event_time=candle.start_ns,
                available_at=available,
                received_at=candle.received_ns,
                published_at=available,
                source="synthetic",
                sequence=index,
                quality=QualityState.OK,
                payload={
                    "start_ns": candle.start_ns,
                    "end_ns": candle.end_ns,
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                    "source_hash": candle.source_hash,
                },
            )
        )
    return out


def _frame(candles: tuple[Candle, ...]) -> CausalFrame:
    return CausalFrame.from_ordered_prefix(
        candles[-1].instrument_id,
        candles[-1].end_ns,
        candles,
        build_candle_dataframe(candles),
    )


def _view(candles: tuple[Candle, ...], *, cut_time_ns: int | None = None) -> ObservationView:
    cut = candles[-1].end_ns if cut_time_ns is None else cut_time_ns
    return build_view(_observations(candles), cut_time_ns=cut)


def _context(
    view: ObservationView,
    cut: int,
    *,
    timeframes: Sequence[Timeframe] = (Timeframe.H1,),
    instrument: str = IID,
) -> HorizonContext:
    return assemble_context(
        {timeframe: view for timeframe in timeframes},
        instrument_id=instrument,
        decision_time_ns=cut,
    )


def _readings(cut: int) -> tuple[PositioningReading, ...]:
    """Funding + open interest known before ``cut`` and valid across it."""
    return (
        PositioningReading(
            IID,
            "settled_funding_rate",
            Decimal("0.0001"),
            cut - H1,
            cut - H1,
            cut - H1,
            cut + H1,
            "funding-syn-1",
        ),
        PositioningReading(
            IID,
            "open_interest",
            Decimal("1000"),
            cut - H1,
            cut - H1,
            cut - H1,
            cut + H1,
            "oi-syn-1",
        ),
    )


def _snapshot(
    n: int = 140,
) -> tuple[CausalFrame, ObservationView, HorizonContext, tuple[PositioningReading, ...]]:
    candles = _candles(n)
    frame = _frame(candles)
    cut = frame.decision_ns
    view = _view(candles)
    return frame, view, _context(view, cut), _readings(cut)


def _produce(
    frame: CausalFrame,
    view: ObservationView,
    context: HorizonContext,
    **kwargs: Any,
) -> EvidenceBundle:
    return produce_evidence(
        view=view,
        context=context,
        frame=frame,
        cache=kwargs.pop("cache", None) or FeatureCache(),
        **kwargs,
    )


def _rows(bundle: EvidenceBundle) -> dict[str, ExpertEvidenceV1]:
    return {row.expert_id: row for row in bundle.evidence}


# --------------------------------------------------------------------------- #
# contract surface: 28/28, no authority
# --------------------------------------------------------------------------- #
def test_module_version_and_no_authority_surface() -> None:
    assert EVIDENCE_VERSION == "v9.expert-evidence.1"
    assert len(CANONICAL_28_EXPERTS) == 28
    assert len(set(CANONICAL_28_EXPERTS)) == 28

    # Structurally: the only dispatch surface is the registry's observer call.
    source = Path(evidence_module.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden_tokens = ("execution", "order", "broker", "adapter", "portfolio", "capital")
    assert not [name for name in imported if any(token in name for token in forbidden_tokens)]
    assert not any(name.startswith(("place_", "submit_", "execute_", "send_")) for name in dir(evidence_module))
    assert not hasattr(evidence_module, "opportunity_at")

    frame, view, context, readings = _snapshot()
    bundle = _produce(frame, view, context, readings=readings)
    assert bundle.advisory_only is True
    assert bundle.order_authority is False
    assert all(row.order_authority is False and row.advisory_only is True for row in bundle.evidence)
    fields = set(EvidenceBundle.model_fields) | set(ExpertEvidenceV1.model_fields)
    assert not fields & {
        "opportunity_id",
        "exposure_id",
        "order_id",
        "capital",
        "notional",
        "quantity",
        "position_size",
    }


def test_all_28_experts_emit_one_evidence_row_each() -> None:
    frame, view, context, readings = _snapshot()
    bundle = _produce(frame, view, context, readings=readings)

    assert [row.expert_id for row in bundle.evidence] == list(CANONICAL_28_EXPERTS)
    assert bundle.coverage == 28
    assert bundle.coverage == len({row.expert_id for row in bundle.evidence})
    assert all(row.warmup_ok for row in bundle.evidence)
    assert bundle.duplicate_witness_ids == ()
    assert bundle.abstain_count == sum(
        1 for row in bundle.evidence if row.stance is StanceKind.ABSTAIN
    )
    for row in bundle.evidence:
        expected = Decimal(0) if row.stance is StanceKind.ABSTAIN else Decimal(1)
        assert row.confidence == expected
        assert row.context_ref == bundle.context_ref
        assert row.expert_version == "v1"

    # Dependency groups are deduplicated: 28 experts share fewer requirement sets.
    groups = tuple(sorted({row.input_dependency_group for row in bundle.evidence}))
    assert bundle.input_dependency_groups == groups
    assert len(groups) < 28
    assert all(group.startswith("requires:") for group in groups)


# --------------------------------------------------------------------------- #
# duplicate observers / aliases
# --------------------------------------------------------------------------- #
def test_duplicate_observers_cannot_inflate_evidence() -> None:
    frame, view, context, readings = _snapshot()
    single = _produce(frame, view, context, readings=readings)

    duplicated = (
        *CANONICAL_28_EXPERTS,
        "bollinger-breakout",
        "bollinger_breakout",
        "bollinger-breakout",
        "macd-stoch-trend",
    )
    doubled = _produce(frame, view, context, readings=readings, observers=duplicated)

    assert doubled.coverage == 28
    assert len(doubled.evidence) == 28
    assert len({row.evidence_id for row in doubled.evidence}) == 28
    assert doubled.duplicate_witness_ids == ("bollinger_breakout", "macd_stoch_trend")
    assert doubled.abstain_count == single.abstain_count
    # A repeat cannot change the content or the count, only the recorded duplicates.
    assert doubled.provenance == single.provenance
    assert tuple(row.evidence_id for row in doubled.evidence) == tuple(
        row.evidence_id for row in single.evidence
    )

    alias_only = _produce(frame, view, context, readings=readings, observers=("bollinger-breakout",))
    assert alias_only.coverage == 1
    assert alias_only.evidence[0].expert_id == "bollinger_breakout"

    with pytest.raises(KeyError):
        _produce(frame, view, context, readings=readings, observers=("not_an_expert",))
    with pytest.raises(ValueError, match="no observers"):
        _produce(frame, view, context, readings=readings, observers=())


def test_bundle_and_row_reject_duplicate_or_unknown_material() -> None:
    frame, view, context, readings = _snapshot()
    bundle = _produce(frame, view, context, readings=readings)
    row = bundle.evidence[0]

    duplicated = ExpertEvidenceV1(**row.model_dump())
    assert duplicated.evidence_id == row.evidence_id

    unknown = row.model_dump()
    unknown["expert_id"] = "squeeze_swing"  # registered alias target, but not one of the 28
    with pytest.raises(ValidationError, match="28 canonical"):
        ExpertEvidenceV1(**unknown)

    contradictory = row.model_dump()
    contradictory["stance"] = StanceKind.ABSTAIN
    contradictory["confidence"] = Decimal(1)
    with pytest.raises(ValidationError, match="contradicts stance"):
        ExpertEvidenceV1(**contradictory)

    payload = bundle.model_dump()
    payload["evidence"] = (bundle.evidence[0].model_dump(), bundle.evidence[0].model_dump())
    payload["coverage"] = 2
    payload["abstain_count"] = 2
    with pytest.raises(ValidationError, match="duplicate expert row"):
        EvidenceBundle(**payload)


# --------------------------------------------------------------------------- #
# explicit shortfalls: warmup, missing input, dependency group, context
# --------------------------------------------------------------------------- #
def test_warmup_shortfall_abstains_with_typed_reason() -> None:
    candles = _candles(5)
    frame = _frame(candles)
    view = _view(candles)
    context = _context(view, frame.decision_ns)

    bundle = _produce(frame, view, context)
    rows = _rows(bundle)

    assert bundle.coverage == 28
    assert bundle.abstain_count == 28
    assert rows["ichimoku_cloud"].warmup_ok is False
    assert rows["ichimoku_cloud"].stance is StanceKind.ABSTAIN
    assert rows["ichimoku_cloud"].reason == EvidenceReason.WARMUP_INPUT.value
    assert rows["ichimoku_cloud"].confidence == Decimal(0)
    # A witness whose declared warmup is met is dispatched and abstains on its own reason.
    assert rows["candlestick_reversal"].warmup_ok is True
    assert rows["candlestick_reversal"].reason == "NO_OPPORTUNITY"


def test_missing_view_input_abstains_instead_of_fabricating() -> None:
    frame, _, _, readings = _snapshot()
    cut = frame.decision_ns
    empty = build_view([], cut_time_ns=cut)
    context = _context(empty, cut)

    bundle = _produce(frame, empty, context, readings=readings)
    assert bundle.coverage == 28
    assert bundle.abstain_count == 28
    assert {row.reason for row in bundle.evidence} == {EvidenceReason.INPUT_QUALITY.value}
    assert {row.stance for row in bundle.evidence} == {StanceKind.ABSTAIN}
    assert all(row.confidence == Decimal(0) for row in bundle.evidence)

    # The V9 layer is what abstains: the registry alone still evaluated the bar.
    baseline = _produce(frame, empty, context, readings=readings, context_layer=False)
    plain = observe_all_28(frame, None, readings=readings)
    assert [(row.expert_id, row.stance, row.reason) for row in baseline.evidence] == [
        (stance.observer_id, stance.kind, stance.reason) for stance in plain
    ]


def test_zero_candle_frame_abstains_for_every_witness() -> None:
    frame = CausalFrame(IID, BASE, ())
    empty = build_view([], cut_time_ns=BASE)
    context = _context(empty, BASE)
    bundle = _produce(frame, empty, context)
    assert bundle.coverage == 28
    assert {row.reason for row in bundle.evidence} == {EvidenceReason.WARMUP_INPUT.value}
    assert bundle.abstain_count == 28


def test_absent_positioning_readings_abstain_with_typed_reason() -> None:
    frame, view, context, _ = _snapshot()
    bundle = _produce(frame, view, context)  # readings omitted on purpose
    rows = _rows(bundle)
    assert rows["funding_crowding_reversal"].reason == EvidenceReason.MISSING_INPUT.value
    assert rows["open_interest_divergence"].reason == EvidenceReason.MISSING_INPUT.value
    assert rows["bollinger_breakout"].reason != EvidenceReason.MISSING_INPUT.value


def test_unavailable_dependency_group_abstains_without_crashing() -> None:
    frame, view, context, readings = _snapshot()
    bundle = _produce(
        frame,
        view,
        context,
        readings=readings,
        unavailable_dependency_groups=("dep_derivatives", "trend"),
    )
    rows = _rows(bundle)
    assert bundle.coverage == 28
    for expert_id in ("funding_crowding_reversal", "open_interest_divergence", "trend_pullback"):
        assert rows[expert_id].stance is StanceKind.ABSTAIN
        assert rows[expert_id].reason == EvidenceReason.DEPENDENCY_UNAVAILABLE.value
    assert rows["bollinger_breakout"].reason != EvidenceReason.DEPENDENCY_UNAVAILABLE.value


def test_absent_decision_horizon_abstains_with_typed_reason() -> None:
    frame, view, _, readings = _snapshot()
    cut = frame.decision_ns
    # A 1h frame judged on an M15-only context: its own horizon is absent.
    context = _context(view, cut, timeframes=(Timeframe.M15,))
    bundle = _produce(frame, view, context, readings=readings)
    assert {row.reason for row in bundle.evidence} == {EvidenceReason.CONTEXT_INCOMPLETE.value}
    assert bundle.abstain_count == 28


def test_context_and_belief_mismatches_fail_closed() -> None:
    frame, view, context, readings = _snapshot()
    cut = frame.decision_ns

    other_instrument = _context(view, cut, instrument="ETHUSDT-PERP.BINANCE")
    with pytest.raises(ValueError, match="context instrument"):
        _produce(frame, view, other_instrument, readings=readings)

    other_time = _context(view, cut + H1)
    with pytest.raises(ValueError, match="context decision time"):
        _produce(frame, view, other_time, readings=readings)

    future_view = _view(_candles(141), cut_time_ns=cut + 2 * H1)
    with pytest.raises(ValueError, match="future input"):
        _produce(frame, future_view, context, readings=readings)

    ts_belief = advance(None, view, context, now_ns=cut)
    mismatch = _context(view, cut + H1)
    other_belief = advance(None, view, mismatch, now_ns=cut + H1)
    with pytest.raises(ValueError, match="belief decision time"):
        _produce(frame, view, context, readings=readings, belief=other_belief)

    tampered = ts_belief.model_copy(update={"version_hash": "0" * 64})
    with pytest.raises(BeliefIntegrityError):
        _produce(frame, view, context, readings=readings, belief=tampered)


def test_belief_is_bound_into_context_and_bundle() -> None:
    frame, view, context, readings = _snapshot()
    cut = frame.decision_ns
    belief = advance(None, view, context, now_ns=cut)

    without = _produce(frame, view, context, readings=readings)
    with_belief = _produce(frame, view, context, readings=readings, belief=belief)

    assert without.belief_version_hash == ""
    assert with_belief.belief_version_hash == belief.version_hash
    assert with_belief.context_ref != without.context_ref
    assert with_belief.provenance != without.provenance
    assert tuple(row.evidence_id for row in without.evidence) != tuple(
        row.evidence_id for row in with_belief.evidence
    )
    assert all(row.context_ref == with_belief.context_ref for row in with_belief.evidence)


# --------------------------------------------------------------------------- #
# determinism and baseline parity
# --------------------------------------------------------------------------- #
def test_evidence_ids_are_deterministic_across_runs() -> None:
    frame, view, context, readings = _snapshot()
    first = _produce(frame, view, context, readings=readings)
    second = _produce(frame, view, context, readings=readings)

    assert first.context_ref == second.context_ref
    assert first.provenance == second.provenance
    assert tuple(row.evidence_id for row in first.evidence) == tuple(
        row.evidence_id for row in second.evidence
    )
    # A later decision time is a different context, hence different ids for every row.
    later_candles = _candles(150)
    later_frame = _frame(later_candles)
    later_view = _view(later_candles)
    later = _produce(
        later_frame,
        later_view,
        _context(later_view, later_frame.decision_ns),
        readings=_readings(later_frame.decision_ns),
    )
    assert later.context_ref != first.context_ref
    assert {row.evidence_id for row in later.evidence}.isdisjoint(
        {row.evidence_id for row in first.evidence}
    )


def test_shared_cache_retains_state_across_evidence_bars() -> None:
    candles = _candles(140)
    longer = _candles(141)
    numeric = build_candle_dataframe(longer)
    frame = CausalFrame.from_ordered_prefix(IID, candles[-1].end_ns, candles, numeric)
    cache = FeatureCache()

    view = _view(candles)
    first = _produce(frame, view, _context(view, frame.decision_ns), cache=cache)
    assert first.coverage == 28
    assert cache.retained_bars(IID) == 140

    extended = extend_causal_frame(frame, longer[-1].end_ns, longer[-1], numeric)
    assert extended is not None
    next_view = _view(longer)
    second = _produce(extended, next_view, _context(next_view, extended.decision_ns), cache=cache)
    stats = cache.stats()
    assert second.coverage == 28
    assert stats.extensions == 1
    assert stats.reseeds == 0
    assert stats.invalidations == 0
    assert second.context_ref != first.context_ref


def test_context_layer_off_equals_plain_registry_output() -> None:
    frame, view, context, readings = _snapshot()
    opportunity = opportunity_at(frame)
    assert opportunity is not None  # a monotone drift breaks the prior 48-bar high

    plain = observe_all_28(frame, opportunity, readings=readings)
    bundle = _produce(
        frame,
        view,
        context,
        readings=readings,
        opportunity=opportunity,
        context_layer=False,
    )

    assert bundle.context_layer_enabled is False
    assert [(row.expert_id, row.stance, row.reason) for row in bundle.evidence] == [
        (stance.observer_id, stance.kind, stance.reason) for stance in plain
    ]
    assert bundle.coverage == 28


def test_enabled_layer_is_transparent_when_every_input_is_warm() -> None:
    frame, view, context, readings = _snapshot()
    opportunity = opportunity_at(frame)
    assert opportunity is not None

    plain = observe_all_28(frame, opportunity, readings=readings)
    enabled = _produce(frame, view, context, readings=readings, opportunity=opportunity)

    assert enabled.context_layer_enabled is True
    # No gate fired, so every row is the registry's own verdict, unchanged.
    assert [(row.expert_id, row.stance, row.reason) for row in enabled.evidence] == [
        (stance.observer_id, stance.kind, stance.reason) for stance in plain
    ]
    assert enabled.abstain_count < 28


# --------------------------------------------------------------------------- #
# real tape (slow)
# --------------------------------------------------------------------------- #
def _breakout_index(candles: Sequence[Candle], *, start: int = 48) -> int | None:
    """First bar from ``start`` whose close breaks the prior 48-bar high or low."""
    for index in range(start, len(candles)):
        window = candles[index - 48 : index]
        close = candles[index].close
        if close > max(candle.high for candle in window):
            return index
        if close < min(candle.low for candle in window):
            return index
    return None


@pytest.mark.slow
def test_real_tape_28_expert_evidence_and_baseline_parity() -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    from v8_next.evaluation.gate_resolution import load_tape_candles

    candles = tuple(load_tape_candles(BTC_TAPE, limit=260))
    if len(candles) < 160:
        pytest.skip("tape slice too short")
    # Start at 100 so every declared warmup in the registry is satisfied.
    index = _breakout_index(candles, start=100)
    assert index is not None, "no 48-bar breakout after 100 tape bars"

    used = candles[: index + 1]
    frame = _frame(used)
    view = _view(used)
    context = _context(view, frame.decision_ns)
    opportunity = opportunity_at(frame)
    assert opportunity is not None

    cache = FeatureCache()
    bundle = _produce(frame, view, context, cache=cache, opportunity=opportunity)

    assert bundle.coverage == 28
    assert len({row.expert_id for row in bundle.evidence}) == 28
    assert {row.expert_id for row in bundle.evidence} == set(CANONICAL_28_EXPERTS)
    typed = {reason.value for reason in EvidenceReason}
    dispatched = [row for row in bundle.evidence if row.reason not in typed]
    # Only the two positioning witnesses are gated (no readings were supplied);
    # every other witness saw the opportunity itself, not a bare frame.
    assert len(dispatched) == 26
    assert "NO_OPPORTUNITY" not in {row.reason for row in bundle.evidence}
    assert all(row.context_ref == bundle.context_ref for row in bundle.evidence)

    # Same inputs, same ids.
    again = _produce(frame, view, context, opportunity=opportunity)
    assert tuple(row.evidence_id for row in again.evidence) == tuple(
        row.evidence_id for row in bundle.evidence
    )
    assert again.context_ref == bundle.context_ref

    # Baseline-off equality on the real tape: kind and reason, exactly.
    plain = observe_all_28(frame, opportunity)
    off = _produce(frame, view, context, opportunity=opportunity, context_layer=False)
    assert [(row.expert_id, row.stance, row.reason) for row in off.evidence] == [
        (stance.observer_id, stance.kind, stance.reason) for stance in plain
    ]

    # A different decision bar is a different context binding.
    earlier_used = candles[: max(index - 20, 100)]
    earlier_frame = _frame(earlier_used)
    earlier_view = _view(earlier_used)
    earlier = _produce(
        earlier_frame,
        earlier_view,
        _context(earlier_view, earlier_frame.decision_ns),
        opportunity=opportunity_at(earlier_frame),
    )
    assert earlier.coverage == 28
    assert earlier.context_ref != bundle.context_ref
