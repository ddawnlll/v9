"""#490 — an evidential capability score carries the sample size it was measured on.

The defect: a UTC-bounded window declares no bar count (``WindowSpec.bars`` is the
*smoke shortcut*, refused for evidential profiles), and the receipt copied that
``None`` verbatim. So the record that minted a capability score from 168 loaded bars
said ``window_evidence.bars = null`` -- a number with no sample size bound into the
digest that certifies it, and no reason for a reader to distrust it.

Evidence classes:

* **mechanics** -- the carry rule (:attr:`WindowSpec.carried_bars`), the digest
  placement of ``bars`` (inside ``WindowEvidence.canon_fields``, nothing else), and the
  publication predicate with its named refusal. No economic claim is made here.
* **evaluative** -- one real-tape run of the portfolio path over a 7-day UTC window:
  the ledger entry it appends carries the 168 bars it actually loaded, and the received
  digest recomputes from a canon whose only changed element is that count.

What this file deliberately does *not* do: it never invents a bar count (an unloaded
window keeps ``None``), never coerces a missing count to ``0``, never rewrites a stored
ledger byte (D-164: a pre-#490 record still certifies, it is only refused publication),
and never asserts a minimum evidential length -- that is an owner decision (OPEN_PIN).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from v8_next.evaluation.benchmark_receipt import (
    EVIDENTIAL_WINDOW_BARS_UNMEASURED,
    RECEIPT_DIGEST_VERSION,
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    ScoreEvidence,
    WindowEvidence,
    bound_evidence_fingerprint,
    build_canon_payload,
)
from v8_next.evaluation.certificate import PolicyCertificate
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.run_window import WindowSpec
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

HOUR_MS = 3_600_000
TAPE_DIR = Path(__file__).resolve().parents[2] / "research/tape/quad-1h-12m"
TAPE_FILE = TAPE_DIR / "tape.jsonl"

#: The window the issue was filed on: one 7-day UTC slice of the four-instrument tape.
SEVEN_DAY_START_MS = 1_751_328_000_000  # 2025-07-01T00:00:00Z
SEVEN_DAY_END_MS = SEVEN_DAY_START_MS + 7 * 24 * HOUR_MS  # 2025-07-08T00:00:00Z
MEASURED_BARS = 168  # 7 days x 24 hourly bars

#: MECHANICS ONLY: fixed determinants, so the fixture number is *derived* from evidence
#: the receipt binds (#408). No economic weight is claimed for it and no run is executed.
_MEASUREMENT: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 3, total_bars=60, total_trades=6, abstain_rate=0.2
)
_SCORE_EVIDENCE = ScoreEvidence.from_breakdown(compute_capability_breakdown(**_MEASUREMENT))
MEASURED_SCORE = compute_capability_score(**_MEASUREMENT)
assert MEASURED_SCORE is not None, "the mechanics fixture must measure a number"


def _bounded_window(*, measured_bars: int | None = MEASURED_BARS) -> WindowSpec:
    """The #490 shape: a UTC-bounded evidential window, optionally with its measurement."""
    window = WindowSpec(
        tape_path="tape",
        profile="benchmark",
        start_ms=SEVEN_DAY_START_MS,
        end_ms=SEVEN_DAY_END_MS,
        measured_bars=measured_bars,
    )
    window.validate()
    return window


def _receipt(tmp_path: Path, *, window: WindowSpec, name: str = "trades.jsonl") -> BenchmarkReceipt:
    """A MECHANICS ONLY receipt whose number is recomputable from its own evidence."""
    artifact = tmp_path / name
    artifact.write_text('{"trade_id":"t","pnl":1.0}\n')
    return BenchmarkReceipt.create(
        case_id="BC-490-BARS",
        policy_id="pol_490",
        capability_score=MEASURED_SCORE,
        score_evidence=_SCORE_EVIDENCE,
        coverage_factor=_SCORE_EVIDENCE.coverage_factor,
        gates=GateVector(g0_identity=GateState.PASS, g1_causal_pit=GateState.UNKNOWN),
        computed_at_timestamp_ns=1_700_000_000_000_000_000,
        artifact_bindings=(ArtifactBinding.from_file("native_trades", artifact),),
        input_binding="input-binding-490",
        window_evidence=WindowEvidence.from_window(window),
    )


