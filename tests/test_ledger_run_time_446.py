"""#446 — the ledger says *when* a run happened, never by republishing its window end.

The defect: every producer filled the ledger's only time field
(``computed_at_timestamp_ns``) with the last bar of the data window it loaded, so the
append-only ledger -- the system's chronology authority -- could not say when any run
happened, and the product surface republished a 2025-07-21 window end as a 2026 run's
date (``site/status.json`` -> ``ledger.history[].ts`` rendered while ``generated_at`` was
2026-09-11). 21 stored entries carried only four distinct readings, one of them mis-scaled
to 1970-01-03 (``180000000000000`` ns = 50 hours).

The fix: two time quantities, two names, one unchanged digest.

* ``window_end_timestamp_ns`` -- the end of the data window the run measured;
* ``run_time_timestamp_ns``  -- the wall clock of the run itself, read when the receipt
  is built.

Neither enters any digest canon (``RECEIPT_CANON_TABLE`` does not grow, and the canon's
time slot still carries the window end), so the same inputs still hash to the same
``receipt_digest`` on a rerun -- the G2 determinism the chain proves. Both are published
only through :meth:`BenchmarkReceipt.time_publication`, which refuses each quantity *by
name* when it was not measured: ``RUN_TIME_UNMEASURED`` for every record written before
the field existed, ``WINDOW_END_UNDECLARED`` for a record that never says its reading is a
window end, ``WINDOW_END_MISSCALED`` for a reading that renders as 1970,
``RUN_TIME_PRECEDES_WINDOW_END`` for a run earlier than the window it measured.

Evidence classes:

* **mechanics** -- the two names, the refusals, the digest invariance and the consumer
  rendering, on MECHANICS ONLY fixtures. No assertion here claims performance, minting or
  authority; the fixture number comes from the canonical breakdown over fixed determinants
  (#408), exactly as the other ledger tests do it.
* **evaluative** -- the same reader applied to the *real* canonical ledger
  (``artifacts/benchmarks/benchmark_ledger.jsonl``): it still verifies under its own stored
  versions and its bytes are unchanged. Skips when the ledger is absent.

The canonical ledger is append-only and it *grows*: a producer run adds the next entry, and
that entry measures a real run time. So this file states the invariant that outlives every
entry -- a run time is published only when it was measured, never as the window end it
measured -- and it proves the append leaves the stored lines byte-identical. No stored
ledger entry, digest or fixture is written by this file: the canonical ledger is read-only
evidence here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from v8_next.adapters.tape_paths import data_path
from v8_next.evaluation.benchmark_receipt import (
    MAX_CLOCK_SKEW_NS,
    RECEIPT_CANON_TABLE,
    RECEIPT_DIGEST_VERSION,
    RUN_TIME_OUT_OF_RANGE,
    RUN_TIME_PRECEDES_WINDOW_END,
    RUN_TIME_UNMEASURED,
    TIME_EPOCH_FLOOR_NS,
    TIME_FIELD_UNIT,
    WINDOW_END_INCONSISTENT,
    WINDOW_END_MISSCALED,
    WINDOW_END_UNDECLARED,
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    ScoreEvidence,
    build_canon_payload,
)
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: MECHANICS ONLY: fixed determinants, so the fixture number is *derived* from evidence the
#: receipt binds (#408). No economic weight is claimed for it and no run is executed.
_MEASUREMENT: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 3, total_bars=60, total_trades=6, abstain_rate=0.2
)
MEASURED_EVIDENCE = ScoreEvidence.from_breakdown(compute_capability_breakdown(**_MEASUREMENT))
MEASURED_SCORE = compute_capability_score(**_MEASUREMENT)
assert MEASURED_SCORE is not None, "the mechanics fixture must measure a number"

HOUR_NS = 3_600_000_000_000

#: A window end shaped like the stored ones (2025-07-21T02:00:00Z), and the mis-scaled
#: reading the issue measured (``180000000000000`` ns -> 1970-01-03T02:00:00Z).
WINDOW_END_NS = 1_753_060_800_000_000_000
MISSCALED_NS = 180_000_000_000_000


def _canonical_ledger() -> Path:
    """The canonical ledger, from wherever this tree can see it.

    ``artifacts/`` is git-ignored, so a worktree checkout does not carry it; the primary
    checkout's copy is the same append-only file. Read-only either way.
    """
    candidates = (
        REPO_ROOT / "artifacts" / "benchmarks" / "benchmark_ledger.jsonl",
        data_path("artifacts", "benchmarks", "benchmark_ledger.jsonl"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _iso_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _receipt(
    tmp_path: Path,
    *,
    name: str = "trades.jsonl",
    computed_at_ns: int = WINDOW_END_NS,
    window_end_ns: int | None = None,
    run_time_ns: int | None = None,
    case_id: str = "BC-446-TIME",
) -> BenchmarkReceipt:
    """A MECHANICS ONLY receipt: real structure, no economic claim attached to it."""
    artifact = tmp_path / name
    artifact.write_text('{"trade_id":"t","pnl":1.0}\n')
    return BenchmarkReceipt.create(
        case_id=case_id,
        policy_id="pol_446",
        capability_score=MEASURED_SCORE,
        coverage_factor=MEASURED_EVIDENCE.coverage_factor,
        score_evidence=MEASURED_EVIDENCE,
        gates=GateVector(g0_identity=GateState.PASS, g1_causal_pit=GateState.UNKNOWN),
        computed_at_timestamp_ns=computed_at_ns,
        artifact_bindings=(ArtifactBinding.from_file("native_trades", artifact),),
        input_binding="input-binding-446",
        window_end_timestamp_ns=window_end_ns,
        run_time_timestamp_ns=run_time_ns,
    )


def _canon_of(receipt: BenchmarkReceipt) -> list:
    canon, refusal = build_canon_payload(
        digest_version=receipt.digest_version,
        case_id=receipt.case_id,
        policy_id=receipt.policy_id,
        capability_score=receipt.capability_score,
        coverage_factor=receipt.coverage_factor,
        gates=receipt.gates,
        artifact_bindings=receipt.artifact_bindings,
        computed_at_timestamp_ns=receipt.computed_at_timestamp_ns,
        economic_evidence_digest=receipt.economic_evidence_digest,
        economic_receipt_path=receipt.economic_receipt_path,
        input_binding=receipt.input_binding,
        window_evidence=receipt.window_evidence,
        score_evidence=receipt.score_evidence,
    )
    assert refusal is None, refusal
    assert canon is not None
    return canon


def _values_for_key(document: object, key: str) -> list:
    """Every value published under ``key`` anywhere in a JSON document."""
    found: list = []
    if isinstance(document, dict):
        for name, value in document.items():
            if name == key:
                found.append(value)
            found.extend(_values_for_key(value, key))
    elif isinstance(document, list):
        for item in document:
            found.extend(_values_for_key(item, key))
    return found


def _status_module():
    """`tools/generate_status.py`, imported: the real consumer, never a copy of its rule."""
    path = REPO_ROOT / "tools" / "generate_status.py"
    if not path.is_file():
        pytest.skip(f"status generator absent at {path}")
    spec = importlib.util.spec_from_file_location("v8_446_generate_status", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 1 — two quantities, two names (A2/A4 mechanics)
# --------------------------------------------------------------------------- #
def test_a_run_time_and_a_window_end_are_two_named_quantities(tmp_path: Path) -> None:
    """#446.A2: the run's own clock is inside its run, and the window end stays separate."""
    run_time = time.time_ns()
    receipt = _receipt(tmp_path, window_end_ns=WINDOW_END_NS, run_time_ns=run_time)

    times = receipt.time_publication()
    assert times.unit == TIME_FIELD_UNIT
    assert times.publishes_run_time is True
    assert times.run_time_ns == run_time
    assert times.run_time_refusal == ""
    assert times.publishes_window_end is True
    assert times.window_end_ns == WINDOW_END_NS
    assert times.window_end_refusal == ""

    # ...the run happened after the window it measured, and the two are different
    # quantities -- not one reading published twice under two names
    assert run_time >= times.window_end_ns
    assert run_time != times.window_end_ns

    vector = times.as_dict()
    assert vector["run_time_kind"] == "run_time"
    assert vector["window_end_kind"] == "window_end"
    assert vector["unit"] == TIME_FIELD_UNIT
    assert vector["run_time"] == run_time and vector["window_end"] == WINDOW_END_NS


