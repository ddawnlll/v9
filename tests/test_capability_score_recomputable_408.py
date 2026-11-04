"""#408 — a published capability score must be a function of the evidence the receipt binds.

The defect this file pins (issue #408): the canonical ledger publishes two
receipts bound to byte-identical evidence with *different* headline numbers
(seq 15 = 6.2, seq 16 = 11.1) and ``verify()`` certified both.

Evidence classes:

* **mechanics** — the same bound evidence publishes the same number; a number the
  bound evidence does not support is refused at the source (:meth:`create`), and a
  receipt forged with a *matching* digest for such a number is named and failed by
  ``verify()``. Synthetic fixtures here carry no evaluative weight.
* **golden (real)** — the canonical pair (seq 15/16), copied verbatim out of
  ``artifacts/benchmarks/benchmark_ledger.jsonl``, plus the live ledger when it is
  present on this machine. Under the new rule at least one of the pair fails
  (the pre-fix tree certified both — the RED baseline); the bytes, the chain and
  the older digest versions are not invalidated.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from v8_next.evaluation.benchmark_receipt import (
    CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE,
    CAPABILITY_SCORE_NOT_RECOMPUTABLE,
    CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE,
    RECEIPT_DIGEST_VERSION,
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    LedgerEntry,
    ScoreEvidence,
    bound_evidence_fingerprint,
    build_canon_payload,
)
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

REPO_ROOT = Path("/Users/hootie/src/v8")
LEDGER = REPO_ROOT / "artifacts" / "benchmarks" / "benchmark_ledger.jsonl"
FIXTURES = Path(__file__).parent / "fixtures" / "ledger_canon"
PAIR_FIXTURE = FIXTURES / "seq-15-16-bound-evidence-pair.json"
PAIR_SEQUENCES = (15, 16)

#: MECHANICS ONLY: fixed determinants for a synthetic measurement. No economic
#: weight is claimed for these numbers; they exercise arithmetic and adjudication.
DETERMINANTS: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 10,
    total_bars=60,
    total_trades=6,
    abstain_rate=0.2,
)


def _measurement() -> tuple[float | None, ScoreEvidence]:
    """The one measurement this file reuses: (published number, bound evidence)."""
    breakdown = compute_capability_breakdown(**DETERMINANTS)  # type: ignore[arg-type]
    score = compute_capability_score(**DETERMINANTS)  # type: ignore[arg-type]
    return score, ScoreEvidence.from_breakdown(breakdown)


def _binding(tmp_path: Path, name: str = "trades.jsonl") -> ArtifactBinding:
    artifact = tmp_path / name
    artifact.write_text('{"trade_id":"t","pnl":1.0}\n')
    return ArtifactBinding.from_file("native_trades", artifact)


def _gates() -> GateVector:
    return GateVector(g0_identity=GateState.PASS, g1_causal_pit=GateState.UNKNOWN)


def _forged(payload_score: float | None, evidence: ScoreEvidence | None) -> BenchmarkReceipt:
    """A receipt whose digest MATCHES the number it publishes, and nothing else.

    This is the exact manufacturing path of the defect: the digest is honest about
    the bytes it was handed, so a digest-only verdict can never catch it.
    """
    case_id, policy_id, timestamp = "BC-408-MECH", "pol_408", 1_700_000_000_000_000_000
    canon, refusal = build_canon_payload(
        digest_version=RECEIPT_DIGEST_VERSION,
        case_id=case_id,
        policy_id=policy_id,
        capability_score=payload_score,
        coverage_factor=None if evidence is None else evidence.coverage_factor,
        gates=_gates(),
        artifact_bindings=(),
        computed_at_timestamp_ns=timestamp,
        economic_evidence_digest="",
        economic_receipt_path="",
        input_binding="input-binding-408",
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
        capability_score=payload_score,
        coverage_factor=None if evidence is None else evidence.coverage_factor,
        gates=_gates(),
        artifact_bindings=(),
        computed_at_timestamp_ns=timestamp,
        receipt_digest=digest,
        input_binding="input-binding-408",
        score_evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# mechanics — the number follows from the bound evidence
# --------------------------------------------------------------------------- #
def test_same_bound_evidence_publishes_the_same_number(tmp_path: Path) -> None:
    """Two receipts over one bound evidence carry one number and one digest."""
    score, evidence = _measurement()
    assert score is not None

    def build(capability_score: float | None) -> BenchmarkReceipt:
        return BenchmarkReceipt.create(
            case_id="BC-408-MECH",
            policy_id="pol_408",
            capability_score=capability_score,
            gates=_gates(),
            computed_at_timestamp_ns=1_700_000_000_000_000_000,
            artifact_bindings=(_binding(tmp_path),),
            input_binding="input-binding-408",
            score_evidence=evidence,
        )

    first = build(score)
    second = build(score)

    assert first.capability_score == second.capability_score == score
    assert first.receipt_digest == second.receipt_digest
    assert first.verify() == (True, "OK")

    # and the number is the one the bound evidence produces, recomputed here from
    # the receipt's own record rather than read back from the receipt
    from v8_next.evaluation.scoring import recompute_capability_score

    assert first.score_evidence is not None
    assert recompute_capability_score(first.score_evidence) == score

    # a receipt may also publish no number at all (the pre-#444-NX08 case of a
    # missing measurement, and a window class that may not mint a score): evidence
    # without a claim is not a claim, and it is not silently turned into one
    unreported = build(None)
    assert unreported.capability_score is None
    assert unreported.score_evidence is not None


def test_create_refuses_a_number_its_own_evidence_does_not_support(tmp_path: Path) -> None:
    """The seq-15/16 shape is unconstructible for new writes, in both variants."""
    score, evidence = _measurement()
    assert score is not None

    for candidate in (score + 0.1, score + 4.9, 6.2, 11.1):
        if candidate == score:
            continue
        with pytest.raises(ValueError, match=CAPABILITY_SCORE_NOT_RECOMPUTABLE):
            BenchmarkReceipt.create(
                case_id="BC-408-MECH",
                policy_id="pol_408",
                capability_score=candidate,
                gates=_gates(),
                computed_at_timestamp_ns=1_700_000_000_000_000_000,
                artifact_bindings=(_binding(tmp_path),),
                input_binding="input-binding-408",
                score_evidence=evidence,
            )

    # and a number with no evidence bound at all cannot be minted either
    with pytest.raises(ValueError, match=CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE):
        BenchmarkReceipt.create(
            case_id="BC-408-MECH",
            policy_id="pol_408",
            capability_score=42.0,
            gates=_gates(),
            computed_at_timestamp_ns=1_700_000_000_000_000_000,
            artifact_bindings=(_binding(tmp_path),),
            input_binding="input-binding-408",
        )


def test_verify_names_a_number_the_bound_evidence_does_not_support() -> None:
    """A self-consistent forgery is still not evidence: the claim is refused by name."""
    score, evidence = _measurement()
    assert score is not None

    forged = _forged(score + 3.3, evidence)
    assert forged.verify_digest() == (True, "OK")  # the bytes are the bytes ...
    ok, reason = forged.verify()
    assert ok is False  # ... and the claim still does not hold
    assert reason.startswith(CAPABILITY_SCORE_NOT_RECOMPUTABLE)
    assert repr(score + 3.3) in reason and repr(score) in reason

    # a tampered stored number is named too (the evidence is what it must follow)
    lowered = forged.model_copy(update={"capability_score": score - 1.0})
    ok, reason = lowered.verify()
    assert ok is False and CAPABILITY_SCORE_NOT_RECOMPUTABLE in reason

    # evidence that declares no measured basis cannot back a number
    empty = ScoreEvidence(
        total_bars=0,
        total_trades=0,
        abstain_rate=0.0,
        coverage_factor=None,
        aggregate_status="MISSING_NO_TRADES",
    )
    unsupported = _forged(1.0, empty)
    ok, reason = unsupported.verify()
    assert ok is False and reason.startswith(CAPABILITY_SCORE_NOT_RECOMPUTABLE)


def test_verify_names_a_number_with_no_evidence_bound() -> None:
    """A v7 receipt that publishes a number without evidence is refused by name."""
    unbound = _forged(6.2, None)
    assert unbound.verify_digest() == (True, "OK")
    ok, reason = unbound.verify()
    assert ok is False
    assert reason.startswith(CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE)
    assert "6.2" in reason


def test_ledger_appends_a_bound_receipt_and_refuses_an_unbound_one(tmp_path: Path) -> None:
    """The append path is where a published number becomes a ledger entry."""
    score, evidence = _measurement()
    assert score is not None
    bound = BenchmarkReceipt.create(
        case_id="BC-408-MECH",
        policy_id="pol_408",
        capability_score=score,
        gates=_gates(),
        computed_at_timestamp_ns=1_700_000_000_000_000_000,
        artifact_bindings=(_binding(tmp_path),),
        input_binding="input-binding-408",
        score_evidence=evidence,
    )
    ledger = BenchmarkLedger()
    entry = ledger.append(bound)
    assert entry.receipt.capability_score == score
    assert ledger.verify_chain() == (True, "OK")

    with pytest.raises(ValueError, match=CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE):
        BenchmarkLedger().append(_forged(6.2, None))


# --------------------------------------------------------------------------- #
# golden (real) — the canonical pair carries identical evidence, two numbers
# --------------------------------------------------------------------------- #
def _pair_entries() -> tuple[dict, dict]:
    if not PAIR_FIXTURE.is_file():
        pytest.skip(f"canonical pair fixture absent at {PAIR_FIXTURE}")
    payload = json.loads(PAIR_FIXTURE.read_text())
    entries = {entry["sequence_number"]: entry for entry in payload["entries"]}
    return entries[PAIR_SEQUENCES[0]], entries[PAIR_SEQUENCES[1]]


def _rebuilt_ledger(*entries: dict) -> BenchmarkLedger:
    """The verbatim receipts, re-framed as a 2-entry chain (entry bytes untouched).

    ``BenchmarkLedger.append`` is the *minting* gate and refuses these receipts (their
    published numbers predate the evidence record), which is the point: the claim is
    refused while the entry bytes and the chain stay exactly as they were written.
    """
    framed: list[LedgerEntry] = []
    parent = BenchmarkLedger.GENESIS_HASH
    for sequence, entry in enumerate(entries):
        framed.append(
            LedgerEntry.create(sequence, parent, BenchmarkReceipt.model_validate(entry["receipt"]))
        )
        parent = framed[-1].entry_hash
    return BenchmarkLedger(framed)


def test_canonical_pair_publishes_two_numbers_over_one_bound_evidence() -> None:
    """The defect itself, on the real bytes: identical evidence, two numbers."""
    first, second = _pair_entries()
    r15 = BenchmarkReceipt.model_validate(first["receipt"])
    r16 = BenchmarkReceipt.model_validate(second["receipt"])

    assert r15.capability_score is not None and r16.capability_score is not None
    assert r15.capability_score != r16.capability_score
    assert bound_evidence_fingerprint(r15) == bound_evidence_fingerprint(r16)

    # their own versions keep certifying their own bytes: nothing was re-hashed and
    # no stored byte was rewritten to make the new rule fit
    assert r15.verify_digest() == (True, "OK")
    assert r16.verify_digest() == (True, "OK")

    # and neither number is carried by the evidence those bytes bind
    for receipt in (r15, r16):
        ok, reason = receipt.verify()
        assert ok is False
        assert reason.startswith(CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE)

    ledger = _rebuilt_ledger(first, second)
    report = ledger.verify_report()
    assert report.chain_valid is True
    assert report.digests_valid is True
    assert report.score_binding != "OK"
    flagged = [e for e in report.entries if e.score_binding == CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE]
    assert flagged, [e.as_dict() for e in report.entries]
    for entry in flagged:
        assert str(r15.capability_score) in entry.score_binding_detail
        assert str(r16.capability_score) in entry.score_binding_detail


def test_live_ledger_flags_the_defect_without_invalidating_the_chain() -> None:
    """The canonical ledger on disk: only the seq-15/16 pair contradicts the rule."""
    if not LEDGER.is_file():
        pytest.skip(f"canonical ledger absent at {LEDGER}")
    ledger = BenchmarkLedger.load_jsonl(LEDGER)
    report = ledger.verify_report()
    flagged = {e.sequence_number for e in report.entries if e.score_binding == CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}

    assert set(PAIR_SEQUENCES) <= flagged, [
        (e.sequence_number, e.score_binding, e.score_binding_detail) for e in report.entries
    ]
    # every other entry in the canonical ledger already satisfies the rule: the
    # rule is not a retro-invalidation of the tail, it names the one contradiction
    assert flagged == set(PAIR_SEQUENCES)
    assert report.chain_valid is True
    assert report.digests_valid is True
    for sequence in PAIR_SEQUENCES:
        receipt = ledger.entries[sequence].receipt
        assert receipt.capability_score is not None