def _window_canon(canon: list[Any]) -> list[Any]:
    """The ``window_evidence`` sub-payload inside a v7 canon (located by its shape)."""
    matches = [
        element for element in canon if isinstance(element, list) and list(element[:2]) == [
            "benchmark",
            "benchmark",
        ]
    ]
    assert len(matches) == 1, f"expected exactly one window-evidence element, got {matches}"
    return matches[0]


# --------------------------------------------------------------------------- #
# 1 — the measurement travels from the window, never from the profile
# --------------------------------------------------------------------------- #
def test_bounded_window_carries_the_bars_it_measured() -> None:
    window = _bounded_window()
    assert window.bars is None, "a UTC-bounded window declares no bar count"
    assert window.measured_bars == MEASURED_BARS
    assert window.carried_bars == MEASURED_BARS

    evidence = WindowEvidence.from_window(window)
    assert evidence.bars == MEASURED_BARS, "the measured count travels into the receipt"
    assert evidence.economic_evidence is True
    assert evidence.evidence_class == "benchmark"
    assert evidence.is_smoke is False


def test_an_unmeasured_window_records_no_count_rather_than_a_guess() -> None:
    """The no-imputation rule: unloaded stays ``None`` -- not 0, not the span length."""
    window = _bounded_window(measured_bars=None)
    assert window.carried_bars is None
    evidence = WindowEvidence.from_window(window)
    assert evidence.bars is None, "nothing was measured, so nothing is recorded"
    assert evidence.bars != 0, "a missing sample size is never coerced to zero"


def test_the_manifest_keeps_the_declaration_beside_the_measurement() -> None:
    """``bars`` keeps reporting the request; ``measured_bars`` reports the tape."""
    declared = _bounded_window().as_dict()
    assert declared["bars"] is None, "the #444 manifest contract must not move"
    assert declared["measured_bars"] == MEASURED_BARS


# --------------------------------------------------------------------------- #
# 2 — the smoke shortcut and the profile cap are untouched
# --------------------------------------------------------------------------- #
def test_smoke_shortcut_and_profile_cap_are_unchanged() -> None:
    smoke = WindowSpec(tape_path="tape", profile="smoke", bars=500)
    smoke.validate()
    assert smoke.carried_bars == 500, "a bar-count window still carries its own request"
    assert WindowEvidence.from_window(smoke).bars == 500
    assert WindowEvidence.from_window(smoke).economic_evidence is False

    # a smoke run that loaded fewer bars than it asked for records what it loaded
    short = WindowSpec(tape_path="tape", profile="smoke", bars=500, measured_bars=400)
    assert short.carried_bars == 400

    # the 500-bar cap is a rule about the request, and it still fires
    with pytest.raises(ValueError, match="exceeds the smoke profile cap"):
        WindowSpec(tape_path="tape", profile="smoke", bars=501).validate()
    # an evidential profile still refuses to be described by a bar-count shortcut
    with pytest.raises(ValueError, match="bar-count window is a smoke window"):
        WindowSpec(tape_path="tape", profile="benchmark", bars=500).validate()


