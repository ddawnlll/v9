"""NX05 (#426) — one window/profile/run-key/resume contract for both CLI paths.

Evidence classes:

* **mechanics** — profile rules (a bar-count window is smoke, never a release
  benchmark), run-key determinism and input sensitivity, the completed-window
  refusal, and the rule that a foreign or unfinished run cannot resolve a gate.
* **evaluative** — on the real tape: a UTC window is loaded exactly and
  deterministically, and the receipt's input binding changes when the bound run
  identity changes (so another window's artifact cannot pass as this one's).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles
from v8_next.evaluation.run_window import (
    WINDOW_MANIFEST_VERSION,
    RunKey,
    WindowAlreadyCompleted,
    WindowRunManifest,
    WindowSpec,
    assert_window_resumable,
    execution_profile,
    incomplete_runs,
    load_window_manifest,
    verify_artifact_run_key,
    write_window_manifest,
)
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

QUAD_TAPE = tape_path("multi-1h-4y", "tape.jsonl")
JAN_2025_START_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
FEB_2025_START_MS = 1_738_368_000_000  # 2025-02-01T00:00:00Z
HOUR_MS = 3_600_000


def _run_key(**overrides) -> RunKey:
    window = overrides.pop(
        "window",
        WindowSpec(tape_path="research/tape/quad-1h-12m", profile="smoke", bars=385),
    )
    kwargs = dict(
        window=window,
        case_id="BC-NX05",
        policy_id="pol_nx05",
        dataset_sha256="a" * 64,
        strategy_config='{"quorum": 1}',
        capital="10000",
        taker_fee="0.0005",
        baseline="cash",
        execution_profile_digest="b" * 64,
        code_and_lock_hash="c" * 64,
    )
    kwargs.update(overrides)
    return RunKey.build(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_bar_count_window_is_smoke_and_says_so() -> None:
    smoke = WindowSpec(tape_path="tape", profile="smoke", bars=385)
    smoke.validate()
    assert smoke.is_smoke is True
    assert smoke.proves_economic_evidence is False
    label = smoke.label()
    assert "SMOKE" in label
    assert "NOT economic evidence" in label
    assert execution_profile("smoke").economic_evidence is False

    # a fold/benchmark window must be UTC-bounded, never a bar count
    with pytest.raises(ValueError, match="bar-count window is a smoke window"):
        WindowSpec(tape_path="tape", profile="fold", bars=5000).validate()
    with pytest.raises(ValueError, match="requires explicit UTC start and end"):
        WindowSpec(tape_path="tape", profile="benchmark").validate()
    bounded = WindowSpec(
        tape_path="tape", profile="fold", start_ms=JAN_2025_START_MS, end_ms=FEB_2025_START_MS
    )
    bounded.validate()
    assert bounded.proves_economic_evidence is True
    with pytest.raises(ValueError, match="unknown execution profile"):
        execution_profile("release")


def test_window_refuses_inverted_or_oversized_smoke() -> None:
    with pytest.raises(ValueError, match="start must precede"):
        WindowSpec(
            tape_path="tape",
            profile="fold",
            start_ms=FEB_2025_START_MS,
            end_ms=JAN_2025_START_MS,
        ).validate()
    with pytest.raises(ValueError, match="exceeds the smoke profile cap"):
        WindowSpec(tape_path="tape", profile="smoke", bars=100_000).validate()


def test_run_key_is_deterministic_and_input_sensitive() -> None:
    first = _run_key()
    second = _run_key()
    assert first.digest == second.digest
    assert first.components == second.components
    # no wall clock anywhere in the identity
    assert not any("time" in key or "now" in key for key in first.components)

    other_window = _run_key(
        window=WindowSpec(
            tape_path="research/tape/quad-1h-12m",
            profile="fold",
            start_ms=JAN_2025_START_MS,
            end_ms=FEB_2025_START_MS,
        )
    )
    assert other_window.digest != first.digest
    assert _run_key(dataset_sha256="d" * 64).digest != first.digest
    assert _run_key(code_and_lock_hash="e" * 64).digest != first.digest
    with pytest.raises(ValueError, match="duplicate run-key component"):
        _run_key(extra={"case_id": "collision"})


def _manifest(tmp_path: Path, *, state: str, key: str, artifact: Path | None = None):
    artifacts = ()
    if artifact is not None:
        artifacts = (
            {
                "role": "native_trades",
                "path": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "bytes": artifact.stat().st_size,
            },
        )
    return WindowRunManifest(
        run_key=key,
        window=WindowSpec(tape_path="tape", profile="smoke", bars=300).as_dict(),
        state=state,  # type: ignore[arg-type]
        artifacts=artifacts,
        started_ns=1,
        finished_ns=2,
    )


def test_completed_window_refuses_reexecution(tmp_path: Path) -> None:
    key = _run_key().digest
    path = tmp_path / "runs" / "one.json"
    write_window_manifest(path, _manifest(tmp_path, state="COMPLETED", key=key))

    loaded = load_window_manifest(path)
    assert loaded is not None and loaded.completed is True
    with pytest.raises(WindowAlreadyCompleted, match="already COMPLETED"):
        assert_window_resumable(loaded, key)

    write_window_manifest(path, _manifest(tmp_path, state="RUNNING", key=key))
    resumable = load_window_manifest(path)
    assert resumable is not None
    assert assert_window_resumable(resumable, key) is resumable  # incomplete, never success
    assert [m.run_key for m in incomplete_runs([path])] == [key]

    foreign = load_window_manifest(path)
    assert foreign is not None
    with pytest.raises(ValueError, match="another run key"):
        assert_window_resumable(foreign, "sha256:" + "f" * 64)


def test_foreign_or_incomplete_run_cannot_resolve_a_gate(tmp_path: Path) -> None:
    key = _run_key().digest
    artifact = tmp_path / "trades.jsonl"
    artifact.write_text('{"trade_id":"t"}\n')

    complete = _manifest(tmp_path, state="COMPLETED", key=key, artifact=artifact)
    ok, reason = verify_artifact_run_key(complete, key)
    assert ok, reason

    ok, reason = verify_artifact_run_key(complete, "sha256:" + "0" * 64)
    assert not ok and reason.startswith("FOREIGN_RUN_ARTIFACT")

    running = _manifest(tmp_path, state="RUNNING", key=key, artifact=artifact)
    ok, reason = verify_artifact_run_key(running, key)
    assert not ok and reason.startswith("INCOMPLETE_RUN")

    artifact.write_text('{"trade_id":"tampered"}\n')
    ok, reason = verify_artifact_run_key(complete, key)
    assert not ok and reason.startswith("ARTIFACT_HASH_MISMATCH")

    artifact.unlink()
    ok, reason = verify_artifact_run_key(complete, key)
    assert not ok and reason.startswith("ARTIFACT_MISSING")


def test_manifest_round_trip_and_version_guard(tmp_path: Path) -> None:
    key = _run_key().digest
    path = tmp_path / "manifest.json"
    manifest = _manifest(tmp_path, state="RUNNING", key=key)
    write_window_manifest(path, manifest)
    loaded = load_window_manifest(path)
    assert loaded is not None
    assert loaded.as_dict() == manifest.as_dict()
    assert load_window_manifest(tmp_path / "absent.json") is None

    path.write_text(path.read_text().replace(WINDOW_MANIFEST_VERSION, "v87-window-manifest-v0"))
    with pytest.raises(ValueError, match="unsupported window manifest version"):
        load_window_manifest(path)


# --------------------------------------------------------------------------- #
# evaluative — real tape
# --------------------------------------------------------------------------- #
def test_real_utc_window_is_exact_bounded_and_deterministic() -> None:
    if not QUAD_TAPE.is_file():
        pytest.skip(f"four-year tape absent at {QUAD_TAPE}")
    window = load_tape_candles(
        QUAD_TAPE, instrument="BTCUSDT", start_ms=JAN_2025_START_MS, end_ms=FEB_2025_START_MS
    )
    expected_bars = (FEB_2025_START_MS - JAN_2025_START_MS) // HOUR_MS  # 744
    assert len(window) == expected_bars
    assert window[0].start_ns == JAN_2025_START_MS * 1_000_000
    assert window[-1].end_ns == FEB_2025_START_MS * 1_000_000
    for candle in window:
        assert JAN_2025_START_MS * 1_000_000 <= candle.start_ns < FEB_2025_START_MS * 1_000_000

    # identical to the corresponding slice of the unbounded series: the window is
    # a selection, not a re-derivation
    full = load_tape_candles(QUAD_TAPE, instrument="BTCUSDT")
    index_of = {c.start_ns: i for i, c in enumerate(full)}
    offset = index_of[window[0].start_ns]
    assert full[offset : offset + expected_bars] == list(window)

    # an empty window fails closed instead of returning nothing
    with pytest.raises(FileNotFoundError, match="no kline rows"):
        load_tape_candles(
            QUAD_TAPE,
            instrument="BTCUSDT",
            start_ms=1_000_000_000_000,  # 2001: before the tape begins
            end_ms=1_000_000_000_000 + HOUR_MS,
        )


def test_receipt_input_binding_grows_with_the_run_identity(tmp_path: Path) -> None:
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=120)
    case = BenchmarkCase(
        case_id="BC-NX05-BIND",
        policy_id="pol_nx05",
        dataset_name="BTCUSDT-1H-REAL",
        strategy_config=ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28),
    )
    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    base = _run_key()
    first = runner.run(case, candles, run_identity=base.components)
    again = runner.run(case, candles, run_identity=base.components)
    assert first.receipt.input_binding == again.receipt.input_binding

    other = _run_key(dataset_sha256="d" * 64)
    third = runner.run(case, candles, run_identity=other.components)
    assert third.receipt.input_binding != first.receipt.input_binding
    # the receipt still verifies under its own binding
    assert third.receipt.verify()[0] is True


def test_cli_binds_one_run_key_to_report_telemetry_ledger_and_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """NX05.R2: the same run key reaches all four sinks of one smoke run."""
    from v8_next.app.benchmark import main as benchmark_main
    from v8_next.evaluation.benchmark_receipt import BenchmarkLedger
    from v8_next.evaluation.store import ResearchStore

    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    out_dir = tmp_path / "smoke"
    exit_code = benchmark_main(
        [
            "--bars", "150",
            "--diagnostic-only",
            "--output-dir", str(out_dir),
            "--html-out", str(out_dir / "report.html"),
        ]
    )
    capsys.readouterr()
    assert exit_code == 0

    manifests = sorted((out_dir / "runs").glob("*.json"))
    runs = [path for path in manifests if not path.name.endswith(".telemetry.json")]
    assert len(runs) == 1
    manifest = json.loads(runs[0].read_text())
    key = manifest["run_key"]
    assert manifest["state"] == "COMPLETED"
    assert manifest["window"]["is_smoke"] is True

    # sink 2: telemetry artifact carries the same key
    telemetry = runs[0].with_name(runs[0].name.replace(".json", ".telemetry.json"))
    assert telemetry.is_file()
    assert json.loads(telemetry.read_text())["run_key"] == key

    # sink 3: the ledger receipt's input binding is the one the manifest recorded
    ledger = BenchmarkLedger.load_jsonl(out_dir / "benchmark_ledger.jsonl")
    assert len(ledger.entries) == 1
    assert ledger.entries[0].receipt.input_binding == manifest["detail"]["input_binding"]

    # sink 4: the ResearchStore row is the same identity
    store = ResearchStore(out_dir / "runs" / "runs.sqlite")
    try:
        recorded = store.get_run(key)
        assert recorded is not None
        payload, digest = recorded
        assert digest == key
        assert json.loads(payload)["receipt_digest"] == manifest["detail"]["receipt_digest"]
    finally:
        store.close()

    # a second identical invocation is refused: no second ledger append
    rerun = benchmark_main(
        [
            "--bars", "150",
            "--diagnostic-only",
            "--output-dir", str(out_dir),
            "--html-out", str(out_dir / "report.html"),
        ]
    )
    capsys.readouterr()
    assert rerun == 3
    assert len(BenchmarkLedger.load_jsonl(out_dir / "benchmark_ledger.jsonl").entries) == 1

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
