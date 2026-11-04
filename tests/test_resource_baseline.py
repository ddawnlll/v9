"""Resource-baseline receipts and the parity gate (M0-08 / #9).

Mechanics: fail-closed receipt fields (no number without ``MEASURED``, no empty
field without a named refusal), timing/RSS measurement, gate precedence.
Real tape (slow): a genuine measurement of both replay modes, their decision
digest equality, and the gate's honest ``BLOCKED`` verdict for streams this
slice does not run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.resource_baseline import (
    MIN_REPETITIONS,
    REQUIRED_PARITY_STREAMS,
    GateStatus,
    MeasurementStatus,
    MemorySummary,
    ParityStreamDigests,
    ResourceReceipt,
    ScopeCoverage,
    TapeIdentity,
    TimingSummary,
    build_feature_receipt,
    build_receipt,
    compare_receipts,
    funding_digest,
    git_sha,
    load_receipt,
    measure,
    nearest_rank_p95,
    parity_gate,
    peak_rss,
    replay_feature_values,
    values_parity,
)

BTC_TAPE = tape_path("btcusdt-1h-2025Q1", "tape.jsonl")
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _full_digests(**overrides: str) -> ParityStreamDigests:
    values = dict.fromkeys(REQUIRED_PARITY_STREAMS, DIGEST_A)
    values.update(overrides)
    return ParityStreamDigests(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# receipt primitives fail closed
# --------------------------------------------------------------------------- #
def test_timing_summary_refuses_numbers_without_measured_status() -> None:
    with pytest.raises(ValueError):
        TimingSummary(
            status=MeasurementStatus.NOT_RUN,
            repetitions=0,
            median_wall_s=1.0,
            refusal_reason="NOT_RUN: skipped",
        )
    with pytest.raises(ValueError):
        TimingSummary(status=MeasurementStatus.NOT_RUN, repetitions=0)
    with pytest.raises(ValueError):
        TimingSummary(status=MeasurementStatus.NOT_RUN, repetitions=0, refusal_reason="  ")


def test_timing_summary_refuses_measured_without_numbers() -> None:
    with pytest.raises(ValueError):
        TimingSummary(status=MeasurementStatus.MEASURED, repetitions=5)


def test_memory_summary_refuses_numbers_without_measured_status() -> None:
    with pytest.raises(ValueError):
        MemorySummary(
            status=MeasurementStatus.NOT_MEASURED,
            peak_rss_bytes=1,
            refusal_reason="NOT_MEASURED: rusage unavailable",
        )
    with pytest.raises(ValueError):
        MemorySummary(status=MeasurementStatus.NOT_MEASURED)


def test_measure_reports_cold_warm_and_rss() -> None:
    calls: list[int] = []

    def operation() -> None:
        calls.append(len(calls))

    result = measure(operation, warmup=1, repetitions=MIN_REPETITIONS)
    assert len(calls) == 1 + 1 + MIN_REPETITIONS
    assert result.cold.status is MeasurementStatus.MEASURED
    assert result.cold.repetitions == 1
    warm = result.warm
    assert warm.status is MeasurementStatus.MEASURED
    assert warm.repetitions == MIN_REPETITIONS
    assert warm.min_wall_s is not None and warm.max_wall_s is not None
    assert warm.median_wall_s is not None and warm.p95_wall_s is not None
    assert warm.min_wall_s <= warm.median_wall_s <= warm.max_wall_s
    assert warm.p95_wall_s >= warm.median_wall_s
    memory = result.memory
    assert memory.status is MeasurementStatus.MEASURED
    assert memory.peak_rss_bytes is not None and memory.peak_rss_bytes > 0
    assert memory.ru_maxrss_unit in {"bytes", "kibibytes"}


def test_measure_rejects_zero_repetitions() -> None:
    with pytest.raises(ValueError):
        measure(lambda: None, repetitions=0)
    with pytest.raises(ValueError):
        measure(lambda: None, warmup=-1)


def test_peak_rss_unit_is_consistent() -> None:
    bytes_value, raw, unit = peak_rss()
    assert bytes_value > 0 and raw > 0
    if unit == "bytes":
        assert bytes_value == raw
    else:
        assert bytes_value == raw * 1024


def test_nearest_rank_p95() -> None:
    assert nearest_rank_p95([3.0]) == 3.0
    assert nearest_rank_p95([1.0, 2.0, 3.0, 4.0, 5.0]) == 5.0
    assert nearest_rank_p95([1.0, 2.0]) == 2.0
    values = [float(i) for i in range(1, 21)]
    assert nearest_rank_p95(values) == 19.0
    with pytest.raises(ValueError):
        nearest_rank_p95([])


# --------------------------------------------------------------------------- #
# parity gate
# --------------------------------------------------------------------------- #
def test_parity_gate_passes_only_when_every_stream_matches() -> None:
    result = parity_gate(_full_digests(), _full_digests())
    assert result.status is GateStatus.PASS
    assert result.passed()
    assert result.blocking_reasons == ()
    assert len(result.streams) == len(REQUIRED_PARITY_STREAMS)


def test_parity_gate_is_blocked_when_a_digest_is_missing() -> None:
    baseline = ParityStreamDigests(decision=DIGEST_A, funding=DIGEST_B)
    candidate = ParityStreamDigests(decision=DIGEST_A, funding=DIGEST_B)
    result = parity_gate(baseline, candidate)
    assert result.status is GateStatus.BLOCKED
    assert not result.passed()
    missing = tuple(
        stream for stream in REQUIRED_PARITY_STREAMS if stream not in {"decision", "funding"}
    )
    assert result.blocking_reasons == tuple(
        f"DIGEST_MISSING:{s}:baseline+candidate" for s in missing
    )


def test_parity_gate_reports_which_side_is_missing() -> None:
    baseline = _full_digests()
    candidate = _full_digests(report=None)
    result = parity_gate(baseline, candidate)
    assert result.status is GateStatus.BLOCKED
    assert result.blocking_reasons == ("DIGEST_MISSING:report:candidate",)


def test_parity_gate_fails_on_drift() -> None:
    baseline = _full_digests()
    candidate = _full_digests(decision=DIGEST_B)
    result = parity_gate(baseline, candidate)
    assert result.status is GateStatus.FAIL
    assert result.blocking_reasons == ("DIGEST_DRIFT:decision",)
    verdict = next(stream for stream in result.streams if stream.stream == "decision")
    assert verdict.status is GateStatus.FAIL


def test_parity_gate_drift_dominates_missing() -> None:
    baseline = _full_digests()
    candidate = _full_digests(decision=DIGEST_B, order=None)
    result = parity_gate(baseline, candidate)
    assert result.status is GateStatus.FAIL


def test_parity_streams_reject_blank_digests() -> None:
    with pytest.raises(ValueError):
        ParityStreamDigests(decision="   ")
    with pytest.raises(ValueError):
        ParityStreamDigests(decision="not-hex")


def test_parity_stream_lookup_rejects_unknown_stream() -> None:
    with pytest.raises(ValueError, match="unknown parity stream"):
        _full_digests().digest("pnl")


# --------------------------------------------------------------------------- #
# receipt invariants
# --------------------------------------------------------------------------- #
def _receipt_kwargs() -> dict[str, object]:
    return {
        "issue_id": "M0-08",
        "workload": "unit",
        "mode": "candidate",
        "cold": TimingSummary(
            status=MeasurementStatus.MEASURED,
            repetitions=1,
            median_wall_s=1.0,
            p95_wall_s=1.0,
            min_wall_s=1.0,
            max_wall_s=1.0,
        ),
        "warm": TimingSummary(
            status=MeasurementStatus.MEASURED,
            repetitions=MIN_REPETITIONS,
            median_wall_s=0.9,
            p95_wall_s=1.0,
            min_wall_s=0.8,
            max_wall_s=1.0,
        ),
        "memory": MemorySummary(
            status=MeasurementStatus.MEASURED,
            peak_rss_bytes=1024,
            ru_maxrss_raw=1024,
            ru_maxrss_unit="bytes",
        ),
        "within_rss_target": True,
        "oom": False,
        "code_sha": "deadbeef",
        "config_sha256": DIGEST_A,
        "tape": TapeIdentity(
            path="t",
            sha256=DIGEST_A,
            bytes=10,
            bars=3,
            instrument_id="X",
            status=MeasurementStatus.MEASURED,
        ),
        "digests": _full_digests(),
        "scope": ScopeCoverage.observed(legs=1, months=3.0),
    }


def test_receipt_refuses_empty_fields_without_a_refusal_reason() -> None:
    with pytest.raises(ValueError, match="code_sha is None"):
        ResourceReceipt(**{**_receipt_kwargs(), "code_sha": None})  # type: ignore[arg-type]


def test_receipt_accepts_named_refusals() -> None:
    receipt = ResourceReceipt(
        **{  # type: ignore[arg-type]
            **_receipt_kwargs(),
            "code_sha": None,
            "refusals": {"code_sha": "GIT_HEAD_UNREADABLE: no checkout"},
        }
    )
    assert receipt.code_sha is None
    assert receipt.refusals["code_sha"].startswith("GIT_HEAD_UNREADABLE")


def test_receipt_rejects_refusal_for_present_field() -> None:
    with pytest.raises(ValueError, match="carries a refusal reason but has a value"):
        ResourceReceipt(
            **{  # type: ignore[arg-type]
                **_receipt_kwargs(),
                "refusals": {"code_sha": "GIT_HEAD_UNREADABLE: no checkout"},
            }
        )


def test_receipt_rejects_thin_warm_median() -> None:
    kwargs = _receipt_kwargs()
    kwargs["warm"] = TimingSummary(
        status=MeasurementStatus.MEASURED,
        repetitions=MIN_REPETITIONS - 1,
        median_wall_s=0.9,
        p95_wall_s=1.0,
        min_wall_s=0.8,
        max_wall_s=1.0,
    )
    with pytest.raises(ValueError, match="at least"):
        ResourceReceipt(**kwargs)  # type: ignore[arg-type]


def test_scope_below_target_is_not_run() -> None:
    scope = ScopeCoverage.observed(legs=1, months=3.0)
    assert scope.status is MeasurementStatus.NOT_RUN
    assert scope.covers_target() is False
    assert scope.refusal_reason is not None
    assert scope.refusal_reason.startswith("SCOPE_BELOW_TARGET")
    with pytest.raises(ValueError):
        ScopeCoverage(legs=1, months=3.0, status=MeasurementStatus.MEASURED)


def test_receipt_roundtrips_through_json(tmp_path: Path) -> None:
    receipt = ResourceReceipt(**{**_receipt_kwargs(), "notes": ("n",)})  # type: ignore[arg-type]
    written = receipt.write(tmp_path / "receipt.json")
    loaded = load_receipt(written)
    assert loaded == receipt
    assert loaded.as_dict()["digests"]["decision"] == DIGEST_A


# --------------------------------------------------------------------------- #
# real tape: measured parity, honest scope
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_git_sha_is_readable_in_a_checkout() -> None:
    sha = git_sha()
    assert sha is None or len(sha) == 40


@pytest.mark.slow
def test_funding_digest_is_deterministic_on_real_tape() -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    first = funding_digest(BTC_TAPE)
    assert first == funding_digest(BTC_TAPE)
    assert len(first) == 64


@pytest.mark.slow
def test_real_measurement_parity_and_blocked_gate(tmp_path: Path) -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")

    baseline = build_receipt(tape=BTC_TAPE, mode="baseline", repetitions=5, limit=400)
    candidate = build_receipt(tape=BTC_TAPE, mode="candidate", repetitions=5, limit=400)

    assert baseline.warm.status is MeasurementStatus.MEASURED
    assert baseline.warm.repetitions == 5
    assert candidate.cache is not None
    assert candidate.cache.version == "v9.feature-cache.1"
    assert candidate.cache.bars_observed == 400

    gate = compare_receipts(baseline, candidate)
    verdicts = {verdict.stream: verdict.status for verdict in gate.streams}
    assert verdicts["decision"] is GateStatus.PASS
    assert verdicts["funding"] is GateStatus.PASS
    for stream in ("order", "fill", "report"):
        assert verdicts[stream] is GateStatus.BLOCKED
    # Fail-closed: three unrun streams cannot certify the candidate.
    assert gate.status is GateStatus.BLOCKED
    assert not gate.passed()

    assert candidate.scope.status is MeasurementStatus.NOT_RUN
    assert candidate.within_rss_target is True
    assert candidate.oom is False
    assert candidate.tape.status is MeasurementStatus.MEASURED
    assert candidate.tape.sha256 is not None

    written = candidate.write(tmp_path / "candidate.json")
    assert load_receipt(written).digests.decision == candidate.digests.decision


def test_values_parity_is_fail_closed() -> None:
    assert values_parity(DIGEST_A, DIGEST_A) is GateStatus.PASS
    assert values_parity(DIGEST_A, DIGEST_B) is GateStatus.FAIL
    assert values_parity(None, DIGEST_A) is GateStatus.BLOCKED
    assert values_parity(DIGEST_A, None) is GateStatus.BLOCKED
    assert values_parity("", DIGEST_A) is GateStatus.BLOCKED


@pytest.mark.slow
def test_feature_value_receipt_parity_on_real_tape(tmp_path: Path) -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")

    direct = replay_feature_values(BTC_TAPE, use_cache=False, consumers=2, limit=300)
    cached = replay_feature_values(BTC_TAPE, use_cache=True, consumers=2, limit=300)
    assert direct.values_digest == cached.values_digest
    assert direct.reads == cached.reads == 2 * 5 * (300 - 40 + 1)
    assert cached.cache is not None
    assert cached.cache.hits == cached.cache.computations
    assert cached.cache.evictions > 0  # the LRU bound was actually reached
    assert cached.cache.entries == cached.cache.max_entries

    candidate = build_feature_receipt(
        tape=BTC_TAPE, mode="candidate", consumers=2, repetitions=5, limit=300
    )
    baseline = build_feature_receipt(
        tape=BTC_TAPE, mode="baseline", consumers=2, repetitions=5, limit=300
    )
    assert values_parity(baseline.values_digest, candidate.values_digest) is GateStatus.PASS
    assert candidate.cache is not None
    assert candidate.issue_id == "M1-06"
    written = candidate.write(tmp_path / "features.json")
    assert written.exists()
