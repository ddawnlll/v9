"""NX04 (#425) — versioned ledger canonicalization; history is never re-hashed.

Evidence classes:

* **golden (real)** — one entry per supported digest version, copied verbatim from
  the real ``artifacts/benchmarks/benchmark_ledger.jsonl``. Their digests were
  computed by the historical producer revisions of that era, so they are
  independent of this tree's canon table. The fixture files are read-only inputs.
* **mechanics** — single-field tamper, unknown version, unproven canon shape,
  chain-vs-artifact separation, predecessor tamper, and the v5 -> v6 continuity
  of newly written receipts (the window-evidence field, #444).
* **evaluative** — the whole real ledger verifies and its bytes are unchanged by
  verification (read-only proof). Skips when the ledger is absent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from v8_next.evaluation.benchmark_receipt import (
    RECEIPT_CANON_TABLE,
    RECEIPT_DIGEST_VERSION,
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    LedgerEntry,
    ScoreEvidence,
    build_canon_payload,
)

REPO_ROOT = Path("/Users/hootie/src/v8")
LEDGER = REPO_ROOT / "artifacts" / "benchmarks" / "benchmark_ledger.jsonl"
FIXTURES = Path(__file__).parent / "fixtures" / "ledger_canon"
GOLDEN_VERSIONS = ("v8.5-digest-v2", "v8.5-digest-v3", "v8.5-digest-v4")


def _fixture(version: str) -> dict:
    path = FIXTURES / f"{version}.json"
    if not path.is_file():
        pytest.skip(f"golden fixture absent at {path}; run tools/nx04_ledger_fixtures.py")
    return json.loads(path.read_text())


def _receipt_from(fields: dict) -> BenchmarkReceipt:
    return BenchmarkReceipt.model_validate(fields)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# golden — real historical digests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", GOLDEN_VERSIONS)
def test_historical_entries_verify_under_their_own_canon(version: str) -> None:
    """A real stored digest from that era must reproduce under its canon."""
    fixture = _fixture(version)
    assert fixture["digest_version"] == version
    receipt = _receipt_from(fixture["receipt"])
    ok, reason = receipt.verify_digest()
    assert ok, reason
    assert receipt.receipt_digest == fixture["receipt"]["receipt_digest"]


@pytest.mark.parametrize("version", GOLDEN_VERSIONS)
def test_single_field_tamper_on_a_historical_entry_is_detected(version: str) -> None:
    fixture = _fixture(version)
    fields = dict(fixture["receipt"])
    fields["case_id"] = fields["case_id"] + "X"
    ok, reason = _receipt_from(fields).verify_digest()
    assert not ok
    assert reason.startswith("DIGEST_TAMPERED")


def test_canon_table_covers_every_golden_version() -> None:
    for version in GOLDEN_VERSIONS:
        assert version in RECEIPT_CANON_TABLE
        assert RECEIPT_CANON_TABLE[version].provenance.strip()
    assert RECEIPT_DIGEST_VERSION in RECEIPT_CANON_TABLE


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_unknown_version_fails_explicitly_without_guessing() -> None:
    fixture = _fixture("v8.5-digest-v4")
    fields = dict(fixture["receipt"])
    fields["digest_version"] = "v8.5-digest-v99"
    ok, reason = _receipt_from(fields).verify_digest()
    assert not ok
    assert reason.startswith("UNSUPPORTED_DIGEST_VERSION")


def test_unproven_canon_shape_is_refused_not_searched() -> None:
    """A v2 entry with a populated economic digest has no proven layout."""
    fixture = _fixture("v8.5-digest-v2")
    fields = dict(fixture["receipt"])
    fields["economic_evidence_digest"] = "0" * 64
    ok, reason = _receipt_from(fields).verify_digest()
    assert not ok
    assert reason.startswith("CANON_SHAPE_UNPROVEN")


def _receipt(tmp_path: Path, *, name: str = "trades.jsonl") -> BenchmarkReceipt:
    artifact = tmp_path / name
    artifact.write_text('{"trade_id":"t","pnl":1.0}\n')
    from v8_next.evaluation.parity import ArtifactBinding
    from v8_next.evaluation.scoring import (
        compute_capability_breakdown,
        compute_capability_score,
    )

    # MECHANICS ONLY: fixed determinants, so the receipt's published number is
    # *derived* from the evidence it binds (#408). A hand-set score its own evidence
    # cannot produce is no longer constructible, so this fixture measures one.
    measurement: dict = dict(
        pnl_series=[0.01, -0.02, 0.03, 0.005] * 3,
        total_bars=60,
        total_trades=6,
        abstain_rate=0.2,
    )
    evidence = ScoreEvidence.from_breakdown(compute_capability_breakdown(**measurement))
    return BenchmarkReceipt.create(
        case_id="BC-NX04-MECH-01",
        policy_id="pol_nx04",
        capability_score=compute_capability_score(**measurement),
        coverage_factor=evidence.coverage_factor,
        gates=GateVector(g0_identity=GateState.PASS, g1_causal_pit=GateState.UNKNOWN),
        computed_at_timestamp_ns=1_700_000_000_000_000_000,
        artifact_bindings=(ArtifactBinding.from_file("native_trades", artifact),),
        input_binding="input-binding-nx04",
        score_evidence=evidence,
    )


def test_chain_validity_is_reported_separately_from_artifact_availability(
    tmp_path: Path,
) -> None:
    """A sound chain with a missing artifact is ARTIFACTS_INCOMPLETE, never OK."""
    receipt = _receipt(tmp_path)
    ledger = BenchmarkLedger()
    ledger.append(receipt)
    assert ledger.verify_report().overall == "OK"

    (tmp_path / "trades.jsonl").unlink()
    report = ledger.verify_report()
    assert report.chain_valid is True
    assert report.digests_valid is True
    assert report.artifacts_intact is False
    assert report.overall == "ARTIFACTS_INCOMPLETE"
    assert ledger.verify_chain()[0] is False


def test_predecessor_tamper_breaks_the_chain(tmp_path: Path) -> None:
    first = _receipt(tmp_path, name="one.jsonl")
    ledger = BenchmarkLedger()
    ledger.append(first)
    second = _receipt(tmp_path, name="two.jsonl")
    ledger.append(second)
    entries = ledger.entries
    tampered = entries[1].model_copy(update={"parent_entry_hash": "f" * 64})
    rebuilt = BenchmarkLedger([entries[0], tampered])
    report = rebuilt.verify_report()
    assert report.chain_valid is False
    assert report.entries[1].chain == "PARENT_HASH_MISMATCH"
    assert report.overall == "CHAIN_INVALID"


def test_sequence_gap_is_reported(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path)
    entry = LedgerEntry.create(7, BenchmarkLedger.GENESIS_HASH, receipt)
    report = BenchmarkLedger([entry]).verify_report()
    assert report.entries[0].chain == "SEQUENCE_GAP"
    assert report.overall == "CHAIN_INVALID"


def test_new_writes_register_a_version_without_moving_old_digests(tmp_path: Path) -> None:
    """v7 is the registered write version; history stays put.

    v7 is v6's layout plus exactly one trailing field (the determinants of the
    published capability score, #408), so every stored v2-v6 record keeps its
    original bytes, digest and parent hash.
    """
    receipt = _receipt(tmp_path)
    assert receipt.digest_version == RECEIPT_DIGEST_VERSION
    assert RECEIPT_DIGEST_VERSION == "v8.5-digest-v7"
    assert RECEIPT_CANON_TABLE["v8.5-digest-v7"].economic_pair == (
        RECEIPT_CANON_TABLE["v8.5-digest-v6"].economic_pair
    )
    assert RECEIPT_CANON_TABLE["v8.5-digest-v7"].input_binding == (
        RECEIPT_CANON_TABLE["v8.5-digest-v6"].input_binding
    )
    assert RECEIPT_CANON_TABLE["v8.5-digest-v7"].window_evidence == (
        RECEIPT_CANON_TABLE["v8.5-digest-v6"].window_evidence
    )
    # the score-evidence field exists only from v7 on: no historical version grew one
    for version in (
        "v8.5-digest-v2",
        "v8.5-digest-v3",
        "v8.5-digest-v4",
        "v8.5-digest-v5",
        "v8.5-digest-v6",
    ):
        assert RECEIPT_CANON_TABLE[version].score_evidence == "absent"
    assert RECEIPT_CANON_TABLE["v8.5-digest-v7"].score_evidence == "always_present"

    def canon_for(version: str, *, score_evidence: ScoreEvidence | None = None) -> list:
        canon, refusal = build_canon_payload(
            digest_version=version,
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
            score_evidence=score_evidence,
        )
        assert refusal is None
        assert canon is not None
        return canon

    # a version that never carried the field is refused rather than quietly
    # re-interpreted with one (no layout is guessed at verification time)
    _, refusal = build_canon_payload(
        digest_version="v8.5-digest-v6",
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
    assert refusal is not None and refusal.startswith("CANON_SHAPE_UNPROVEN")

    legacy = canon_for("v8.5-digest-v6")
    current = canon_for(RECEIPT_DIGEST_VERSION, score_evidence=receipt.score_evidence)
    # one field more than v6, and nothing else moved
    assert len(current) == len(legacy) + 1
    assert legacy[1] == "v8.5-digest-v6" and current[1] == "v8.5-digest-v7"
    assert legacy[:1] == current[:1] and legacy[2:] == current[2 : len(legacy)]

    # history is never rewritten: every stored record keeps its own digest
    for version in GOLDEN_VERSIONS:
        fixture = _fixture(version)
        stored = fixture["receipt"]["receipt_digest"]
        assert _receipt_from(fixture["receipt"]).receipt_digest == stored
        ok, reason = _receipt_from(fixture["receipt"]).verify_digest()
        assert ok, reason


# --------------------------------------------------------------------------- #
# evaluative — the real ledger
# --------------------------------------------------------------------------- #
def test_real_ledger_verifies_and_is_never_rewritten() -> None:
    if not LEDGER.is_file():
        pytest.skip(f"real ledger absent at {LEDGER}")
    before = _sha256(LEDGER)
    ledger = BenchmarkLedger.load_jsonl(LEDGER)
    report = ledger.verify_report()
    assert ledger.entries, "the real ledger carries no entries"
    assert report.overall == "OK", [e.as_dict() for e in report.entries if not e.fully_valid]
    assert report.chain_valid and report.digests_valid and report.artifacts_intact
    versions = {e.digest_version for e in report.entries}
    assert {"v8.5-digest-v2", "v8.5-digest-v3", "v8.5-digest-v4"} <= versions
    # verification is read-only: the ledger bytes are untouched
    assert _sha256(LEDGER) == before
