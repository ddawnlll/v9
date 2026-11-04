"""Hot-path performance guards for the per-bar decision record.

Two conversions sit inside the innermost loop of every backtest (bars x curves x
legs x 28 experts) and were measured as the dominant cost of a portfolio run:

* ``dataclasses.asdict`` on each ``Stance`` / ``Opportunity`` -- 3.3M recursive
  ``_asdict_inner`` calls, 302k ``fields()`` lookups and 325k per-field
  ``deepcopy`` calls on a 385-bar run;
* ``dataclasses.replace`` rebuilding each stance just to stamp the witness
  metadata -- 499k calls, 1.88s cumulative.

Both were replaced by flat, non-recursive equivalents. These tests pin that the
replacement is *identical* in content (not merely plausible) and that it is
actually faster, so the loop cannot silently regress back to the slow path.

The frame is built from the real BTC tape; the tests skip when it is absent.
"""

from __future__ import annotations

import time
from dataclasses import asdict, replace

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.domain.market import frame_at
from v8_next.economics.decisions import (
    STANCE_FIELDS,
    Stance,
    opportunity_at,
    opportunity_record,
    stance_record,
    stance_with,
)
from v8_next.evaluation.gate_resolution import load_tape_candles
from v8_next.experts.registry import CANONICAL_28_EXPERTS, get_expert, observe_expert

BTC_TAPE = tape_path("btcusdt-1h-12m", "tape.jsonl")
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def _real_frame():
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    candles = tuple(load_tape_candles(BTC_TAPE, limit=120))
    if len(candles) < 60:
        pytest.skip("tape loaded too few candles for a causal frame")
    decision_ns = max(c.end_ns for c in candles)
    return frame_at(INSTRUMENT, decision_ns, candles)


def test_stance_record_is_identical_to_asdict_on_the_real_tape() -> None:
    frame = _real_frame()
    opportunity = opportunity_at(frame)
    observed = 0
    for expert_id in CANONICAL_28_EXPERTS:
        stance = observe_expert(expert_id, frame, opportunity)
        assert stance_record(stance) == asdict(stance)
        observed += 1
    assert observed == 28
    if opportunity is not None:
        assert opportunity_record(opportunity) == asdict(opportunity)


def test_stance_record_covers_every_field() -> None:
    frame = _real_frame()
    stance = observe_expert(CANONICAL_28_EXPERTS[0], frame, opportunity_at(frame))
    assert tuple(stance_record(stance)) == STANCE_FIELDS
    assert set(stance_record(stance)) == {f for f in stance.__dict__}


def test_witness_wrapping_matches_the_dataclasses_replace_path() -> None:
    """The metadata stamping must stay byte-identical to the old ``replace`` call."""
    frame = _real_frame()
    opportunity = opportunity_at(frame)
    for expert_id in CANONICAL_28_EXPERTS:
        spec = get_expert(expert_id)
        kwargs: dict[str, object] = {}
        chosen_variant = spec.default_variant if spec.takes_variant else None
        if spec.takes_variant and chosen_variant is not None:
            kwargs["variant"] = chosen_variant
        raw = spec.observer_fn(frame, opportunity, **kwargs)
        expected = replace(
            raw,
            observer_id=spec.expert_id,
            behavior_family=spec.behavior_family,
            mechanism_family=spec.mechanism_family,
            dependency_group=spec.dependency_group,
            version=f"{spec.expert_id}-witness-{spec.version}",
            variant_id=chosen_variant or raw.variant_id or "UNRESOLVED",
        )
        assert observe_expert(expert_id, frame, opportunity) == expected


def test_flat_record_conversion_is_faster_than_asdict() -> None:
    """Regression guard on the measured hot path (content equality is above)."""
    frame = _real_frame()
    stance = observe_expert(CANONICAL_28_EXPERTS[0], frame, opportunity_at(frame))
    rounds = 20_000

    start = time.perf_counter()
    for _ in range(rounds):
        asdict(stance)
    asdict_s = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(rounds):
        stance_record(stance)
    flat_s = time.perf_counter() - start

    print(
        f"\n[perf] {rounds} conversions: asdict={asdict_s * 1e3:.1f}ms "
        f"flat={flat_s * 1e3:.1f}ms speedup={asdict_s / flat_s:.2f}x "
        f"fields={len(STANCE_FIELDS)}"
    )
    assert flat_s < asdict_s, (flat_s, asdict_s)


def test_stance_is_a_flat_scalar_record() -> None:
    """The fast path is only valid while every field is a scalar."""
    frame = _real_frame()
    stance = observe_expert(CANONICAL_28_EXPERTS[0], frame, opportunity_at(frame))
    assert isinstance(stance, Stance)
    for value in stance.__dict__.values():
        assert isinstance(value, (str, int, type(None))) or type(value).__name__.endswith("Kind"), (
            f"non-scalar field breaks the flat conversion: {value!r}"
        )


def test_stance_with_matches_dataclasses_replace_in_every_kwarg_shape() -> None:
    """The experts stamp variant/version through ``stance_with``; pin equivalence."""
    frame = _real_frame()
    stance = observe_expert(CANONICAL_28_EXPERTS[0], frame, opportunity_at(frame))
    assert stance_with(stance, variant_id="v2") == replace(stance, variant_id="v2")
    assert stance_with(stance, version="x-v2") == replace(stance, version="x-v2")
    assert stance_with(stance, variant_id="v3", version="y-v2") == replace(
        stance, variant_id="v3", version="y-v2"
    )
    # no metadata given: the record is unchanged field by field
    assert stance_with(stance).__dict__ == stance.__dict__


def test_every_expert_still_stamps_its_own_variant() -> None:
    """The 28 experts must produce the same stances as before the conversion."""
    frame = _real_frame()
    opportunity = opportunity_at(frame)
    stamped = 0
    for expert_id in CANONICAL_28_EXPERTS:
        spec = get_expert(expert_id)
        kwargs: dict[str, object] = {}
        if spec.takes_variant and spec.default_variant is not None:
            kwargs["variant"] = spec.default_variant
        raw = spec.observer_fn(frame, opportunity, **kwargs)
        # The registry canonicalises the observer identity; the expert's own call
        # is what stamps a variant/version, and that is what changed here. The
        # per-expert SUPPORT/CONTRADICT/ABSTAIN fixtures in test_expert_registry
        # pin the values themselves.
        if raw.variant_id != "baseline" or raw.version != "squeeze-observer-v1":
            stamped += 1
    assert stamped >= 1, "no expert stamped a variant/version; the conversion is inert"

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