# --------------------------------------------------------------------------- #
# 3 — the digest changes only through WindowEvidence.canon_fields
# --------------------------------------------------------------------------- #
def test_equal_digests_imply_equal_bars(tmp_path: Path) -> None:
    first = _receipt(tmp_path, window=_bounded_window(measured_bars=168), name="a.jsonl")
    same = _receipt(tmp_path, window=_bounded_window(measured_bars=168), name="a.jsonl")
    other = _receipt(tmp_path, window=_bounded_window(measured_bars=169), name="a.jsonl")

    assert first.digest_version == RECEIPT_DIGEST_VERSION
    assert first.receipt_digest == same.receipt_digest
    assert first.window_evidence is not None and same.window_evidence is not None
    assert first.window_evidence.bars == same.window_evidence.bars

    # a different sample size is a different record: the count is *inside* the digest
    assert other.receipt_digest != first.receipt_digest
    assert other.window_evidence is not None
    assert other.window_evidence.bars == 169
    assert bound_evidence_fingerprint(other) != bound_evidence_fingerprint(first)

    # ... and it is the only element that moved: the two canons differ in exactly the
    # window_evidence sub-payload (index 4 of canon_fields is the bar count)
    def canon(receipt: BenchmarkReceipt) -> list[Any]:
        payload, refusal = build_canon_payload(
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
        assert refusal is None
        assert payload is not None
        return payload

    first_canon, other_canon = canon(first), canon(other)
    assert len(first_canon) == len(other_canon)
    differing = [
        index
        for index, (left, right) in enumerate(zip(first_canon, other_canon, strict=True))
        if left != right
    ]
    assert len(differing) == 1, f"exactly one canon element may differ, got {differing}"
    only = differing[0]
    assert _window_canon(first_canon)[4] == 168
    assert _window_canon(other_canon)[4] == 169
    assert first_canon[only] == _window_canon(first_canon)
    # the digest is that payload hashed, so the equality above is the whole story
    assert first.verify_digest() == (True, "OK")


# --------------------------------------------------------------------------- #
# 4 — publication predicate: a number with no measured sample size is refused
# --------------------------------------------------------------------------- #
def test_evidential_receipt_without_measured_bars_is_refused_by_name(
    tmp_path: Path,
) -> None:
    unmeasured = _receipt(tmp_path, window=_bounded_window(measured_bars=None))

    # D-164: the record still certifies under its own canon -- nothing is rewritten, and
    # this is a publication refusal, not a tamper verdict.
    ok, reason = unmeasured.verify()
    assert ok, reason

    refusal = unmeasured.score_publication_refusal_reason()
    assert refusal is not None
    assert refusal.startswith(EVIDENTIAL_WINDOW_BARS_UNMEASURED)
    assert str(MEASURED_SCORE) not in refusal, "a refusal never republishes the number"
    assert "no measured bar count" in refusal, "the absence is named, not replaced"

    ledger = BenchmarkLedger()
    ledger.append(unmeasured)
    publication = ledger.publication()
    assert publication.publishes_capability_score is False
    assert publication.capability_score is None
    assert publication.refusal_reason.startswith(EVIDENTIAL_WINDOW_BARS_UNMEASURED)
    assert publication.entry is None
    assert publication.as_dict()["capability_score"] is None
    # the bar count in the refused record is absent, not zeroed
    assert publication.latest_entry is not None
    assert publication.latest_entry.receipt.window_evidence is not None
    assert publication.latest_entry.receipt.window_evidence.bars is None

    # the dashboard still renders, and it withholds the number by name
    certificate = PolicyCertificate.generate(unmeasured, publication=publication)
    assert certificate.research_capability_score is None
    assert certificate.publication_token == EVIDENTIAL_WINDOW_BARS_UNMEASURED
    rendered = certificate.render_ascii()
    assert "Score: MISSING / 100" in rendered
    assert EVIDENTIAL_WINDOW_BARS_UNMEASURED in rendered
    assert str(MEASURED_SCORE) not in rendered


def test_evidential_receipt_with_measured_bars_still_publishes(tmp_path: Path) -> None:
    """The measured path is not weakened: carrying the count is enough to publish."""
    measured = _receipt(tmp_path, window=_bounded_window(measured_bars=MEASURED_BARS))
    assert measured.score_publication_refusal_reason() is None

    ledger = BenchmarkLedger()
    ledger.append(measured)
    publication = ledger.publication()
    assert publication.publishes_capability_score is True
    assert publication.capability_score == MEASURED_SCORE
    assert publication.refusal_reason == ""

    certificate = PolicyCertificate.generate(measured, publication=publication)
    assert certificate.research_capability_score == MEASURED_SCORE


def test_a_smoke_window_is_still_refused_by_its_class_first(tmp_path: Path) -> None:
    """#444 keeps precedence: a non-evidential window is named as such, not as #490."""
    artifact = tmp_path / "smoke.jsonl"
    artifact.write_text('{"trade_id":"t"}\n')
    smoke_window = WindowSpec(tape_path="tape", profile="smoke", bars=500)
    smoke_window.validate()
    receipt = BenchmarkReceipt.create(
        case_id="BC-490-SMOKE",
        policy_id="pol_490",
        capability_score=MEASURED_SCORE,
        score_evidence=_SCORE_EVIDENCE,
        coverage_factor=_SCORE_EVIDENCE.coverage_factor,
        gates=GateVector(g0_identity=GateState.PASS),
        computed_at_timestamp_ns=1_700_000_000_000_000_000,
        artifact_bindings=(ArtifactBinding.from_file("native_trades", artifact),),
        input_binding="input-binding-490",
        window_evidence=WindowEvidence.from_window(smoke_window),
    )
    refusal = receipt.score_publication_refusal_reason()
    assert refusal is not None
    assert not refusal.startswith(EVIDENTIAL_WINDOW_BARS_UNMEASURED)


# --------------------------------------------------------------------------- #
# 5 — evaluative: the real 7-day UTC run records the 168 bars it loaded
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_seven_day_utc_window_carries_its_measured_bars(tmp_path: Path) -> None:
    from v8_next.app import portfolio as port_mod

    if not TAPE_FILE.is_file():
        pytest.skip(f"quad tape absent at {TAPE_FILE}")

    out_dir = tmp_path / "seven_day"
    assert (
        port_mod.main(
            [
                "--tape-path", str(TAPE_DIR),
                "--start-utc", "2025-07-01",
                "--end-utc", "2025-07-08",
                "--output-dir", str(out_dir),
                "--profile", "benchmark",
            ]
        )
        == 0
    )

    ledger = BenchmarkLedger.load_jsonl(out_dir / "benchmark_ledger.jsonl")
    assert ledger.verify_chain() == (True, "OK")
    assert len(ledger.entries) == 1
    receipt = ledger.entries[0].receipt
    evidence = receipt.window_evidence
    assert evidence is not None

    # requirement 1: the count is the loaded tape's, not null
    assert evidence.bars == MEASURED_BARS
    assert evidence.economic_evidence is True
    assert evidence.evidence_class == "benchmark"
    assert receipt.capability_score is not None
    assert receipt.verify()[0] is True, receipt.verify()[1]

    # requirement 2: the digest recomputes, and its window_evidence tail ends in 168
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
        window_evidence=evidence,
        score_evidence=receipt.score_evidence,
    )
    assert refusal is None and canon is not None
    assert _window_canon(canon) == ["benchmark", "benchmark", False, True, MEASURED_BARS]

    # requirement 3: the number it carries is publishable, and no economic edge is minted
    assert receipt.score_publication_refusal_reason() is None
    publication = ledger.publication()
    assert publication.publishes_capability_score is True
    assert publication.capability_score == receipt.capability_score
    certificate = PolicyCertificate.generate(receipt, publication=publication)
    assert certificate.publication_token == "PUBLISHED"
    assert "NO_ECONOMIC_CLAIM" in certificate.render_ascii()
    assert "SUPPORTED_EDGE" not in certificate.render_ascii()

    # the declaration and the measurement are both on the manifest, unchanged contract
    manifest = next(
        path
        for path in sorted((out_dir / "runs").glob("*.json"))
        if not path.name.endswith(".telemetry.json")
    )
    window = json.loads(manifest.read_text())["window"]
    assert window["bars"] is None
    assert window["measured_bars"] == MEASURED_BARS
    assert window["start_ms"] == SEVEN_DAY_START_MS
    assert window["end_ms"] == SEVEN_DAY_END_MS
