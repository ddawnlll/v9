"""Parity harness: canonicalisation, determinism and divergence localisation.

The parity gate is what licenses every optimisation in this goal, so the gate
itself is tested: it must be stable across identical replays, blind to the
wall-clock audit stamp, sensitive to a single flipped stance, and able to point
at the bar range that moved.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
from v8_next.domain.market import Candle
from v8_next.economics.decisions import Stance, StanceKind
from v8_next.opportunities.models import OpportunityStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "performance"))
from parity_canon import canonical, count_volatile, digest, first_difference  # noqa: E402
from parity_capture import build_sections  # noqa: E402

HOUR = 3_600_000_000_000


def _candles(count: int = 50) -> tuple[Candle, ...]:
    def candle(i: int) -> Candle:
        if i == 48:
            values = ("101", "122", "100", "120", "500")
        elif i == 49:
            values = ("120", "125", "119", "123", "200")
        else:
            values = ("100", "100.5", "99.5", "100", "100")
        return Candle(
            "BTCUSDT-PERP.BINANCE", i * HOUR, (i + 1) * HOUR,
            *(Decimal(value) for value in values),
            (i + 1) * HOUR, (i + 1) * HOUR, "test",
        )

    return tuple(candle(i) for i in range(count))


def _replay() -> dict:
    candles = _candles()
    cfg = ExpertStrategyConfig(
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.05"),
    )
    return run_expert_strategy_backtest(candles, cfg)


def test_two_identical_replays_produce_identical_sections() -> None:
    first, _ = build_sections(_replay(), [])
    second, _ = build_sections(_replay(), [])
    assert first.keys() == second.keys()
    for name in first:
        assert first[name]["sha256"] == second[name]["sha256"], name
    assert first["decisions"]["count"] == 50


def test_run_identity_fields_are_not_compared() -> None:
    early = {"event_type": "MARKET_OBSERVED", "recorded_time_ns": 1, "init_id": "uuid-a",
             "event_id": "evt-a", "fields": {"close": "100"}}
    late = {"event_type": "MARKET_OBSERVED", "recorded_time_ns": 999, "init_id": "uuid-b",
            "event_id": "evt-b", "fields": {"close": "100"}}
    assert digest(early) == digest(late)
    dumped = json.dumps(canonical(early))
    for key in ("recorded_time_ns", "init_id", "event_id"):
        assert key not in dumped
    assert count_volatile({"a": [early]}) == {"recorded_time_ns": 1, "init_id": 1, "event_id": 1}
    # A real content change still diverges.
    assert digest({**early, "fields": {"close": "101"}}) != digest(early)


def test_one_flipped_stance_changes_the_digest() -> None:
    baseline = _replay()
    tampered = json.loads(json.dumps(baseline, default=str))
    tampered["decisions"][48]["action"] = "FLIP" if baseline["decisions"][48]["action"] != "FLIP" else "HOLD"
    left, _ = build_sections(baseline, [])
    right, _ = build_sections(tampered, [])
    assert left["decisions"]["sha256"] != right["decisions"]["sha256"]

    left_chunks, right_chunks = left["decisions"], right["decisions"]
    assert left_chunks["chunked"] and right_chunks["chunked"]


def test_divergence_localises_to_a_chunk_and_record() -> None:
    record = {"action": "LONG", "stances": [{"observer_id": "a", "kind": "SUPPORT"}]}
    left = [record] * 200
    right = [dict(record)] * 200
    right[130] = {"action": "SHORT", "stances": [{"observer_id": "a", "kind": "SUPPORT"}]}
    left_sections, left_chunks = build_sections({"decisions": left}, [])
    right_sections, right_chunks = build_sections({"decisions": right}, [])
    assert left_sections["decisions"]["sha256"] != right_sections["decisions"]["sha256"]
    first_chunk = next(
        index
        for index, (a, b) in enumerate(zip(left_chunks["decisions"], right_chunks["decisions"], strict=True))
        if a != b
    )
    assert first_chunk == 130 // 64
    assert first_difference(left[130], right[130]) is not None


def test_decimal_and_enum_normalise_stably() -> None:
    stance = Stance(
        observer_id="x", dependency_group="g", kind=StanceKind.ABSTAIN,
        reason="no-edge", opportunity_id=None, decision_ns=1,
    )
    dumped = canonical({"price": Decimal("1.2300"), "stance": stance,
                        "status": OpportunityStatus.CANDIDATE})
    assert dumped["price"] == "1.2300"
    assert dumped["stance"]["kind"] == "ABSTAIN"
    assert dumped["status"] == OpportunityStatus.CANDIDATE.value
    assert digest({"price": Decimal("1.23")}) == digest({"price": "1.23"})