def test_the_run_time_is_outside_the_canon_and_does_not_move_the_digest(tmp_path: Path) -> None:
    """#446.A3 / F1: same inputs, different wall clocks, one receipt_digest."""
    first = _receipt(
        tmp_path, name="first.jsonl", window_end_ns=WINDOW_END_NS, run_time_ns=time.time_ns()
    )
    second = _receipt(
        tmp_path,
        name="second.jsonl",
        window_end_ns=WINDOW_END_NS,
        run_time_ns=time.time_ns() + HOUR_NS,
    )

    assert first.run_time_timestamp_ns != second.run_time_timestamp_ns
    assert first.receipt_digest == second.receipt_digest, "a wall clock reached the digest"
    assert first.verify() == (True, "OK")
    assert second.verify() == (True, "OK")

    # the canon itself is unchanged by the wall clock, and the digested time slot is still
    # the window end (so stored entries keep verifying byte-for-byte)
    canon_first, canon_second = _canon_of(first), _canon_of(second)
    assert canon_first == canon_second
    assert WINDOW_END_NS in canon_first
    assert first.run_time_timestamp_ns not in canon_first
    assert second.run_time_timestamp_ns not in canon_second

    # #446 does not version the canon: v7 is still the write version and no historical
    # version grew a time field, so no RECEIPT_*_DIGEST_VERSION bump is needed
    assert RECEIPT_DIGEST_VERSION == "v8.5-digest-v7" == first.digest_version
    assert RECEIPT_CANON_TABLE[RECEIPT_DIGEST_VERSION].score_evidence == "always_present"
    for version in ("v8.5-digest-v2", "v8.5-digest-v3", "v8.5-digest-v4", "v8.5-digest-v5"):
        assert version in RECEIPT_CANON_TABLE


