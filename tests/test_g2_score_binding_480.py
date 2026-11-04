"""#480 — G2 names the failure it measured instead of blocking on retired claims.

The defect this file pins (issue #480): ``measure_ledger_parity()`` collapsed two
different verdicts into one. ``verify_report()`` keeps the byte history (chain,
digest, bound artifacts) apart from the score-binding claim on purpose, so on the
canonical ledger ``overall == "OK"`` while 21 entries refuse their own published
number. The gate read that combination as ``BLOCKED`` and published
``LEDGER_VERIFICATION_FAILED:OK`` -- naming the component as fine and pinning G2
to BLOCKED forever, because every one of those refusals sits on an entry whose own
declared digest version predates ``score_evidence`` and whose bytes may not be
rewritten (D-164 append-only, D-166 retired seq 20).

Four acceptance conditions are measured here:

1. ``verify_report()`` on the canonical ledger reports ``overall`` and
   ``score_binding`` consistently, and every refusal is named
   ``COMPONENT:<sequence>`` -- never a bare component, never ``...:OK``.
2. ``measure_ledger_parity(canonical_ledger)`` returns ``UNKNOWN`` +
   ``DETERMINISM_RERUN_NOT_PERFORMED`` when the only refusals left are history
   (D-166 seq 20 and the other pre-``score_evidence`` entries), and ``BLOCKED``
   with the named live binding error when one exists.
3. A new evidential run recorded under ``artifacts/benchmarks`` publishes
   ``g2_determinism_ledger != BLOCKED`` (its receipt carries the state the fixed
   resolver measured on the real 24-entry canonical history).
4. ``artifacts/benchmarks/benchmark_ledger.jsonl`` is still the pinned revision:
   sha256 ``f1b824be...`` with unchanged bytes, no re-hash.

Evidence classes: the canonical-ledger legs are golden (real bytes, read-only);
the live-defect and inconsistent-verdict legs are mechanics fixtures with no
evaluative weight. Nothing here asserts an economic claim, a gate PASS or a
threshold change.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from v8_next.evaluation.benchmark_receipt import (
    CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE,
    CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE,
    RECEIPT_CANON_TABLE,
    RECEIPT_DIGEST_VERSION,
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    LedgerEntry,
    LedgerVerificationReport,
    ScoreEvidence,
    build_canon_payload,
)
from v8_next.evaluation.gate_resolution import (
    DETERMINISM_RERUN_NOT_PERFORMED,
    LEDGER_VERDICT_INCONSISTENT,
    LEDGER_VERIFICATION_FAILED,
    measure_ledger_parity,
)
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_LEDGER_REL = "artifacts/benchmarks/benchmark_ledger.jsonl"

#: Acceptance condition 4: the revision the #480 evidence was taken on. A later
#: append is a NEW revision that must move this pin deliberately -- never silently.
PINNED_CANONICAL_SHA256 = "f1b824bec9dad63ed49d9341889ad05a01670e3d1a4a5412cbb3a378a8c7093c"

#: The evidential run this file's condition-3 leg reads, and the receipt committed
#: beside it. The run directory is gitignored (``artifacts/``); the receipt is not.
EVIDENTIAL_RUN_REL = "artifacts/benchmarks/evidence-480"
EVIDENTIAL_SEED_NAME = "benchmark_ledger.seed.jsonl"
EVIDENTIAL_LEDGER_NAME = "benchmark_ledger.jsonl"
EVIDENTIAL_RECEIPT_REL = "docs/evidence/g2-determinism/480/evidential_run_receipt.json"

#: A refusal is ``COMPONENT:<sequence>``: the component says what failed, the
#: sequence says which entry it was measured on (#480). The canonical history only
#: carries the two components below; a live number its own evidence does not produce
#: is named ``CAPABILITY_SCORE_NOT_RECOMPUTABLE``, so the pattern is the family.
_REFUSAL = re.compile(r"^CAPABILITY_SCORE_[A-Z_]+:(\d+)$")

#: MECHANICS ONLY: fixed determinants for the synthetic live-defect receipts below.
#: No economic weight is claimed for these numbers; they exercise adjudication.
DETERMINANTS: dict[str, Any] = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 10,
    total_bars=60,
    total_trades=6,
    abstain_rate=0.2,
)


# --------------------------------------------------------------------------- #
# real canonical data: the ledger is gitignored and shared by all worktrees
# --------------------------------------------------------------------------- #
def _canonical_root() -> Path | None:
    """The checkout holding the canonical ``artifacts/``, if this machine has it."""
    if (REPO_ROOT / CANONICAL_LEDGER_REL).is_file():
        return REPO_ROOT
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    common_path = Path(common)
    root = (
        common_path.parent
        if common_path.is_absolute()
        else (REPO_ROOT / common_path).resolve().parent
    )
    return root if (root / CANONICAL_LEDGER_REL).is_file() else None


def _canonical_root_or_skip() -> Path:
    root = _canonical_root()
    if root is None:
        pytest.skip(f"canonical ledger absent at {CANONICAL_LEDGER_REL}")
    assert root is not None
    return root


def _canonical_path() -> Path:
    return _canonical_root_or_skip() / CANONICAL_LEDGER_REL


def _canonical_ledger() -> BenchmarkLedger:
    return BenchmarkLedger.load_jsonl(_canonical_path())


def _is_pre_score_evidence(digest_version: str) -> bool:
    canon = RECEIPT_CANON_TABLE.get(digest_version)
    return canon is not None and canon.score_evidence != "always_present"


# --------------------------------------------------------------------------- #
# mechanics fixtures: a live (v7) defect, and a report that disagrees with itself
# --------------------------------------------------------------------------- #
def _gates() -> GateVector:
    return GateVector(g0_identity=GateState.PASS, g1_causal_pit=GateState.UNKNOWN)


def _forged(capability_score: float | None, evidence: ScoreEvidence | None) -> BenchmarkReceipt:
    """A v7 receipt whose digest MATCHES the number it publishes, and nothing else.

    This is the manufacturing path of the live defect #480 must still block: the
    digest is honest about the bytes it was handed, so only the claim check sees it.
    """
    case_id, policy_id, timestamp = "BC-480-MECH", "pol_480", 1_700_000_000_000_000_000
    canon, refusal = build_canon_payload(
        digest_version=RECEIPT_DIGEST_VERSION,
        case_id=case_id,
        policy_id=policy_id,
        capability_score=capability_score,
        coverage_factor=None if evidence is None else evidence.coverage_factor,
        gates=_gates(),
        artifact_bindings=(),
        computed_at_timestamp_ns=timestamp,
        economic_evidence_digest="",
        economic_receipt_path="",
        input_binding="input-binding-480",
        window_evidence=None,
        score_evidence=evidence,
    )
    assert refusal is None, refusal
    assert canon is not None
    digest = hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()
    return BenchmarkReceipt(
        case_id=case_id,
        policy_id=policy_id,
        digest_version=RECEIPT_DIGEST_VERSION,
        capability_score=capability_score,
        coverage_factor=None if evidence is None else evidence.coverage_factor,
        gates=_gates(),
        artifact_bindings=(),
        computed_at_timestamp_ns=timestamp,
        receipt_digest=digest,
        input_binding="input-binding-480",
        score_evidence=evidence,
    )


def _ledger_of(*receipts: BenchmarkReceipt) -> BenchmarkLedger:
    parent = BenchmarkLedger.GENESIS_HASH
    entries = []
    for sequence, receipt in enumerate(receipts):
        entry = LedgerEntry.create(sequence, parent, receipt)
        entries.append(entry)
        parent = entry.entry_hash
    return BenchmarkLedger(entries)


def _measurement() -> tuple[float | None, ScoreEvidence]:
    breakdown = compute_capability_breakdown(**DETERMINANTS)
    score = compute_capability_score(**DETERMINANTS)
    return score, ScoreEvidence.from_breakdown(breakdown)


# --------------------------------------------------------------------------- #
# 1 -- the report's two verdicts are consistent, and every refusal is named
# --------------------------------------------------------------------------- #
def test_canonical_report_keeps_overall_and_score_binding_consistent() -> None:
    ledger = _canonical_ledger()
    report = ledger.verify_report()
    live = report.live_score_binding_failures
    historical = report.historical_score_binding_failures

    # the verdicts describe the same ledger: a live refusal IS the invalidation,
    # and with the byte history intact its absence leaves the ledger usable
    assert report.overall in {"OK", "SCORE_BINDING_INVALID"}, report.overall
    assert (report.overall == "SCORE_BINDING_INVALID") is bool(live)
    assert report.score_bindings_valid is (not live)
    assert report.score_binding_status == (
        "INVALID" if live else "HISTORICAL_ONLY" if historical else "OK"
    )

    # the canonical revision: byte history holds, refusals are history, not defects
    assert report.chain_valid and report.digests_valid and report.artifacts_intact
    assert report.overall == "OK"
    assert live == ()
    assert report.score_binding_status == "HISTORICAL_ONLY"
    assert len(historical) == 21

    # every refusal names the component AND the sequence it was measured on, and no
    # verdict may ever publish ":OK" as the failing component (#480)
    for verdict in report.score_binding_failures:
        assert not verdict.score_binding.endswith(":OK"), verdict
        match = _REFUSAL.match(verdict.score_binding_reason)
        assert match is not None, verdict.score_binding_reason
        assert int(match.group(1)) == verdict.sequence_number, verdict
        assert verdict.score_binding_detail, "a refusal must carry its detail"
    assert not report.score_binding.endswith(":OK"), report.score_binding
    assert _REFUSAL.match(report.score_binding) is not None, report.score_binding
    assert report.score_binding == report.score_binding_failures[0].score_binding_reason

    # the named refusals are the D-166-retired and pre-score_evidence entries
    contradicted = sorted(
        verdict.sequence_number
        for verdict in historical
        if verdict.score_binding.startswith(CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE)
    )
    assert contradicted == [15, 16]
    retired = [verdict for verdict in historical if verdict.sequence_number == 20]
    assert len(retired) == 1, "D-166 retired seq 20 must still be named"
    assert _is_pre_score_evidence(retired[0].digest_version)


# --------------------------------------------------------------------------- #
# 2 -- history does not block; a live defect does, by name
# --------------------------------------------------------------------------- #
def test_canonical_parity_is_unknown_not_blocked_by_retired_claims() -> None:
    state, metrics = measure_ledger_parity(_canonical_ledger())

    assert state is GateState.UNKNOWN
    assert metrics["status"] == "UNRUN"
    assert metrics["reason"] == DETERMINISM_RERUN_NOT_PERFORMED
    # the defect #480 removes: a reason that names its own failing component as fine
    assert metrics["reason"] != f"{LEDGER_VERIFICATION_FAILED}:OK"
    assert not metrics["reason"].endswith(":OK"), metrics["reason"]
    assert not metrics["reason"].startswith(LEDGER_VERIFICATION_FAILED), metrics["reason"]
    assert "blocked_component" not in metrics and "blocked_sequence" not in metrics

    assert metrics["ledger_overall"] == "OK"
    assert metrics["score_bindings_valid"] is True
    assert metrics["live_score_binding_defects"] == []

    historical = metrics["historical_score_binding_defects"]
    assert len(historical) == 21
    for defect in historical:
        assert defect["live"] is False
        assert defect["reason"] == f"{defect['component']}:{defect['sequence']}"
        assert _REFUSAL.match(defect["reason"]) is not None, defect
        assert _is_pre_score_evidence(defect["digest_version"]), defect
        assert not defect["reason"].endswith(":OK")
        # the history is named in the metrics, never promoted into the state's reason
        assert defect["reason"] not in str(metrics["reason"])

    by_sequence = {defect["sequence"]: defect for defect in historical}
    assert by_sequence[20]["component"] == CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE
    assert by_sequence[20]["digest_version"] == "v8.5-digest-v5"
    assert by_sequence[15]["component"] == CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE
    assert by_sequence[16]["component"] == CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE


def test_a_live_binding_defect_blocks_with_the_named_component_and_sequence() -> None:
    """MECHANICS ONLY: a v7 entry whose number its own evidence does not carry."""
    score, evidence = _measurement()
    assert score is not None

    # (a) a v7 number published with no score evidence at all
    unbound = _ledger_of(_forged(score, None))
    unbound_report = unbound.verify_report()
    assert unbound_report.overall == "SCORE_BINDING_INVALID"
    assert unbound_report.score_binding == f"{CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE}:0"
    assert unbound_report.score_binding_status == "INVALID"
    state, metrics = measure_ledger_parity(unbound)
    assert state is GateState.BLOCKED
    assert metrics["reason"] == f"{CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE}:0"
    assert metrics["blocked_component"] == CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE
    assert metrics["blocked_sequence"] == 0
    assert metrics["live_score_binding_defects"][0]["reason"] == metrics["reason"]
    assert not metrics["reason"].endswith(":OK")

    # (b) two v7 entries bound to identical evidence publishing different numbers
    contradicted = _ledger_of(_forged(score, None), _forged(score + 4.9, None))
    contradicted_report = contradicted.verify_report()
    assert contradicted_report.overall == "SCORE_BINDING_INVALID"
    assert contradicted_report.score_binding == (
        f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}:0"
    )
    assert contradicted_report.live_score_binding_reasons == (
        f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}:0",
        f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}:1",
    )
    state, metrics = measure_ledger_parity(contradicted)
    assert state is GateState.BLOCKED
    assert metrics["reason"] == f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}:0"
    assert metrics["blocked_sequence"] == 0
    assert {defect["reason"] for defect in metrics["live_score_binding_defects"]} == {
        f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}:0",
        f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}:1",
    }

    # (c) a live defect that is also repairable evidence-wise is still a claim defect:
    # evidence bound but the number it does not produce
    wrong_number = _ledger_of(_forged(score + 1.0, evidence))
    state, metrics = measure_ledger_parity(wrong_number)
    assert state is GateState.BLOCKED
    assert _REFUSAL.match(metrics["reason"]) is not None, metrics["reason"]
    assert metrics["reason"].endswith(f":{metrics['blocked_sequence']}")


def test_a_report_that_disagrees_with_its_own_components_never_publishes_ok() -> None:
    """#480: ``LEDGER_VERIFICATION_FAILED:<component>`` may never be ``:OK``.

    The resolver publishes the disagreement itself when a report says ``OK`` while a
    component it is derived from is broken, so the ``:OK`` formatting is unreachable
    even if a producer ever returned such a report.
    """
    ledger = _canonical_ledger()
    healthy = ledger.verify_report().entries
    broken = dataclasses.replace(
        healthy[0], chain="ENTRY_HASH_TAMPERED", digest="OK", artifacts="NO_BINDINGS"
    )
    inconsistent = LedgerVerificationReport(
        entries=(broken, *healthy[1:]),
        overall="OK",
        score_binding="OK",
    )
    assert inconsistent.chain_valid is False

    class _ContradictingLedger:
        """The shape a future producer must not be able to turn into ``...:OK``."""

        entries = ledger.entries

        @staticmethod
        def verify_report() -> LedgerVerificationReport:
            return inconsistent

    state, metrics = measure_ledger_parity(_ContradictingLedger())  # type: ignore[arg-type]
    assert state is GateState.BLOCKED
    assert metrics["reason"] == f"{LEDGER_VERIFICATION_FAILED}:{LEDGER_VERDICT_INCONSISTENT}"
    assert not metrics["reason"].endswith(":OK")


def test_a_tampered_byte_history_blocks_under_a_named_component() -> None:
    """A byte failure still blocks, and the component it names is a failure."""
    raw = _canonical_path().read_bytes()
    lines = raw.splitlines(keepends=True)
    # drop one entry from a copy: the chain's sequence numbering can no longer hold
    tampered = b"".join(lines[:10] + lines[11:])
    ledger = BenchmarkLedger.load_jsonl(_write_tmp(tampered))

    state, metrics = measure_ledger_parity(ledger)
    assert state is GateState.BLOCKED
    assert metrics["reason"].startswith(f"{LEDGER_VERIFICATION_FAILED}:")
    suffix = metrics["reason"].split(":", 1)[1]
    assert suffix != "OK", metrics["reason"]
    assert suffix in {"CHAIN_INVALID", "DIGEST_TAMPERED", "DIGEST_CANON_UNPROVEN", "ARTIFACTS_INCOMPLETE"}


def _write_tmp(payload: bytes) -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        prefix="v8-480-tampered-", suffix=".jsonl", delete=False
    )
    handle.write(payload)
    handle.close()
    return Path(handle.name)


# --------------------------------------------------------------------------- #
# 3 -- the pinned canonical bytes are unchanged
# --------------------------------------------------------------------------- #
def test_pinned_canonical_ledger_bytes_are_unchanged() -> None:
    raw = _canonical_path().read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINNED_CANONICAL_SHA256
    assert len([line for line in raw.splitlines() if line.strip()]) == 24


# --------------------------------------------------------------------------- #
# 4 -- a new evidential run records g2_determinism_ledger != BLOCKED
# --------------------------------------------------------------------------- #
def test_the_committed_evidential_receipt_records_a_non_blocked_g2() -> None:
    receipt_path = REPO_ROOT / EVIDENTIAL_RECEIPT_REL
    if not receipt_path.is_file():
        pytest.skip(f"evidential receipt absent at {EVIDENTIAL_RECEIPT_REL}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    run = receipt["evidential_run"]
    assert run["window"]["evidence_class"] == "benchmark"
    assert run["window"]["economic_evidence"] is True
    assert run["appended_entry"]["window_evidence"]["economic_evidence"] is True

    cell = run["recorded_g2_cell"]
    assert cell["state"] != "BLOCKED"
    assert cell["state"] in {"UNKNOWN", "PASS"}
    assert cell["reason"] != f"{LEDGER_VERIFICATION_FAILED}:OK"
    assert not str(cell["reason"]).endswith(":OK")
    assert cell["measured_on"]["entries"] == 24

    # the run measured the real canonical revision, byte-identically
    assert receipt["canonical_ledger"]["sha256"] == PINNED_CANONICAL_SHA256
    assert run["seed_ledger"]["sha256"] == PINNED_CANONICAL_SHA256
    assert run["seed_ledger"]["bytes_preserved"] is True
    assert receipt["checks"]["all_conditions_hold"] is True


def test_the_evidential_run_artifact_holds_the_state_the_resolver_measures() -> None:
    root = _canonical_root_or_skip()
    run_dir = root / EVIDENTIAL_RUN_REL
    seed = run_dir / EVIDENTIAL_SEED_NAME
    ledger_path = run_dir / EVIDENTIAL_LEDGER_NAME
    if not (seed.is_file() and ledger_path.is_file()):
        pytest.skip(f"evidential run absent under {EVIDENTIAL_RUN_REL}")

    canonical = (root / CANONICAL_LEDGER_REL).read_bytes()
    seed_bytes = seed.read_bytes()
    assert hashlib.sha256(seed_bytes).hexdigest() == PINNED_CANONICAL_SHA256
    assert seed_bytes == canonical, "the seed is the canonical revision, byte for byte"

    after = ledger_path.read_bytes()
    assert after.startswith(seed_bytes), "the canonical entries were appended to, never rewritten"
    lines = [line for line in after.splitlines() if line.strip()]
    assert len(lines) == 25
    appended = json.loads(lines[-1])
    assert appended["sequence_number"] == 24
    assert appended["parent_entry_hash"] == json.loads(lines[-2])["entry_hash"]

    receipt = appended["receipt"]
    assert receipt["window_evidence"]["economic_evidence"] is True
    assert receipt["gates"]["g2_determinism_ledger"] != "BLOCKED"
    assert receipt["gates"]["g2_determinism_ledger"] == "UNKNOWN"
    assert receipt["capability_score"] is not None
    assert receipt["score_evidence"] is not None

    # the state the run recorded is the state the fixed resolver measures on the same
    # history: UNKNOWN because only pre-score_evidence refusals remain, never BLOCKED
    state, metrics = measure_ledger_parity(BenchmarkLedger.load_jsonl(seed))
    assert state.name == receipt["gates"]["g2_determinism_ledger"]
    assert state is GateState.UNKNOWN
    assert metrics["reason"] == DETERMINISM_RERUN_NOT_PERFORMED
    assert metrics["live_score_binding_defects"] == []
    assert len(metrics["historical_score_binding_defects"]) == 21

    # and the run's own ledger is a clean revision: the append neither invents nor
    # inherits a live defect
    state_after, metrics_after = measure_ledger_parity(BenchmarkLedger.load_jsonl(ledger_path))
    assert state_after is GateState.UNKNOWN
    assert metrics_after["reason"] == DETERMINISM_RERUN_NOT_PERFORMED
    assert metrics_after["live_score_binding_defects"] == []
