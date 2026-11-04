"""#448 — the latest-benchmark reader publishes only from an evidential entry.

The defect: ``cli status`` (and every other reader of the canonical ledger) took
``ledger.entries[-1]`` and published its capability score as *the* latest benchmark.
The newest entry is not a measurement class, so a run whose receipt declares no window
evidence at all -- every stored record before the class existed -- had its number
published as if a window that may mint a score had produced it. The canonical ledger's
seq 20 entry is exactly that shape (digest version ``v8.5-digest-v5``, no
``window_evidence`` key at all) and its 14.0 was being published.

The fix is a classifier on the *declaration* the receipt itself carries:
:meth:`BenchmarkReceipt.declares_evidential_window` -- true only when the receipt binds
a :class:`WindowEvidence` whose ``economic_evidence`` is true -- and a selector
(:meth:`BenchmarkLedger.publication`) that takes the most recent entry declaring it.
With no such entry the reader refuses by name (:data:`NO_EVIDENTIAL_LEDGER_ENTRY`),
quoting the entry and the class it refused, and the number is never printed.

Evidence classes:

* **mechanics** -- the reader rule and its refusals, on MECHANICS ONLY fixtures: no
  assertion here claims performance, minting or authority.
* **evaluative** -- the same reader applied to the *real* canonical ledger
  (``artifacts/benchmarks/benchmark_ledger.jsonl``), which is read-only here: nothing in
  this file writes to it, and the test skips when it is absent.

The ledger is append-only and it grows, so the reader is exercised against whatever is
currently stored -- including a scoreless post-fix entry -- and the refusal is checked
value-wise: a *number* must not be republished, while an entry that publishes no number at
all is a legitimate state, not a leak.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from v8_next.adapters.tape_paths import data_path
from v8_next.app.cli import main as cli_main
from v8_next.app.readiness import (
    GATE_AUDIT_CONTRACT_FIELDS,
    GATE_AUDIT_CONTRACT_MISMATCH,
    GATE_AUDIT_REL,
    audit,
    gate_factor,
)
from v8_next.evaluation.benchmark_receipt import (
    EVIDENCE_CLASS_UNDECLARED,
    NO_EVIDENTIAL_LEDGER_ENTRY,
    NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE,
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    ScoreEvidence,
    WindowEvidence,
)
from v8_next.evaluation.certificate import PolicyCertificate
from v8_next.evaluation.gate_resolution import resolve_structural_gates
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.run_window import WindowSpec
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

#: the canonical ledger is read-only evidence for the evaluative test below. `artifacts/`
#: is git-ignored, so a worktree checkout does not carry it: the primary checkout's copy is
#: the same append-only file, and a worktree reads that one rather than skipping the test.
PRIMARY_CHECKOUT_LEDGER = data_path("artifacts", "benchmarks", "benchmark_ledger.jsonl")


def _canonical_ledger() -> Path:
    """The canonical ledger, from wherever this tree can see it (read-only either way)."""
    candidates = (
        Path(__file__).resolve().parents[2] / "artifacts" / "benchmarks" / "benchmark_ledger.jsonl",
        PRIMARY_CHECKOUT_LEDGER,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


#: the canonical ledger the reader is applied to below
CANONICAL_LEDGER = _canonical_ledger()

#: the repository the readiness reader is pointed at (#458): the committed battery artifact
#: lives under this root and is read-only here -- nothing in this file writes to it.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: MECHANICS ONLY: fixed determinants, so a fixture number is *derived* from evidence the
#: receipt binds (#408). No economic weight is claimed for it and no run is executed.
_MEASUREMENT: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 3, total_bars=60, total_trades=6, abstain_rate=0.2
)
_MEASURED_EVIDENCE = ScoreEvidence.from_breakdown(compute_capability_breakdown(**_MEASUREMENT))
MEASURED_SCORE = compute_capability_score(**_MEASUREMENT)
assert MEASURED_SCORE is not None, "the mechanics fixture must measure a number"

HOUR_MS = 3_600_000


def _published_floats(document: Any) -> list[float]:
    """Every float published as a *value* anywhere in a JSON-ish document (keys excluded).

    #448.A1 asks whether a refused number was republished. The question is value-wise, not
    textual: a scoreless entry (``capability_score is None`` -- the legitimate shape #444 and
    #448 want) has no number to leak, and ``str(None)`` is not a score. Sequence numbers are
    ints, so comparing floats cannot confuse a chain position with a measurement.
    """
    found: list[float] = []
    if isinstance(document, dict):
        for value in document.values():
            found.extend(_published_floats(value))
    elif isinstance(document, (list, tuple)):
        for item in document:
            found.extend(_published_floats(item))
    elif isinstance(document, float):
        found.append(document)
    return found


def _smoke_evidence() -> WindowEvidence:
    window = WindowSpec(tape_path="tape", profile="smoke", bars=500)
    window.validate()
    return WindowEvidence.from_window(window)


def _benchmark_evidence() -> WindowEvidence:
    # #490: a benchmark window that never recorded how many bars it measured is no longer
    # publishable (``EVIDENTIAL_WINDOW_BARS_UNMEASURED``), so this fixture declares the
    # measurement its 500-hour window loads. The assertions below are unchanged; the
    # unmeasured shape is exercised, by name, in test_evidential_window_bars_490.py.
    window = WindowSpec(
        tape_path="tape",
        profile="benchmark",
        start_ms=1_751_328_000_000,
        end_ms=1_751_328_000_000 + 500 * HOUR_MS,
        measured_bars=500,
    )
    window.validate()
    return WindowEvidence.from_window(window)


def _receipt(
    tmp_path: Path,
    *,
    window_evidence: WindowEvidence | None,
    capability_score: float | None,
    name: str = "trades.jsonl",
    case_id: str = "BC-448-READER",
) -> BenchmarkReceipt:
    """A MECHANICS ONLY receipt: real structure, no economic claim attached to it."""
    artifact = tmp_path / name
    artifact.write_text('{"trade_id":"t","pnl":1.0}\n')
    scored = capability_score is not None
    return BenchmarkReceipt.create(
        case_id=case_id,
        policy_id="pol_448",
        capability_score=MEASURED_SCORE if scored else None,
        score_evidence=_MEASURED_EVIDENCE if scored else None,
        coverage_factor=_MEASURED_EVIDENCE.coverage_factor if scored else None,
        gates=GateVector(g0_identity=GateState.PASS, g1_causal_pit=GateState.UNKNOWN),
        computed_at_timestamp_ns=1_700_000_000_000_000_000,
        artifact_bindings=(ArtifactBinding.from_file("native_trades", artifact),),
        input_binding="input-binding-448",
        window_evidence=window_evidence,
    )


def _undeclared_score(tmp_path: Path, name: str = "undeclared.jsonl") -> BenchmarkReceipt:
    """The canonical defect shape: a number with no window class declared at all."""
    receipt = _receipt(tmp_path, window_evidence=None, capability_score=14.0, name=name)
    assert receipt.window_evidence is None
    assert receipt.capability_score is not None
    ok, reason = receipt.verify()
    assert ok, reason
    return receipt


# --------------------------------------------------------------------------- #
# 1 — the canonical ledger: nothing in it may be read as evidential
# --------------------------------------------------------------------------- #
def test_canonical_ledger_returns_no_entry_as_evidential_and_refuses_by_name() -> None:
    """The real ledger read through the real reader (#448.A2).

    Every stored entry that declares no window class must come back *not* evidential --
    the class is never defaulted -- and with no evidential entry the reader must refuse,
    naming the entry it refused (sequence + full ``entry_hash``) and the class it carries.
    """
    if not CANONICAL_LEDGER.is_file():
        pytest.skip(f"canonical ledger absent at {CANONICAL_LEDGER}")

    ledger = BenchmarkLedger.load_jsonl(CANONICAL_LEDGER)
    assert ledger.entries, "the canonical ledger must not be empty"

    for entry in ledger.entries:
        receipt = entry.receipt
        if receipt.window_evidence is None:
            # no class declared => never evidential, and named as undeclared
            assert receipt.declares_evidential_window() is False
            assert receipt.evidence_class() == EVIDENCE_CLASS_UNDECLARED
            if receipt.capability_score is not None:
                refusal = receipt.score_publication_refusal_reason()
                assert refusal is not None
                assert refusal.startswith(EVIDENCE_CLASS_UNDECLARED)
                # A1: the refused number is not repeated by the refusal itself
                assert str(receipt.capability_score) not in refusal

    selected = ledger.latest_evidential_entry()
    if selected is not None:
        # if the class ever arrives, it arrives as a declaration -- never as a default
        assert selected.receipt.window_evidence is not None
        assert selected.receipt.window_evidence.economic_evidence is True
        assert selected.receipt.declares_evidential_window() is True

    publication = ledger.publication()
    newest = ledger.entries[-1]
    if selected is None:
        assert publication.entry is None
        assert publication.publishes_capability_score is False
        assert publication.capability_score is None
        assert publication.refusal_reason.startswith(NO_EVIDENTIAL_LEDGER_ENTRY)
        # the refusal names the entry it refused, by hash and by class
        assert newest.entry_hash in publication.refusal_reason
        assert str(newest.sequence_number) in publication.refusal_reason
        assert publication.evidence_class == newest.receipt.evidence_class()
        assert publication.entry_hash == ""
        assert publication.as_dict()["latest_entry_hash"] == newest.entry_hash
        assert publication.as_dict()["latest_records_capability_score"] is (
            newest.receipt.capability_score is not None
        )
        # A1/A4: the refused number is not written anywhere in the read either -- checked
        # value-wise, not by substring. A scoreless entry carries no number at all (the
        # legitimate state #444/#448 want), so `None` is not a leak; a real number is.
        refused = newest.receipt.capability_score
        assert refused is None or refused not in _published_floats(publication.as_dict())
    else:
        assert publication.entry is not None
        assert publication.publishes_capability_score is True
        assert publication.capability_score == selected.receipt.capability_score


def test_canonical_ledger_is_never_written_by_this_reader() -> None:
    """Reading the ledger is not writing it: byte-identical before and after (#448.A5)."""
    if not CANONICAL_LEDGER.is_file():
        pytest.skip(f"canonical ledger absent at {CANONICAL_LEDGER}")
    import hashlib

    before = hashlib.sha256(CANONICAL_LEDGER.read_bytes()).hexdigest()
    ledger = BenchmarkLedger.load_jsonl(CANONICAL_LEDGER)
    ledger.publication()
    ledger.verify_chain()
    after = hashlib.sha256(CANONICAL_LEDGER.read_bytes()).hexdigest()
    assert before == after


# --------------------------------------------------------------------------- #
# 2 — the rule does not depend on append order
# --------------------------------------------------------------------------- #
def test_an_evidential_entry_is_selected_even_when_a_smoke_entry_follows_it(
    tmp_path: Path,
) -> None:
    """#448.A2(iii) / F4: appending a non-evidential run cannot displace the measurement."""
    evidential = _receipt(
        tmp_path, window_evidence=_benchmark_evidence(), capability_score=14.0, name="bench.jsonl"
    )
    smoke = _receipt(
        tmp_path, window_evidence=_smoke_evidence(), capability_score=None, name="smoke.jsonl"
    )

    ledger = BenchmarkLedger()
    ledger.append(evidential)
    smoke_entry = ledger.append(smoke)
    assert smoke_entry.sequence_number == 1, "the smoke run is the newest entry"

    publication = ledger.publication()
    assert publication.publishes_capability_score is True
    assert publication.entry is not None
    assert publication.entry.sequence_number == 0, "append order must not decide the read"
    assert publication.entry.receipt.receipt_digest == evidential.receipt_digest
    assert publication.entry.receipt.evidence_class() == "benchmark"
    assert publication.as_dict()["latest_entry_hash"] == smoke_entry.entry_hash
    assert publication.capability_score == MEASURED_SCORE
    assert publication.refusal_reason == ""


def test_a_smoke_entry_alone_is_refused_by_class(tmp_path: Path) -> None:
    """A ledger holding only non-evidential runs has no measurement to publish."""
    smoke = _receipt(
        tmp_path, window_evidence=_smoke_evidence(), capability_score=None, name="only.jsonl"
    )
    ledger = BenchmarkLedger()
    entry = ledger.append(smoke)

    publication = ledger.publication()
    assert publication.entry is None
    assert publication.publishes_capability_score is False
    assert publication.capability_score is None
    assert publication.refusal_reason.startswith(NO_EVIDENTIAL_LEDGER_ENTRY)
    assert "smoke" in publication.refusal_reason
    assert entry.entry_hash in publication.refusal_reason
    assert publication.evidence_class == "smoke"
    assert smoke.capability_score is None


def test_a_smoke_receipt_carrying_a_score_is_refused_before_it_can_be_read(
    tmp_path: Path,
) -> None:
    """#444 still holds on this path: the forged entry never reaches the ledger."""
    forged = _receipt(
        tmp_path, window_evidence=_smoke_evidence(), capability_score=14.0, name="forged.jsonl"
    )
    ok, reason = forged.verify()
    assert ok is False
    assert reason.startswith(NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE)
    with pytest.raises(ValueError, match=NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE):
        BenchmarkLedger().append(forged)


# --------------------------------------------------------------------------- #
# 3 — the two commands that publish
# --------------------------------------------------------------------------- #
def _write_ledger(tmp_path: Path, receipts: list[BenchmarkReceipt]) -> Path:
    out_dir = tmp_path / "benchmarks"
    ledger = BenchmarkLedger()
    for receipt in receipts:
        ledger.append(receipt)
    ledger.save_jsonl(out_dir / "benchmark_ledger.jsonl")
    assert ledger.verify_chain() == (True, "OK")
    return out_dir


def test_cli_status_never_prints_the_number_of_a_non_evidential_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The defect, at the command level (#448.A1): the number is refused, not printed."""
    receipt = _undeclared_score(tmp_path)
    out_dir = _write_ledger(tmp_path, [receipt])

    assert cli_main(["status", "--output-dir", str(out_dir)]) == 0
    out = capsys.readouterr().out

    assert "chain: VERIFIED" in out
    assert "capability=REFUSED" in out
    assert EVIDENCE_CLASS_UNDECLARED in out
    assert NO_EVIDENTIAL_LEDGER_ENTRY in out
    assert str(receipt.capability_score) not in out, "the refused number is never published"
    # the dashboard still renders, with the factor absent by rule
    assert "Score: MISSING / 100" in out
    assert "READINESS INDEX: MISSING / 100" in out


def test_cli_status_publishes_the_evidential_entry_it_selected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The evidential path is not weakened: a declared benchmark entry still publishes."""
    evidential = _receipt(
        tmp_path, window_evidence=_benchmark_evidence(), capability_score=14.0, name="bench.jsonl"
    )
    smoke = _receipt(
        tmp_path, window_evidence=_smoke_evidence(), capability_score=None, name="smoke.jsonl"
    )
    out_dir = _write_ledger(tmp_path, [evidential, smoke])

    assert cli_main(["status", "--output-dir", str(out_dir)]) == 0
    out = capsys.readouterr().out

    assert f"capability={MEASURED_SCORE:.1f}" in out
    assert "benchmark" in out
    assert f"Score: {MEASURED_SCORE:.1f} / 100" in out
    assert NO_EVIDENTIAL_LEDGER_ENTRY not in out


def test_readiness_refuses_a_non_evidential_vector_by_name(tmp_path: Path) -> None:
    """#448.A3 / F5: the smoke/liveness vector contributes MISSING/0.0, not a factor."""
    receipt = _undeclared_score(tmp_path)
    ledger = BenchmarkLedger()
    ledger.append(receipt)
    publication = ledger.publication()

    # a repo root with no resolved battery artifact: the receipt leg is the only source
    factors = gate_factor(receipt, tmp_path, publication=publication)
    assert factors["factor"] == 0.0
    assert factors["passed"] == 0
    assert factors["status"] == "MISSING"
    assert factors["states"] == {}, "a non-evidential vector is not published"
    assert factors["refusal"].startswith(NO_EVIDENTIAL_LEDGER_ENTRY)
    assert EVIDENCE_CLASS_UNDECLARED in factors["ledger_read"]
    assert factors["ledger_publication"]["publishes_capability_score"] is False
    assert not (tmp_path / GATE_AUDIT_REL).exists()


def test_readiness_counts_the_vector_of_a_declared_evidential_entry(tmp_path: Path) -> None:
    """The same leg still counts a vector that arrives with an evidential declaration."""
    receipt = _receipt(
        tmp_path, window_evidence=_benchmark_evidence(), capability_score=14.0, name="bench.jsonl"
    )
    ledger = BenchmarkLedger()
    ledger.append(receipt)
    publication = ledger.publication()

    factors = gate_factor(receipt, tmp_path, publication=publication)
    assert factors["status"] == "MEASURED"
    assert factors["states"]["g0_identity"] == "PASS"
    assert factors["passed"] == 1
    assert factors["refusal"] == ""


def test_readiness_refuses_a_class_undeclared_receipt_without_a_ledger_read(
    tmp_path: Path,
) -> None:
    """A caller that hands a receipt in directly still gets the rule, from its declaration."""
    receipt = _undeclared_score(tmp_path)
    factors = gate_factor(receipt, tmp_path)
    assert factors["status"] == "MISSING"
    assert factors["factor"] == 0.0
    assert factors["refusal"].startswith(NO_EVIDENTIAL_LEDGER_ENTRY)
    assert EVIDENCE_CLASS_UNDECLARED in factors["refusal"]


# --------------------------------------------------------------------------- #
# 4 — the certificate carries the class and writes no refused number
# --------------------------------------------------------------------------- #
def test_certificate_withholds_the_refused_number_and_names_the_class(tmp_path: Path) -> None:
    """#448: the dashboard renders, the number does not travel -- in any field."""
    receipt = _undeclared_score(tmp_path)
    ledger = BenchmarkLedger()
    ledger.append(receipt)
    certificate = PolicyCertificate.generate(receipt, publication=ledger.publication())

    assert certificate.research_capability_score is None
    assert certificate.evidence_class == EVIDENCE_CLASS_UNDECLARED
    assert certificate.score_publication.startswith(NO_EVIDENTIAL_LEDGER_ENTRY)
    assert certificate.publication_token == NO_EVIDENTIAL_LEDGER_ENTRY
    derivation = certificate.derivation
    assert derivation is not None
    assert derivation["evidence_class"] == EVIDENCE_CLASS_UNDECLARED
    assert derivation["raw_measurements"]["capability_score"] is None
    assert derivation["raw_measurements"]["records_capability_score"] is True

    rendered = certificate.render_ascii()
    assert "Score: MISSING / 100" in rendered
    assert NO_EVIDENTIAL_LEDGER_ENTRY in rendered
    assert EVIDENCE_CLASS_UNDECLARED in rendered
    assert str(receipt.capability_score) not in rendered


def test_certificate_still_publishes_an_evidential_entry(tmp_path: Path) -> None:
    """The declared path is unchanged: the class travels and the number is published."""
    receipt = _receipt(
        tmp_path, window_evidence=_benchmark_evidence(), capability_score=14.0, name="bench2.jsonl"
    )
    ledger = BenchmarkLedger()
    ledger.append(receipt)
    certificate = PolicyCertificate.generate(receipt, publication=ledger.publication())

    assert certificate.research_capability_score is not None
    assert certificate.evidence_class == "benchmark"
    derivation = certificate.derivation
    assert derivation is not None
    assert derivation["raw_measurements"]["capability_score"] == receipt.capability_score


# --------------------------------------------------------------------------- #
# 5 — the resolved battery is counted only under its own producer contract (#458)
# --------------------------------------------------------------------------- #
def _write_battery_artifact(root: Path, coverage: dict[str, Any]) -> None:
    """MECHANICS ONLY: a battery-shaped artifact at the reader's own path.

    The payload is the artifact's contract, not a measurement: no window is replayed and
    no gate is resolved by writing it.
    """
    path = root / GATE_AUDIT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "audit": "V8.7 readiness audit",
                "gate_coverage": coverage,
                "window": {"start_utc": "2025-01-01", "end_utc": "2025-02-01"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _superseded_coverage() -> dict[str, Any]:
    """The shape the committed battery artifact publishes: a vector written before #447.

    ``passed`` counts four cells, two of them (``g1_causal_pit``, ``g2_determinism_ledger``)
    hard gates -- and there is no ``structural_gates`` block naming what any of the three
    structural cells was measured from.
    """
    return {
        "formula": "gates PASS / ten hard gates, resolved on the same series as capability",
        "passed": 4,
        "pct": 66.7,
        "readiness_status": "HardFailure",
        "total": 6,
        "states": {
            "g0_identity": "PASS",
            "g1_causal_pit": "PASS",
            "g2_determinism_ledger": "PASS",
            "g3_scenario_robustness": "BLOCKED",
            "g4_structural_robustness": "PASS",
            "g5_statistical_credibility": "BLOCKED",
        },
    }


def _contract_coverage() -> dict[str, Any]:
    """The shape ``tools/nx_readiness_audit.py`` writes now: the trio resolved, not declared.

    G0 is measured on the bars and G1/G2 are not measured on this path, so the counted
    cells are ``g0`` + ``g4`` (2) and no PASS stands on an unmeasured input (#447).
    """
    probe = resolve_structural_gates((), BenchmarkLedger()).gates
    return {
        "formula": "gates PASS / ten hard gates, resolved on the same series as capability",
        "passed": 2,
        "pct": 33.3,
        "readiness_status": "HardFailure",
        "total": 6,
        "states": {
            "g0_identity": "PASS",
            "g1_causal_pit": probe.g1_causal_pit.name,
            "g2_determinism_ledger": probe.g2_determinism_ledger.name,
            "g3_scenario_robustness": "BLOCKED",
            "g4_structural_robustness": "PASS",
            "g5_statistical_credibility": "BLOCKED",
        },
        "structural_gates": {
            "note": (
                "#447: g0/g1/g2 are derived by the resolver, not declared; each entry "
                "carries the measured input and the named reason its state carries"
            ),
            "g0_identity": {
                "status": "MEASURED",
                "reason": "SINGLE_INSTRUMENT_GAP_FREE",
                "input": "has_continuous_lineage",
                "measured": True,
            },
            "g1_causal_pit": {
                "status": "UNRUN",
                "reason": "CAUSAL_PIT_NOT_MEASURED_ON_THIS_PATH",
                "input": "is_causal_pit",
                "measured": None,
            },
            "g2_determinism_ledger": {
                "status": "UNRUN",
                "reason": "LEDGER_EMPTY_NO_ENTRIES_TO_VERIFY",
                "entries": 0,
            },
        },
    }


def test_readiness_refuses_a_battery_artifact_that_predates_its_producer_contract(
    tmp_path: Path,
) -> None:
    """#458.a: no cell is counted from a vector its own producer cannot reproduce."""
    _write_battery_artifact(tmp_path, _superseded_coverage())

    factors = gate_factor(None, tmp_path)

    assert factors["status"] == "REFUSED"
    assert factors["factor"] == 0.0
    assert factors["passed"] == 0
    assert factors["states"] == {}, "a vector with no producer contract is not published"
    assert factors["refusal"].startswith(GATE_AUDIT_CONTRACT_MISMATCH)
    assert "structural_gates" in factors["refusal"]
    assert GATE_AUDIT_REL in factors["refusal"]
    assert factors["source"].startswith(GATE_AUDIT_REL)

    # the refusal is not routed around: an evidential ledger entry in hand does not make
    # the reader publish a *different* measurement as the resolved battery's vector
    receipt = _receipt(
        tmp_path, window_evidence=_benchmark_evidence(), capability_score=14.0, name="evid.jsonl"
    )
    ledger = BenchmarkLedger()
    ledger.append(receipt)
    with_entry = gate_factor(receipt, tmp_path, publication=ledger.publication())
    assert with_entry["status"] == "REFUSED"
    assert with_entry["states"] == {}
    assert with_entry["passed"] == 0


def test_readiness_refuses_a_pass_cell_its_own_structure_names_as_unmeasured(
    tmp_path: Path,
) -> None:
    """#458.d: the vector must agree with the structure it is published beside."""
    coverage = _contract_coverage()
    coverage["states"]["g1_causal_pit"] = "PASS"
    coverage["passed"] = 3
    _write_battery_artifact(tmp_path, coverage)

    factors = gate_factor(None, tmp_path)

    assert factors["status"] == "REFUSED"
    assert factors["states"] == {}
    assert "g1_causal_pit = PASS" in factors["refusal"]
    assert "UNRUN" in factors["refusal"]


def test_readiness_counts_a_battery_artifact_that_carries_the_producer_contract(
    tmp_path: Path,
) -> None:
    """#458.b/c: the same reader accepts the vector the current producer stands behind."""
    _write_battery_artifact(tmp_path, _contract_coverage())

    factors = gate_factor(None, tmp_path)

    assert factors["status"] == "MEASURED"
    assert factors["source"] == GATE_AUDIT_REL
    assert factors["passed"] == 2
    assert factors["states"]["g0_identity"] == "PASS"
    assert factors["states"]["g4_structural_robustness"] == "PASS"
    for field in GATE_AUDIT_CONTRACT_FIELDS:
        if factors["states"][field] == "PASS":
            assert (
                _contract_coverage()["structural_gates"][field]["status"] == "MEASURED"
            ), f"{field} may be published as PASS only behind a measurement"

    # the cells are the canonical rule's own output for an unmeasured structural path
    probe = resolve_structural_gates((), BenchmarkLedger()).gates
    assert factors["states"]["g1_causal_pit"] == probe.g1_causal_pit.name
    assert factors["states"]["g2_determinism_ledger"] == probe.g2_determinism_ledger.name
    assert factors["states"]["g1_causal_pit"] != "PASS"
    assert factors["states"]["g2_determinism_ledger"] != "PASS"


def test_the_committed_battery_artifact_is_accounted_for_by_contract() -> None:
    """#458.b/F3: the artifact actually on disk, read through the real reader.

    Committed revision read here: ``docs/evidence/v87/READINESS/readiness_audit.json``,
    sha256 498ce4ee0a4597eab34ef5a0fc66fae854f716b1490f426ee1b26a515448febb (pre-#447). It is
    read-only evidence: this test also documents that the artifact is not written here.
    Whichever revision is on disk, the reader publishes no cell that revision's own
    producer entry does not stand behind.
    """
    artifact_path = REPO_ROOT / GATE_AUDIT_REL
    if not artifact_path.is_file():
        pytest.skip(f"readiness battery artifact absent at {artifact_path}")
    coverage = json.loads(artifact_path.read_text())["gate_coverage"]

    factors = gate_factor(None, REPO_ROOT)

    if "structural_gates" in coverage:
        assert factors["status"] == "MEASURED"
        assert factors["states"] == coverage["states"]
        for field in GATE_AUDIT_CONTRACT_FIELDS:
            if factors["states"].get(field) == "PASS":
                assert coverage["structural_gates"][field]["status"] == "MEASURED"
    else:
        # the superseded revision: the two PASS cells nothing measured are named, and not
        # counted -- its vector is refused by name and no cell travels from it (#458.d)
        assert coverage["states"]["g1_causal_pit"] == "PASS"
        assert coverage["states"]["g2_determinism_ledger"] == "PASS"
        assert factors["status"] == "REFUSED"
        assert factors["states"] == {}
        assert factors["passed"] == 0
        assert factors["source"].startswith(GATE_AUDIT_REL)
        assert factors["refusal"].startswith(GATE_AUDIT_CONTRACT_MISMATCH)

        # the published report names the unpublished battery as the next measurement, not a
        # later requirement: the gate factor is 0.0 because nothing was published
        report = audit(REPO_ROOT, REPO_ROOT / "no-ledger-read.jsonl", None)
        assert report["factors"]["gate_factor"]["status"] == "REFUSED"
        assert report["next_required_measurement"].startswith("gate_battery (REFUSED)")