def test_no_number_is_published_under_a_name_that_does_not_describe_it(tmp_path: Path) -> None:
    """F5: neither time field ever carries a constant, a zero or an unmeasured stand-in."""
    receipt = _receipt(tmp_path, window_end_ns=WINDOW_END_NS, run_time_ns=time.time_ns())
    for constant in (0, 1_000, MISSCALED_NS):
        with pytest.raises(ValueError) as raised:
            _receipt(tmp_path, name=f"c{constant}.jsonl", run_time_ns=constant)
        assert RUN_TIME_OUT_OF_RANGE in str(raised.value)

    times = receipt.time_publication()
    assert times.run_time_ns not in (0, 1_000)
    assert times.window_end_ns not in (0, 1_000)


# --------------------------------------------------------------------------- #
# 2 — every refusal is named, and no quantity is substituted for another
# --------------------------------------------------------------------------- #
def test_a_record_without_a_run_time_is_refused_by_name_never_by_substitution() -> None:
    """A1: the stored shape. The window end is not republished as a run time."""
    receipt = BenchmarkReceipt.model_validate(
        {
            "case_id": "BC-446-STORED-SHAPE",
            "policy_id": "pol_446",
            "digest_version": "v8.5-digest-v5",
            "gates": GateVector(g0_identity=GateState.PASS).model_dump(),
            "computed_at_timestamp_ns": WINDOW_END_NS,
            "receipt_digest": "0" * 64,
        }
    )
    assert receipt.run_time_timestamp_ns is None
    assert receipt.window_end_timestamp_ns is None

    times = receipt.time_publication()
    assert times.publishes_run_time is False
    assert times.run_time_ns is None
    assert times.run_time_refusal.startswith(RUN_TIME_UNMEASURED)
    assert times.publishes_window_end is False
    assert times.window_end_ns is None
    assert times.window_end_refusal.startswith(WINDOW_END_UNDECLARED)
    # the refused window end is not rendered as a date under either name
    vector = times.as_dict()
    assert vector["run_time"] is None and vector["window_end"] is None
    assert vector["run_time_kind"] == "" and vector["window_end_kind"] == ""


