"""The rendered report is a real consumer of the audit verdict (fail-closed).

The report's overall label used to be stamped ``COMPLETE`` from ``gate_results``
and ``validation_results`` alone, so an audit with missing source identities — or
with no audit verdict at all — still read as a pass. These tests pin the closed
behaviour: the label is the most severe state the supplied evidence justifies,
and an absent input can never become ``AUDIT_COMPLETE``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.audit.lineage import (
    LineageCashflow,
    LineageChain,
    LineageDecision,
    LineageFill,
    LineageOpportunity,
    LineageOrder,
    certify,
)
from v8_next.audit.reporting import (
    REPORT_STATUS_BLOCKED,
    REPORT_STATUS_COMPLETE,
    resolve_report_status,
    write_reports,
)
from v8_next.audit.verdicts import (
    AuditVerdict,
    VerdictState,
    derive_verdict,
)

AS_OF_NS = 200
D = Decimal

SOURCE_IDS = {
    "position_id": "POS-1",
    "campaign_id": "CAMP-1",
    "opportunity_id": "OPP-1",
    "decision_id": "DEC-1",
}


def _verdict(
    *,
    mechanical: VerdictState = VerdictState.PASS,
    economic: VerdictState = VerdictState.PASS,
    pit: VerdictState = VerdictState.PASS,
    source_ids: dict[str, str] | None = None,
) -> AuditVerdict:
    return derive_verdict(
        mechanical=mechanical,
        economic=economic,
        pit=pit,
        source_ids=SOURCE_IDS if source_ids is None else source_ids,
    )


def _chain(*, with_opportunity: bool = True) -> LineageChain:
    opportunity = LineageOpportunity(
        opportunity_id="OPP-1",
        campaign_id="CAMP-1",
        instrument_id="BTCUSDT-PERP",
        created_ns=90,
        available_ns=91,
    )
    decision = LineageDecision(
        decision_id="DEC-1",
        opportunity_id="OPP-1",
        campaign_id="CAMP-1",
        instrument_id="BTCUSDT-PERP",
        decision_time_ns=100,
        available_at_ns=101,
    )
    order = LineageOrder(
        order_id="ORD-1",
        decision_id="DEC-1",
        campaign_id="CAMP-1",
        instrument_id="BTCUSDT-PERP",
        event_time_ns=110,
        available_at_ns=111,
    )
    fill = LineageFill(
        fill_id="FIL-1",
        order_id="ORD-1",
        decision_id="DEC-1",
        campaign_id="CAMP-1",
        instrument_id="BTCUSDT-PERP",
        event_time_ns=120,
        available_at_ns=121,
    )
    cashflow = LineageCashflow(
        cashflow_id="CF-1",
        fill_id="FIL-1",
        campaign_id="CAMP-1",
        event_time_ns=130,
        available_at_ns=131,
        amount=D("-0.05"),
    )
    return LineageChain(
        opportunities=(opportunity,) if with_opportunity else (),
        decisions=(decision,),
        orders=(order,),
        fills=(fill,),
        cashflows=(cashflow,),
    )


VALIDATION_PASS = {"status": "PASS", "violations": []}


def _status_text(report: str) -> str:
    line = next(row for row in report.splitlines() if row.startswith("**DURUM:**"))
    return line.removeprefix("**DURUM:**").strip()


# --------------------------------------------------------------------------- #
# A. resolver: severity merge, no input means no pass
# --------------------------------------------------------------------------- #
def test_no_audit_input_is_explicitly_blocked_not_complete() -> None:
    assert resolve_report_status() == REPORT_STATUS_BLOCKED
    assert resolve_report_status(validation=VALIDATION_PASS) == REPORT_STATUS_BLOCKED
    assert (
        resolve_report_status(
            gates=({"gate_id": "G1", "status": "PASS"},),
            validation=VALIDATION_PASS,
        )
        == REPORT_STATUS_BLOCKED
    )


def test_missing_source_ids_never_read_as_complete() -> None:
    incomplete = _verdict(
        source_ids={"position_id": "POS-1", "campaign_id": "CAMP-1", "decision_id": "DEC-1"}
    )
    assert incomplete.completeness is VerdictState.INCOMPLETE
    assert resolve_report_status(verdict=incomplete, validation=VALIDATION_PASS) == (
        "AUDIT_INCOMPLETE"
    )


def test_mechanical_fail_is_labelled_fail_not_pass() -> None:
    failed = _verdict(mechanical=VerdictState.FAIL)
    # Completeness of the evidence itself can still be PASS for a failed mechanic.
    assert failed.completeness is VerdictState.PASS
    assert resolve_report_status(verdict=failed, validation=VALIDATION_PASS) == "AUDIT_FAIL"


def test_mechanical_not_identifiable_is_labelled_not_identifiable() -> None:
    unidentifiable = _verdict(mechanical=VerdictState.NOT_IDENTIFIABLE)
    assert resolve_report_status(verdict=unidentifiable, validation=VALIDATION_PASS) == (
        "AUDIT_NOT_IDENTIFIABLE"
    )


def test_everything_earned_is_the_only_path_to_complete() -> None:
    assert (
        resolve_report_status(
            verdict=_verdict(),
            lineage=certify(_chain(), as_of_ns=AS_OF_NS),
            validation=VALIDATION_PASS,
        )
        == REPORT_STATUS_COMPLETE
    )


def test_unresolved_dimension_downgrades_a_complete_verdict() -> None:
    stuck = _verdict(economic=VerdictState.INCOMPLETE)
    assert stuck.completeness is VerdictState.INCOMPLETE
    assert resolve_report_status(verdict=stuck, validation=VALIDATION_PASS) == "AUDIT_INCOMPLETE"


def test_lineage_incomplete_downgrades_overall() -> None:
    certification = certify(_chain(with_opportunity=False), as_of_ns=AS_OF_NS)
    assert (
        resolve_report_status(verdict=_verdict(), lineage=certification, validation=VALIDATION_PASS)
        == REPORT_STATUS_BLOCKED
    )


def test_lineage_missing_join_keeps_report_incomplete() -> None:
    chain = _chain()
    broken = LineageChain(
        opportunities=chain.opportunities,
        decisions=(replace(chain.decisions[0], opportunity_id="OPP-UNKNOWN"),),
        orders=chain.orders,
        fills=chain.fills,
        cashflows=chain.cashflows,
    )
    certification = certify(broken, as_of_ns=AS_OF_NS)
    assert (
        resolve_report_status(verdict=_verdict(), lineage=certification, validation=VALIDATION_PASS)
        == "AUDIT_INCOMPLETE"
    )


def test_gate_failure_downgrades_even_with_complete_verdict() -> None:
    verdict = _verdict()
    assert (
        resolve_report_status(
            verdict=verdict,
            gates=({"gate_id": "G7", "status": "FAIL"},),
            validation=VALIDATION_PASS,
        )
        == "AUDIT_FAIL"
    )
    assert (
        resolve_report_status(
            verdict=verdict,
            gates=({"gate_id": "G7", "status": "BLOCKED"},),
            validation=VALIDATION_PASS,
        )
        == REPORT_STATUS_BLOCKED
    )


def test_failing_validation_downgrades_even_with_complete_verdict() -> None:
    assert (
        resolve_report_status(
            verdict=_verdict(),
            validation={"status": "FAIL", "violations": ["X"]},
        )
        == "AUDIT_INCOMPLETE"
    )


def test_absent_validation_is_not_a_pass() -> None:
    assert resolve_report_status(verdict=_verdict()) == "AUDIT_INCOMPLETE"


# --------------------------------------------------------------------------- #
# B. rendered report carries the resolved label
# --------------------------------------------------------------------------- #
def _write_validation(run_dir: Path, status: str = "PASS") -> None:
    (run_dir / "validation_results.json").write_text(
        json.dumps({"status": status, "violations": []}) + "\n", encoding="utf-8"
    )


def test_rendered_report_without_audit_input_is_blocked(tmp_path: Path) -> None:
    _write_validation(tmp_path)
    write_reports(tmp_path)
    turkish = (tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
    audit = (tmp_path / "AUDIT_REPORT.md").read_text(encoding="utf-8")
    assert _status_text(turkish) == REPORT_STATUS_BLOCKED
    assert "Audit status: **AUDIT_BLOCKED**" in audit
    assert REPORT_STATUS_COMPLETE not in turkish


def test_rendered_report_with_missing_source_ids_is_incomplete(tmp_path: Path) -> None:
    _write_validation(tmp_path)
    verdict = _verdict(source_ids={"position_id": "POS-1", "campaign_id": "CAMP-1"})
    write_reports(tmp_path, verdict=verdict)
    turkish = (tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
    assert _status_text(turkish) == "AUDIT_INCOMPLETE"
    assert REPORT_STATUS_COMPLETE not in turkish


def test_rendered_report_with_mechanical_fail_never_reads_as_pass(tmp_path: Path) -> None:
    _write_validation(tmp_path)
    write_reports(tmp_path, verdict=_verdict(mechanical=VerdictState.FAIL))
    turkish = (tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
    assert _status_text(turkish) == "AUDIT_FAIL"
    assert REPORT_STATUS_COMPLETE not in turkish


def test_rendered_report_is_complete_only_with_verdict_and_lineage(tmp_path: Path) -> None:
    _write_validation(tmp_path)
    chain = _chain()
    write_reports(tmp_path, verdict=_verdict(), lineage=chain, as_of_ns=AS_OF_NS)
    turkish = (tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
    audit = (tmp_path / "AUDIT_REPORT.md").read_text(encoding="utf-8")
    assert _status_text(turkish) == REPORT_STATUS_COMPLETE
    assert "Audit status: **AUDIT_COMPLETE**" in audit
    assert "lineage=COMPLETE" in turkish

    # The same verdict without the lineage layer renders COMPLETE too (no chain was
    # claimed), but a chain that fails certification always blocks it.
    write_reports(
        tmp_path, verdict=_verdict(), lineage=_chain(with_opportunity=False), as_of_ns=AS_OF_NS
    )
    downgraded = (tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
    assert _status_text(downgraded) == REPORT_STATUS_BLOCKED


def test_rendered_report_renders_a_supplied_certification_directly(tmp_path: Path) -> None:
    _write_validation(tmp_path)
    certification = certify(_chain(), as_of_ns=AS_OF_NS)
    write_reports(tmp_path, verdict=_verdict(), lineage=certification)
    turkish = (tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
    assert _status_text(turkish) == REPORT_STATUS_COMPLETE


def test_chain_without_cut_cannot_be_certified(tmp_path: Path) -> None:
    _write_validation(tmp_path)
    with pytest.raises(ValueError, match="as_of_ns"):
        write_reports(tmp_path, verdict=_verdict(), lineage=_chain())
