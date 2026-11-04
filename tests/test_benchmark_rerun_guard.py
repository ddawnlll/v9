"""A registered run identity is refused before the benchmark CLI writes anything.

The guard pinned here (``v8_next.app.rerun_guard``, consumed by
``v8_next.app.benchmark``): a second ``benchmark`` invocation on the same
``--output-dir`` with ``--allow-rerun`` used to execute the whole window -- receipt,
second ledger append, certificate, forensic HTML, window manifest COMPLETED -- and
only then die in ``ResearchStore.record_run`` with an uncaught ``ValueError:
registered run identity cannot be rewritten`` (exit 1), because the registered
payload folds in the sha256 of the benchmark ledger that a re-execution appends to,
so the second registration can never match. The CLI now fails closed before the
first write, with the same named reason and exit code as ``benchmark-portfolio``.

Guard section: no tape, no engine -- part of the fast loop. End-to-end section:
real quad tape (marked slow), skipped when the tape is absent.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

TAPE = Path(__file__).resolve().parents[2] / "research/tape/quad-1h-12m"
BARS = "120"


def _tape_or_skip() -> Path:
    if not (TAPE / "tape.jsonl").exists():
        pytest.skip("quad tape absent")
    return TAPE


def _argv(tape: Path, out_dir: Path) -> list[str]:
    # The forensic report is directed inside the run dir: the default would write
    # into the repository's artifacts tree for every test run.
    return [
        "--tape-path", str(tape), "--bars", BARS,
        "--output-dir", str(out_dir), "--html-out", str(out_dir / "forensic_report.html"),
    ]


def _runs_rows(out_dir: Path) -> int:
    db = sqlite3.connect(out_dir / "runs" / "runs.sqlite")
    try:
        return int(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
    finally:
        db.close()


def _manifest_path(out_dir: Path) -> Path:
    manifests = [
        p for p in (out_dir / "runs").glob("*.json") if not p.name.endswith(".telemetry.json")
    ]
    assert len(manifests) == 1, [p.name for p in manifests]
    return manifests[0]


def _writes(out_dir: Path) -> dict[str, object]:
    """Everything a re-execution would rewrite: ledger, manifest, report and rows."""
    ledger = out_dir / "benchmark_ledger.jsonl"
    manifest = _manifest_path(out_dir)
    report = out_dir / "forensic_report.html"
    return {
        "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
        "ledger_lines": len(ledger.read_text(encoding="utf-8").splitlines()),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "manifest_state": json.loads(manifest.read_text(encoding="utf-8"))["state"],
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "runs_rows": _runs_rows(out_dir),
    }


def test_fresh_output_dir_registers_nothing_and_creates_nothing(tmp_path: Path) -> None:
    from v8_next.app.benchmark import registered_run_payload

    out_dir = tmp_path / "fresh"
    assert registered_run_payload(out_dir, "sha256:absent") is None
    assert not (out_dir / "runs").exists(), "the guard must not create the store it reads"


def test_registered_run_payload_is_the_store_payload_and_is_key_scoped(tmp_path: Path) -> None:
    """Another window in the same dir is not this run's registration."""
    from v8_next.app.benchmark import registered_run_payload
    from v8_next.evaluation.store import ResearchStore

    out_dir = tmp_path / "bench"
    (out_dir / "runs").mkdir(parents=True)
    store = ResearchStore(out_dir / "runs" / "runs.sqlite")
    try:
        store.record_run(
            run_key="sha256:key", payload='{"artifacts":[]}', digest="sha256:key",
            registered_ns=1,
        )
    finally:
        store.close()

    assert registered_run_payload(out_dir, "sha256:key") == '{"artifacts":[]}'
    assert registered_run_payload(out_dir, "sha256:another-window") is None


def test_both_clis_read_one_named_refusal_and_one_exit_code() -> None:
    from v8_next.app import benchmark as bench_mod
    from v8_next.app import portfolio as port_mod
    from v8_next.app import rerun_guard

    assert (
        rerun_guard.RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY
        == "RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY"
    )
    # 0 = ran and verified, 1 = ran and did not verify: a refusal is neither.
    assert rerun_guard.REFUSED_EXIT_CODE not in (0, 1)
    # One definition, two call sites: neither CLI may carry its own copy again.
    assert bench_mod.REFUSED_EXIT_CODE == rerun_guard.REFUSED_EXIT_CODE
    assert port_mod.REFUSED_EXIT_CODE == rerun_guard.REFUSED_EXIT_CODE
    assert port_mod.RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY == (
        rerun_guard.RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY
    )
    assert bench_mod.registered_run_payload is rerun_guard.registered_run_payload
    assert port_mod.registered_run_payload is rerun_guard.registered_run_payload


@pytest.mark.slow
def test_second_run_with_allow_rerun_is_refused_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from v8_next.app import benchmark as bench_mod
    from v8_next.app import rerun_guard

    tape = _tape_or_skip()
    out_dir = tmp_path / "bench"
    argv = _argv(tape, out_dir)
    assert bench_mod.main(argv) == 0
    capsys.readouterr()

    before = _writes(out_dir)
    assert before["manifest_state"] == "COMPLETED"
    assert before["runs_rows"] == 1
    assert before["ledger_lines"] == 1

    rc = bench_mod.main([*argv, "--allow-rerun"])
    text = "".join(capsys.readouterr())
    assert rc == rerun_guard.REFUSED_EXIT_CODE
    assert rc not in (0, 1)
    assert rerun_guard.RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY in text
    assert "Traceback" not in text and "ValueError" not in text

    # (b) Not one write: same ledger bytes and line count, same COMPLETED manifest,
    # same report, same rows.
    assert _writes(out_dir) == before

    # (c) The completed-window refusal without the flag keeps its own message and
    # exit code, and still writes nothing.
    assert bench_mod.main(argv) == rerun_guard.REFUSED_EXIT_CODE
    no_flag = "".join(capsys.readouterr())
    assert "refusing to re-execute a completed window" in no_flag
    assert "Traceback" not in no_flag
    assert _writes(out_dir) == before


@pytest.mark.slow
def test_first_run_with_allow_rerun_still_registers(tmp_path: Path) -> None:
    """(c) No registration for this key: the first-registration path is unchanged."""
    from v8_next.app import benchmark as bench_mod

    tape = _tape_or_skip()
    out_dir = tmp_path / "bench"
    assert bench_mod.main([*_argv(tape, out_dir), "--allow-rerun"]) == 0
    assert _writes(out_dir)["runs_rows"] == 1