def test_a_mis_scaled_reading_is_refused_by_name_and_never_rendered_as_a_date(
    tmp_path: Path,
) -> None:
    """F2/A4: the 50-hour reading (1970-01-03) is named, not published as a date."""
    with pytest.raises(ValueError, match=WINDOW_END_MISSCALED):
        _receipt(
            tmp_path,
            name="mis-scaled.jsonl",
            computed_at_ns=MISSCALED_NS,
            window_end_ns=MISSCALED_NS,
            run_time_ns=time.time_ns(),
        )

    # a record already at rest that declares it: refused on the read path, never rendered
    stored = _receipt(tmp_path, name="stored.jsonl", computed_at_ns=MISSCALED_NS)
    forged = stored.model_copy(update={"window_end_timestamp_ns": MISSCALED_NS})
    assert forged.window_end_refusal_reason().startswith(WINDOW_END_MISSCALED)
    times = forged.time_publication()
    assert times.window_end_ns is None
    assert times.publishes_window_end is False
    assert _iso_ns(MISSCALED_NS).startswith("1970-01-03")
    assert times.as_dict()["window_end"] is None
    assert _iso_ns(MISSCALED_NS) not in json.dumps(times.as_dict())


def test_the_write_path_refuses_a_contradictory_or_premature_chronology(tmp_path: Path) -> None:
    """F5/D2: a producer cannot write a record whose own chronology contradicts itself."""
    # a run earlier than the window it measured
    with pytest.raises(ValueError, match=RUN_TIME_PRECEDES_WINDOW_END):
        _receipt(
            tmp_path,
            name="early.jsonl",
            window_end_ns=WINDOW_END_NS,
            run_time_ns=WINDOW_END_NS - HOUR_NS,
        )
    # a reading a long way ahead of the reading clock
    with pytest.raises(ValueError, match=RUN_TIME_OUT_OF_RANGE):
        _receipt(
            tmp_path,
            name="future.jsonl",
            window_end_ns=WINDOW_END_NS,
            run_time_ns=time.time_ns() + 2 * MAX_CLOCK_SKEW_NS,
        )
    # a declared window end that contradicts the one carried inside the digest
    with pytest.raises(ValueError, match=WINDOW_END_INCONSISTENT):
        _receipt(
            tmp_path,
            name="contradiction.jsonl",
            window_end_ns=WINDOW_END_NS - HOUR_NS,
            run_time_ns=time.time_ns(),
        )
    assert TIME_EPOCH_FLOOR_NS > 0


# --------------------------------------------------------------------------- #
# 3 — the chain still verifies, and the fields survive a round trip
# --------------------------------------------------------------------------- #
def test_a_ledger_of_old_and_new_entries_still_verifies(tmp_path: Path) -> None:
    """A5: adding the two fields neither breaks the chain nor moves stored digests."""
    ledger = BenchmarkLedger()
    stored_shape = _receipt(tmp_path, name="stored.jsonl")  # neither field declared
    declared = _receipt(
        tmp_path,
        name="declared.jsonl",
        window_end_ns=WINDOW_END_NS,
        run_time_ns=time.time_ns(),
        case_id="BC-446-TIME-NEW",
    )
    ledger.append(stored_shape)
    ledger.append(declared)

    report = ledger.verify_report()
    assert report.overall == "OK", [e.as_dict() for e in report.entries if not e.fully_valid]
    assert report.chain_valid and report.digests_valid

    path = tmp_path / "benchmark_ledger.jsonl"
    ledger.save_jsonl(path)
    reloaded = BenchmarkLedger.load_jsonl(path)
    assert [e.receipt.receipt_digest for e in reloaded.entries] == [
        stored_shape.receipt_digest,
        declared.receipt_digest,
    ]
    assert reloaded.verify_report().overall == "OK"
    assert reloaded.entries[0].receipt.time_publication().publishes_run_time is False
    assert reloaded.entries[1].receipt.time_publication().run_time_ns == (
        declared.run_time_timestamp_ns
    )
    assert reloaded.entries[1].receipt.window_end_timestamp_ns == WINDOW_END_NS


# --------------------------------------------------------------------------- #
# 4 — the product surface: a run date, or nothing (A4/F4)
# --------------------------------------------------------------------------- #
def _write_ledger(path: Path, receipts: list[BenchmarkReceipt]) -> None:
    ledger = BenchmarkLedger()
    for receipt in receipts:
        ledger.append(receipt)
    ledger.save_jsonl(path)


def test_status_history_publishes_the_run_time_and_never_the_window_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4/F4: the newest row's date is the run's own clock; the window end stays separate."""
    status = _status_module()
    run_time = time.time_ns()
    ledger_path = tmp_path / "benchmark_ledger.jsonl"
    _write_ledger(
        ledger_path,
        [
            _receipt(tmp_path, name="stored.jsonl"),  # the pre-#446 shape
            _receipt(
                tmp_path,
                name="new.jsonl",
                window_end_ns=WINDOW_END_NS,
                run_time_ns=run_time,
                case_id="BC-446-TIME-NEW",
            ),
        ],
    )
    monkeypatch.setattr(status, "LEDGER", ledger_path)

    out = status.read_ledger()
    rows = out["history"]
    assert len(rows) == 2

    newest = rows[-1]
    assert newest["run_time_kind"] == "run_time"
    assert newest["run_time"] == _iso_ns(run_time)
    assert newest["window_end_kind"] == "window_end"
    assert newest["window_end"] == _iso_ns(WINDOW_END_NS)
    assert newest["time_unit"] == TIME_FIELD_UNIT
    assert newest["run_time"] != newest["window_end"]
    assert "ts" not in newest, "the unnamed date field is gone"

    stored = rows[0]
    assert stored["run_time"] == "" and stored["run_time_kind"] == ""
    assert stored["run_time_refusal"].startswith(RUN_TIME_UNMEASURED)
    assert stored["window_end"] == "" and stored["window_end_kind"] == ""
    assert stored["window_end_refusal"].startswith(WINDOW_END_UNDECLARED)

    # the read names the newest run's own clock at the top level too
    assert out["run_time"] == _iso_ns(run_time) and out["run_time_kind"] == "run_time"
    assert out["window_end"] == _iso_ns(WINDOW_END_NS)
    assert out["window_end_kind"] == "window_end"

    # the rule, over the whole document: nothing published under a run-time name is the
    # window end, and nothing published under either name is a rendered 1970 reading
    window_iso = _iso_ns(WINDOW_END_NS)
    published_run_times = _values_for_key(out, "run_time")
    assert published_run_times and _iso_ns(run_time) in published_run_times
    assert window_iso not in published_run_times
    for value in published_run_times:
        assert not str(value).startswith("1970-")
    for value in _values_for_key(out, "window_end"):
        assert not str(value).startswith("1970-")


def test_a_mis_scaled_window_end_is_absent_from_the_status_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: a 1970-rendering reading is refused by name, never published as a date."""
    status = _status_module()
    ledger_path = tmp_path / "benchmark_ledger.jsonl"
    stored = _receipt(tmp_path, name="mis.jsonl", computed_at_ns=MISSCALED_NS)
    _write_ledger(ledger_path, [stored.model_copy(update={"window_end_timestamp_ns": MISSCALED_NS})])
    monkeypatch.setattr(status, "LEDGER", ledger_path)

    out = status.read_ledger()
    row = out["history"][-1]
    assert row["window_end"] == ""
    assert row["window_end_kind"] == ""
    assert row["window_end_refusal"].startswith(WINDOW_END_MISSCALED)
    assert row["run_time"] == "" and row["run_time_refusal"].startswith(RUN_TIME_UNMEASURED)
    assert _iso_ns(MISSCALED_NS) not in json.dumps(out)


# --------------------------------------------------------------------------- #
# 5 — a real producer, end to end (no tape required)
# --------------------------------------------------------------------------- #
def test_the_nx08_demo_producer_declares_a_run_time_and_refuses_a_window_end(
    tmp_path: Path,
) -> None:
    """A2: a producer run writes both quantities; the one it did not measure is refused."""
    tool = REPO_ROOT / "tools" / "nx08_gate_manifest.py"
    if not tool.is_file():
        pytest.skip(f"producer absent at {tool}")
    out_dir = tmp_path / "nx08"
    before = time.time_ns()
    completed = subprocess.run(
        [sys.executable, str(tool), "--repo-root", str(REPO_ROOT), "--out", str(out_dir)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    vector = json.loads((out_dir / "scoring_manifest.json").read_text())["time_publication"]

    assert vector["unit"] == TIME_FIELD_UNIT
    assert vector["run_time_kind"] == "run_time"
    assert before <= vector["run_time"] <= time.time_ns() + MAX_CLOCK_SKEW_NS
    # this demo measured no window: it declares none, and says so by name
    assert vector["window_end"] is None
    assert vector["window_end_kind"] == ""
    assert vector["window_end_refusal"].startswith(WINDOW_END_UNDECLARED)


# --------------------------------------------------------------------------- #
# 6 — evaluative: the real ledger (read-only)
# --------------------------------------------------------------------------- #
def test_the_stored_ledger_still_verifies_under_its_own_versions_and_is_not_rewritten() -> None:
    """A1/A3 / F1: 21-ish stored entries, their own digests, byte-identical afterwards."""
    ledger_path = _canonical_ledger()
    if not ledger_path.is_file():
        pytest.skip(f"canonical ledger absent at {ledger_path}")

    before = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    ledger = BenchmarkLedger.load_jsonl(ledger_path)
    report = ledger.verify_report()

    assert len(ledger.entries) >= 21, "the canonical ledger must still carry its entries"
    assert report.overall == "OK", [e.as_dict() for e in report.entries if not e.fully_valid]
    assert report.chain_valid and report.digests_valid and report.artifacts_intact
    for entry in ledger.entries:
        ok, reason = entry.receipt.verify_digest()
        assert ok, reason
        assert entry.receipt.digest_version in RECEIPT_CANON_TABLE

    # F1: not a byte of the stored history moved -- no digest, entry_hash or parent hash
    # was rewritten by this change
    assert hashlib.sha256(ledger_path.read_bytes()).hexdigest() == before


def test_no_stored_entry_publishes_its_window_end_as_a_run_time() -> None:
    """A3 / F4: a run time is published only when it was measured, never as a window end.

    #446's defect was one reading published under two names: the window end the run
    measured, republished as the run's own clock. That -- not "no entry may ever measure a
    run time" -- is what every stored entry must respect, whatever era wrote it. An entry
    that measured no run time says so by name; an entry that did measure one carries that
    clock, and it cannot be the window end it measured.
    """
    ledger_path = _canonical_ledger()
    if not ledger_path.is_file():
        pytest.skip(f"canonical ledger absent at {ledger_path}")

    ledger = BenchmarkLedger.load_jsonl(ledger_path)
    assert ledger.entries, "the canonical ledger must carry entries"
    for entry in ledger.entries:
        receipt = entry.receipt
        times = receipt.time_publication()
        if times.publishes_run_time:
            # measured: the run's own clock, and provably not the window end wearing the
            # run-time name (#446). The value is checked, not the absence of the field.
            assert times.run_time_ns is not None
            assert times.run_time_refusal == ""
            assert times.run_time_ns != receipt.computed_at_timestamp_ns
            assert times.run_time_ns != times.window_end_ns
        else:
            # not measured: named unmeasured, never filled in from the other quantity
            assert times.run_time_ns is None
            assert times.run_time_refusal.startswith(RUN_TIME_UNMEASURED)
        if times.publishes_window_end:
            assert times.window_end_ns == receipt.computed_at_timestamp_ns
        else:
            assert times.window_end_refusal.split(":", 1)[0] in (
                WINDOW_END_UNDECLARED,
                WINDOW_END_MISSCALED,
            )


def test_appending_a_run_grows_the_ledger_and_rewrites_no_stored_line(
    tmp_path: Path,
) -> None:
    """(3c): the producer may append to the canonical ledger, and history keeps its bytes.

    The ledger is the system's chronology authority and it is append-only as *bytes*: a run
    that adds one entry leaves every stored line byte-identical (no key set is re-rendered
    through the current model), no stored identity moves, and the grown ledger still
    verifies under its own versions. The canonical file is read here, never written -- the
    append happens on a copy of those exact bytes.
    """
    ledger_path = _canonical_ledger()
    if not ledger_path.is_file():
        pytest.skip(f"canonical ledger absent at {ledger_path}")

    canonical_bytes = ledger_path.read_bytes()
    stored = BenchmarkLedger.load_jsonl(ledger_path)
    assert len(stored.entries) == len(canonical_bytes.splitlines())
    identities_before = [
        (e.sequence_number, e.entry_hash, e.parent_entry_hash, e.receipt.receipt_digest)
        for e in stored.entries
    ]

    grown_path = tmp_path / "benchmark_ledger.jsonl"
    grown_path.write_bytes(canonical_bytes)
    grown = BenchmarkLedger.load_jsonl(grown_path)
    run_time = time.time_ns()
    receipt = _receipt(
        tmp_path,
        name="append_trades.jsonl",
        computed_at_ns=WINDOW_END_NS,
        window_end_ns=WINDOW_END_NS,
        run_time_ns=run_time,
        case_id="BC-446-APPEND",
    )
    entry = grown.append(receipt)
    grown.save_jsonl(grown_path)

    # (i) the stored history is byte-identical: the appended line is the only difference
    after = grown_path.read_bytes()
    assert after[: len(canonical_bytes)] == canonical_bytes
    assert after[len(canonical_bytes) :] == entry.model_dump_json().encode() + b"\n"
    # (ii) the appended entry is the next link in the chain and it measured its run time
    assert entry.sequence_number == len(stored.entries)
    assert entry.parent_entry_hash == identities_before[-1][1]
    appended_times = entry.receipt.time_publication()
    assert appended_times.publishes_run_time is True
    assert appended_times.run_time_ns == run_time
    # (iii) no stored entry changed its identity
    reloaded = BenchmarkLedger.load_jsonl(grown_path)
    assert [
        (e.sequence_number, e.entry_hash, e.parent_entry_hash, e.receipt.receipt_digest)
        for e in reloaded.entries[: len(identities_before)]
    ] == identities_before
    # (iv) and the grown ledger still verifies in full -- chain, digests and artifacts
    report = reloaded.verify_report()
    assert report.overall == "OK", [e.as_dict() for e in report.entries if not e.fully_valid]
    assert report.chain_valid and report.digests_valid and report.artifacts_intact

    # the canonical file was read, not written: it is not this file's evidence to change
    assert ledger_path.read_bytes() == canonical_bytes

