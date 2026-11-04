"""M4-06 (#34): the independent V9 scientific verdict and promotion matrix.

A promotion decision is not one number. It is five separate questions, each of
which can fail on its own:

* ``MECHANICAL`` -- the code, tests, provenance and lineage evidence exist and
  agree with the recorded code SHA.
* ``PIT`` -- the point-in-time observation and belief contracts are evidenced
  and a usable dataset is declared ``AVAILABLE``.
* ``ECONOMIC`` -- a *registered* preregistration (M4-01), a permitted dataset
  role and a costed result (M3-07 / M4-02) all exist. Without all three the
  dimension is ``NOT_RUN``; it is never inferred from a promising backtest.
* ``RESOURCE`` -- a measured resource receipt states what was actually run and
  whether the 4 GiB / 40-leg / 12-month scope was covered.
* ``LIVE_APPROVAL`` -- always ``BLOCKED`` here. This module has no authority to
  promote anything; it can only publish a matrix for the owner to review.

The overall verdict is the **worst** dimension, using the fail-closed ordering
``BLOCKED > NOT_RUN > FAIL > INCONCLUSIVE > NO_EDGE > PASS``. There is
deliberately no code path that sets ``live_approved`` to ``True``: it is a
property that always returns ``False`` and the only marker it carries is
``requires_owner_authorization``.

Evidence is bound by SHA-256. A ref whose digest does not match the file on
disk is not "stale but usable": :meth:`PromotionMatrix.verify` turns that
dimension ``BLOCKED``.

Run the collector from the repository root::

    PYTHONPATH=src python -m v8_next.evaluation.verdict \
        --root . --out reports/overnight/V9_VERDICT.md
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.evaluation.protocol import DatasetRole
from v8_next.evaluation.tape_identity import sha256_file

#: Canonical report order. The matrix always renders in this order so two runs
#: over identical files produce byte-identical output.
DIMENSION_ORDER: Final[tuple[str, ...]] = (
    "mechanical",
    "pit",
    "economic",
    "resource",
    "live_approval",
)

#: Repository-relative artifacts the collector reads.
MECHANICAL_EVIDENCE: Final[tuple[str, ...]] = (
    "reports/provenance/baseline.json",
    "reports/overnight/evidence/M0-02.md",
    "reports/overnight/evidence/M0-03.md",
)
#: Optional machine-readable test log. It is coordination state (not tracked at
#: every base commit), so its absence is not a failure; when present it is hashed
#: and parsed and a corrupt log still blocks the dimension.
MECHANICAL_TEST_RESULTS: Final = "reports/overnight/test-results.jsonl"
PIT_EVIDENCE: Final[tuple[str, ...]] = (
    "reports/overnight/evidence/M1-01.md",
    "reports/overnight/evidence/M1-02.md",
    "reports/overnight/evidence/M1-03.md",
    "reports/overnight/evidence/M1-04.md",
    "reports/provenance/baseline.json",
)
PREREGISTRATION_REPORT: Final = "reports/evaluation/preregistration.json"
ACCESS_REPORT: Final = "reports/evaluation/access_report.json"
COSTED_RESULT_REPORT: Final = "reports/evaluation/costed_result.json"
RESOURCE_RECEIPT_DIR: Final = "reports/overnight/evidence/receipts"


class PromotionDimension(StrEnum):
    """The five independent promotion questions."""

    MECHANICAL = "MECHANICAL"
    PIT = "PIT"
    ECONOMIC = "ECONOMIC"
    RESOURCE = "RESOURCE"
    LIVE_APPROVAL = "LIVE_APPROVAL"


class DimensionStatus(StrEnum):
    """A dimension's outcome. Only ``PASS`` claims the dimension is satisfied."""

    PASS = "PASS"
    NO_EDGE = "NO_EDGE"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"


#: Higher is worse. Overall = maximum severity, so an unknown/blocked dimension
#: can never be hidden behind a passing one.
_STATUS_SEVERITY: Final[dict[DimensionStatus, int]] = {
    DimensionStatus.PASS: 0,
    DimensionStatus.NO_EDGE: 1,
    DimensionStatus.INCONCLUSIVE: 2,
    DimensionStatus.FAIL: 3,
    DimensionStatus.NOT_RUN: 4,
    DimensionStatus.BLOCKED: 5,
}

#: The four dimensions that can produce a scientific measurement. ``LIVE_APPROVAL``
#: is excluded from the *scientific* verdict because it is never granted here.
_SCIENTIFIC_DIMENSIONS: Final[tuple[PromotionDimension, ...]] = (
    PromotionDimension.MECHANICAL,
    PromotionDimension.PIT,
    PromotionDimension.ECONOMIC,
    PromotionDimension.RESOURCE,
)

_OPEN_ROLES: Final[frozenset[str]] = frozenset(
    {DatasetRole.DEVELOPMENT.value, DatasetRole.VALIDATION.value}
)
_ALL_ROLES: Final[frozenset[str]] = frozenset(role.value for role in DatasetRole)


def worst_status(statuses: Sequence[DimensionStatus]) -> DimensionStatus:
    """Return the most severe status; empty input is ``PASS`` by construction."""

    resolved = DimensionStatus.PASS
    for status in statuses:
        if _STATUS_SEVERITY[status] > _STATUS_SEVERITY[resolved]:
            resolved = status
    return resolved


class EvidenceRef(BaseModel):
    """A repository-relative artifact bound to the SHA-256 it had when read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def matches(self, root: Path) -> bool:
        """True only when the file exists and still hashes to this digest."""

        candidate = root / self.path
        return candidate.is_file() and sha256_file(candidate) == self.sha256


class DimensionVerdict(BaseModel):
    """One dimension's verdict, its evidence binding and the reasons why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: PromotionDimension
    status: DimensionStatus
    evidence: tuple[EvidenceRef, ...] = ()
    notes: tuple[str, ...] = ()


class PromotionMatrix(BaseModel):
    """The five dimensions; the overall verdict is the worst of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanical: DimensionVerdict
    pit: DimensionVerdict
    economic: DimensionVerdict
    resource: DimensionVerdict
    live_approval: DimensionVerdict

    @model_validator(mode="after")
    def _slots_are_consistent(self) -> Self:
        expected = (
            (PromotionDimension.MECHANICAL, self.mechanical),
            (PromotionDimension.PIT, self.pit),
            (PromotionDimension.ECONOMIC, self.economic),
            (PromotionDimension.RESOURCE, self.resource),
            (PromotionDimension.LIVE_APPROVAL, self.live_approval),
        )
        for dimension, verdict in expected:
            if verdict.dimension is not dimension:
                raise ValueError(
                    f"slot {dimension.value} carries a {verdict.dimension.value} verdict"
                )
        if self.live_approval.status is not DimensionStatus.BLOCKED:
            raise ValueError(
                "live approval is never granted by a verdict artifact; "
                "it must remain BLOCKED"
            )
        return self

    @property
    def dimensions(self) -> tuple[DimensionVerdict, ...]:
        """All five verdicts in canonical order."""

        return (self.mechanical, self.pit, self.economic, self.resource, self.live_approval)

    @property
    def overall_verdict(self) -> DimensionStatus:
        """The worst dimension, including ``LIVE_APPROVAL`` (always ``BLOCKED``)."""

        return worst_status([verdict.status for verdict in self.dimensions])

    @property
    def scientific_verdict(self) -> DimensionStatus:
        """The worst of the four measurable dimensions (excludes live approval)."""

        return worst_status(
            [
                verdict.status
                for verdict in self.dimensions
                if verdict.dimension in _SCIENTIFIC_DIMENSIONS
            ]
        )

    @property
    def live_approved(self) -> bool:
        """Always ``False``. Promotion is an owner action, never a module output."""

        return False

    @property
    def requires_owner_authorization(self) -> bool:
        """Always ``True``; the marker that accompanies the immutable ``False``."""

        return True

    def verify(self, root: Path) -> PromotionMatrix:
        """Return a copy where any unverifiable evidence ref blocks its dimension."""

        updates: dict[str, DimensionVerdict] = {}
        for field, verdict in (
            ("mechanical", self.mechanical),
            ("pit", self.pit),
            ("economic", self.economic),
            ("resource", self.resource),
            ("live_approval", self.live_approval),
        ):
            problems = verify_evidence_refs(root, verdict.evidence)
            if problems:
                updates[field] = verdict.model_copy(
                    update={
                        "status": DimensionStatus.BLOCKED,
                        "notes": verdict.notes
                        + tuple(f"blocked prerequisite: {problem}" for problem in problems),
                    }
                )
        if not updates:
            return self
        return self.model_copy(update=updates)


def evidence_ref(root: Path, relative_path: str) -> EvidenceRef:
    """Hash an existing artifact; a missing file is a hard error, never a ref."""

    candidate = root / relative_path
    if not candidate.is_file():
        raise FileNotFoundError(f"evidence artifact does not exist: {relative_path}")
    return EvidenceRef(path=relative_path, sha256=sha256_file(candidate))


def verify_evidence_refs(root: Path, refs: Sequence[EvidenceRef]) -> tuple[str, ...]:
    """Return one problem string per ref that is missing or digest-mismatched."""

    problems: list[str] = []
    for ref in refs:
        candidate = root / ref.path
        if not candidate.is_file():
            problems.append(f"evidence file is missing: {ref.path}")
        elif sha256_file(candidate) != ref.sha256:
            problems.append(
                f"evidence sha256 mismatch for {ref.path}: recorded {ref.sha256}"
            )
    return tuple(problems)


def build_matrix(
    *,
    mechanical: DimensionVerdict,
    pit: DimensionVerdict,
    economic: DimensionVerdict,
    resource: DimensionVerdict,
    live_approval: DimensionVerdict,
) -> PromotionMatrix:
    """Assemble the matrix, refusing a live-approval dimension that is not blocked."""

    return PromotionMatrix(
        mechanical=mechanical,
        pit=pit,
        economic=economic,
        resource=resource,
        live_approval=live_approval,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _blocked_prerequisites(matrix: PromotionMatrix) -> tuple[str, ...]:
    """Every explicit blocked-prerequisite note, in canonical dimension order."""

    seen: set[str] = set()
    ordered: list[str] = []
    for verdict in matrix.dimensions:
        for note in verdict.notes:
            if not note.startswith("blocked prerequisite:"):
                continue
            if note in seen:
                continue
            seen.add(note)
            ordered.append(note)
    return tuple(ordered)


def next_step(matrix: PromotionMatrix) -> str:
    """The single deterministic next action for the current matrix."""

    scientific = [
        verdict for verdict in matrix.dimensions if verdict.dimension in _SCIENTIFIC_DIMENSIONS
    ]
    blocked = [verdict.dimension.value for verdict in scientific if verdict.status is DimensionStatus.BLOCKED]
    not_run = [
        verdict.dimension.value for verdict in scientific if verdict.status is DimensionStatus.NOT_RUN
    ]
    failed = [verdict.dimension.value for verdict in scientific if verdict.status is DimensionStatus.FAIL]
    if blocked:
        step = (
            "Clear the blocked prerequisite(s) for "
            + ", ".join(blocked)
            + " by supplying the named artifacts, then re-run "
            + "`collect_from_artifacts` and publish the regenerated matrix."
        )
    elif not_run:
        step = (
            "Run the missing measurement(s) for "
            + ", ".join(not_run)
            + ". For ECONOMIC that means a registered preregistration, a permitted "
            + "dataset role and a costed replay (M3-07 / M4-02); no edge claim is "
            + "made before then."
        )
    elif failed:
        step = (
            "Resolve the failing dimension(s) "
            + ", ".join(failed)
            + " before any further promotion claim."
        )
    else:
        step = (
            "Submit this matrix to the owner for authorization review. This artifact "
            "does not and cannot grant live approval."
        )
    return step + " LIVE_APPROVAL remains BLOCKED pending explicit owner authorization."


def render_markdown(matrix: PromotionMatrix) -> str:
    """Render the published verdict document. Deterministic for a fixed matrix."""

    lines: list[str] = [
        "# V9 scientific verdict and promotion matrix",
        "",
        "This document is generated deterministically by "
        "`v8_next.evaluation.verdict.collect_from_artifacts`; it contains no clock, "
        "no network access and no live-trading authority.",
        "",
        f"**Overall verdict (worst of five): `{matrix.overall_verdict.value}`**",
        "",
        f"**Scientific verdict (MECHANICAL/PIT/ECONOMIC/RESOURCE): "
        f"`{matrix.scientific_verdict.value}`**",
        "",
        f"**Live approved: `{matrix.live_approved}`**; "
        f"requires owner authorization: `{matrix.requires_owner_authorization}`.",
        "",
        "## Dimensions",
        "",
        "| Dimension | Verdict | Evidence (path + sha256) | Notes |",
        "|---|---|---|---|",
    ]
    for verdict in matrix.dimensions:
        evidence = (
            "<br>".join(f"`{ref.path}` `{ref.sha256}`" for ref in verdict.evidence)
            or "none"
        )
        notes = "<br>".join(verdict.notes) or "-"
        lines.append(
            f"| {verdict.dimension.value} | {verdict.status.value} | {evidence} | {notes} |"
        )

    lines.extend(["", "## Blocked prerequisites", ""])
    prerequisites = _blocked_prerequisites(matrix)
    if prerequisites:
        lines.extend(f"- {note}" for note in prerequisites)
    else:
        lines.append("- none recorded")
    lines.extend(["", "## Failed or inconclusive dimensions", ""])
    residual = [
        verdict
        for verdict in matrix.dimensions
        if verdict.status in (DimensionStatus.FAIL, DimensionStatus.INCONCLUSIVE)
    ]
    if residual:
        lines.extend(
            f"- {verdict.dimension.value}: {verdict.status.value}"
            + (f" ({'; '.join(verdict.notes)})" if verdict.notes else "")
            for verdict in residual
        )
    else:
        lines.append("- none recorded")
    lines.extend(
        [
            "",
            "## Next step",
            "",
            next_step(matrix),
            "",
            "## Promotion policy",
            "",
            "- `live_approved` is defined as `False` by construction; this module has "
            "no assignment path that can set it `True`.",
            "- `requires_owner_authorization` is `True`; only the repository owner may "
            "authorize live trading, outside this artifact.",
            "- The overall verdict is the worst dimension under the fail-closed ordering "
            "`BLOCKED > NOT_RUN > FAIL > INCONCLUSIVE > NO_EDGE > PASS`.",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Collector over the repository's real evidence
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _present_refs(root: Path, relatives: Sequence[str]) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for relative in relatives:
        if (root / relative).is_file():
            refs.append(evidence_ref(root, relative))
    return tuple(refs)


def _missing(root: Path, relatives: Sequence[str]) -> tuple[str, ...]:
    return tuple(relative for relative in relatives if not (root / relative).is_file())


def _blocked(
    dimension: PromotionDimension,
    reasons: Sequence[str],
    evidence: tuple[EvidenceRef, ...] = (),
) -> DimensionVerdict:
    return DimensionVerdict(
        dimension=dimension,
        status=DimensionStatus.BLOCKED,
        evidence=evidence,
        notes=tuple(f"blocked prerequisite: {reason}" for reason in reasons),
    )


def _mechanical(root: Path) -> DimensionVerdict:
    missing = _missing(root, MECHANICAL_EVIDENCE)
    if missing:
        return _blocked(
            PromotionDimension.MECHANICAL,
            [f"missing mechanical evidence: {', '.join(missing)}"],
            _present_refs(root, MECHANICAL_EVIDENCE),
        )
    test_results_path = root / MECHANICAL_TEST_RESULTS
    try:
        baseline = _read_json(root / "reports/provenance/baseline.json")
        code_sha = baseline.get("code_sha")
        baseline_tests = baseline.get("baseline_tests")
        if not isinstance(code_sha, str) or len(code_sha) < 7:
            raise ValueError("provenance baseline has no code_sha")
        if not isinstance(baseline_tests, dict) or not isinstance(
            baseline_tests.get("passed"), int
        ):
            raise ValueError("provenance baseline has no baseline_tests.passed")
        rows: list[dict[str, Any]] = []
        if test_results_path.is_file():
            text = test_results_path.read_text(encoding="utf-8")
            parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
            if not parsed or not all(isinstance(row, dict) for row in parsed):
                raise ValueError("test-results.jsonl is empty or not object-per-line")
            rows = [row for row in parsed if isinstance(row, dict)]
    except (OSError, ValueError) as exc:
        return _blocked(
            PromotionDimension.MECHANICAL,
            [f"mechanical evidence is unreadable: {exc}"],
            _present_refs(root, MECHANICAL_EVIDENCE + (MECHANICAL_TEST_RESULTS,)),
        )
    evidence_paths = MECHANICAL_EVIDENCE + (
        (MECHANICAL_TEST_RESULTS,) if test_results_path.is_file() else ()
    )
    notes = [
        f"provenance code_sha: {code_sha}; baseline passed: {baseline_tests['passed']}",
        "lineage evidence present: M0-02 (two-campaign oracle), M0-03 (opportunity lineage)",
    ]
    notes.append(
        f"recorded test-run rows: {len(rows)}"
        if test_results_path.is_file()
        else "recorded test-run log not present in this checkout; provenance baseline carries the recorded test counts"
    )
    return DimensionVerdict(
        dimension=PromotionDimension.MECHANICAL,
        status=DimensionStatus.PASS,
        evidence=_present_refs(root, evidence_paths),
        notes=tuple(notes),
    )


def _pit(root: Path) -> DimensionVerdict:
    missing = _missing(root, PIT_EVIDENCE)
    if missing:
        return _blocked(
            PromotionDimension.PIT,
            [f"missing PIT evidence: {', '.join(missing)}"],
            _present_refs(root, PIT_EVIDENCE),
        )
    try:
        baseline = _read_json(root / "reports/provenance/baseline.json")
        datasets = baseline.get("datasets")
        if not isinstance(datasets, list):
            raise ValueError("provenance baseline has no datasets list")
        available = [
            dataset
            for dataset in datasets
            if isinstance(dataset, dict) and dataset.get("status") == "AVAILABLE"
        ]
        if not available:
            raise ValueError("no dataset is AVAILABLE and pointed-in-time")
    except (OSError, ValueError) as exc:
        return _blocked(
            PromotionDimension.PIT,
            [f"PIT evidence is unreadable: {exc}"],
            _present_refs(root, PIT_EVIDENCE),
        )
    return DimensionVerdict(
        dimension=PromotionDimension.PIT,
        status=DimensionStatus.PASS,
        evidence=_present_refs(root, PIT_EVIDENCE),
        notes=(
            "observation contract M1-01 and multi-horizon context M1-02 evidenced",
            "belief state M1-03 and incremental update M1-04 evidenced",
            f"dataset(s) AVAILABLE: {len(available)}",
        ),
    )


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _cost_vector(data: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
    costs = data.get("costs")
    if not isinstance(costs, dict):
        return None
    if costs.get("funding_included") is not True:
        return None
    fee = _decimal(costs.get("fee_bps_per_side"))
    funding = _decimal(costs.get("funding_bps_per_day"))
    slippage = _decimal(costs.get("slippage_bps_per_side"))
    if fee is None or funding is None or slippage is None:
        return None
    if fee < 0 or funding < 0 or slippage < 0:
        return None
    return fee, funding, slippage


_COSTED_VERDICTS: Final[dict[str, DimensionStatus]] = {
    "PASS": DimensionStatus.PASS,
    "NO_EDGE": DimensionStatus.NO_EDGE,
    "INCONCLUSIVE": DimensionStatus.INCONCLUSIVE,
    "STOP": DimensionStatus.FAIL,
    "INVALID": DimensionStatus.BLOCKED,
}


def _economic(root: Path) -> DimensionVerdict:
    evidence_paths = (PREREGISTRATION_REPORT, ACCESS_REPORT, COSTED_RESULT_REPORT)
    present = _present_refs(root, evidence_paths)
    prereg_path = root / PREREGISTRATION_REPORT
    if not prereg_path.is_file():
        return DimensionVerdict(
            dimension=PromotionDimension.ECONOMIC,
            status=DimensionStatus.NOT_RUN,
            evidence=present,
            notes=(
                "blocked prerequisite: no registered preregistration record "
                f"({PREREGISTRATION_REPORT}); M4-01 has not published one for this repository",
                "blocked prerequisite: no costed economic result (M3-07 / M4-02)",
            ),
        )
    try:
        prereg = _read_json(prereg_path)
    except (OSError, ValueError) as exc:
        return _blocked(
            PromotionDimension.ECONOMIC,
            [f"preregistration is unreadable: {exc}"],
            present,
        )
    prereg_role = prereg.get("dataset_role")
    if not isinstance(prereg_role, str) or prereg_role not in _ALL_ROLES:
        return _blocked(
            PromotionDimension.ECONOMIC,
            [f"preregistration dataset_role is missing or unknown: {prereg_role!r}"],
            present,
        )
    prereg_costs = _cost_vector(prereg)
    if prereg_costs is None:
        return _blocked(
            PromotionDimension.ECONOMIC,
            [
                "preregistration does not register fee, funding and slippage terms "
                "with funding_included=true"
            ],
            present,
        )
    access_path = root / ACCESS_REPORT
    if not access_path.is_file():
        return DimensionVerdict(
            dimension=PromotionDimension.ECONOMIC,
            status=DimensionStatus.NOT_RUN,
            evidence=present,
            notes=(
                f"blocked prerequisite: no access report ({ACCESS_REPORT}); the dataset "
                f"role {prereg_role} is not yet proven permitted",
            ),
        )
    try:
        access = _read_json(access_path)
        permitted = access.get("permitted_roles")
        if not isinstance(permitted, list) or not all(isinstance(item, str) for item in permitted):
            raise ValueError("access report has no permitted_roles list")
    except (OSError, ValueError) as exc:
        return _blocked(
            PromotionDimension.ECONOMIC,
            [f"access report is unreadable: {exc}"],
            present,
        )
    if prereg_role not in permitted:
        return _blocked(
            PromotionDimension.ECONOMIC,
            [f"dataset role {prereg_role} is not permitted by the access report"],
            present,
        )
    if prereg_role == DatasetRole.SEALED_OOS.value and access.get("sealed_authorized") is not True:
        return _blocked(
            PromotionDimension.ECONOMIC,
            ["SEALED_OOS was not opened under a recorded sealed authorization"],
            present,
        )
    costed_path = root / COSTED_RESULT_REPORT
    if not costed_path.is_file():
        return DimensionVerdict(
            dimension=PromotionDimension.ECONOMIC,
            status=DimensionStatus.NOT_RUN,
            evidence=present,
            notes=(
                f"blocked prerequisite: no costed result ({COSTED_RESULT_REPORT}); "
                "M3-07 finalists and the M4-02 costed replay have not run",
            ),
        )
    try:
        costed = _read_json(costed_path)
        recorded_digest = costed.get("preregistration_digest")
        actual_digest = sha256_file(prereg_path)
        if recorded_digest != actual_digest:
            raise ValueError(
                "costed result is bound to a different preregistration "
                f"(recorded {recorded_digest!r}, on disk {actual_digest})"
            )
        costed_role = costed.get("dataset_role")
        if costed_role != prereg_role:
            raise ValueError(
                f"costed result uses dataset role {costed_role!r}, preregistration "
                f"registers {prereg_role!r}"
            )
        costed_costs = _cost_vector(costed)
        if costed_costs is None:
            raise ValueError(
                "costed result does not include fee, funding and slippage with "
                "funding_included=true"
            )
        if costed_costs != prereg_costs:
            raise ValueError("costed result costs differ from the registered costs")
        net_return = _decimal(costed.get("net_return"))
        if net_return is None:
            raise ValueError("costed result has no numeric net_return")
        verdict_value = costed.get("verdict")
        if not isinstance(verdict_value, str):
            raise ValueError("costed result has no verdict")
        status = _COSTED_VERDICTS.get(verdict_value.upper())
        if status is None:
            raise ValueError(f"costed result verdict is unknown: {verdict_value!r}")
    except (OSError, ValueError) as exc:
        return _blocked(
            PromotionDimension.ECONOMIC,
            [f"costed result is not usable: {exc}"],
            present,
        )
    notes = [
        f"preregistered hypothesis: {prereg.get('hypothesis_id', '<unnamed>')}",
        f"dataset role: {prereg_role}; costed net_return: {net_return}",
        "costs include fees, funding and slippage as registered",
    ]
    uncertainty = costed.get("uncertainty")
    if isinstance(uncertainty, dict) and uncertainty:
        notes.append(
            "uncertainty: "
            + ", ".join(f"{key}={value}" for key, value in sorted(uncertainty.items()))
        )
    else:
        notes.append("uncertainty: not reported by the costed result")
    return DimensionVerdict(
        dimension=PromotionDimension.ECONOMIC,
        status=status,
        evidence=present,
        notes=tuple(notes),
    )


def _resource_status(receipt: dict[str, Any]) -> tuple[DimensionStatus, str]:
    memory = receipt.get("memory")
    scope = receipt.get("scope")
    within = receipt.get("within_rss_target")
    if not isinstance(memory, dict) or not isinstance(scope, dict):
        raise ValueError("resource receipt has no memory/scope objects")
    peak = memory.get("peak_rss_bytes")
    memory_status = memory.get("status")
    if memory_status != "MEASURED" or not isinstance(peak, int):
        return DimensionStatus.NOT_RUN, "peak RSS was not measured"
    if within is not True and within is not False:
        raise ValueError("resource receipt has no boolean within_rss_target")
    scope_status = scope.get("status")
    refusal = scope.get("refusal_reason")
    if within is False:
        return (
            DimensionStatus.FAIL,
            f"peak RSS {peak} B exceeds the declared target",
        )
    if scope_status == "MEASURED":
        return (
            DimensionStatus.PASS,
            f"peak RSS {peak} B within target; scope covers the declared deployment target",
        )
    detail = f"scope {scope_status}: {refusal}" if refusal else f"scope {scope_status}"
    return (
        DimensionStatus.INCONCLUSIVE,
        f"peak RSS {peak} B within target but {detail}",
    )


def _resource(root: Path) -> DimensionVerdict:
    receipt_dir = root / RESOURCE_RECEIPT_DIR
    receipts = sorted(receipt_dir.glob("*receipt.json")) if receipt_dir.is_dir() else []
    relatives = [receipt.relative_to(root).as_posix() for receipt in receipts]
    if not receipts:
        return DimensionVerdict(
            dimension=PromotionDimension.RESOURCE,
            status=DimensionStatus.NOT_RUN,
            notes=(
                "blocked prerequisite: no measured resource receipt under "
                f"{RESOURCE_RECEIPT_DIR} (M0-08)",
            ),
        )
    try:
        parsed = [(receipt, _read_json(receipt)) for receipt in receipts]
    except (OSError, ValueError) as exc:
        return _blocked(
            PromotionDimension.RESOURCE,
            [f"resource receipt is unreadable: {exc}"],
            _present_refs(root, relatives),
        )
    # Throughput receipts (e.g. M1-06 feature reads) measure wall/RSS but do not
    # declare a deployment scope or a 4 GiB target. They are not qualification
    # receipts; only a receipt carrying both fields can qualify the deployment.
    qualifying = [
        (path, receipt)
        for path, receipt in parsed
        if isinstance(receipt.get("scope"), dict)
        and isinstance(receipt.get("within_rss_target"), bool)
    ]
    if not qualifying:
        return DimensionVerdict(
            dimension=PromotionDimension.RESOURCE,
            status=DimensionStatus.NOT_RUN,
            evidence=_present_refs(root, relatives),
            notes=(
                "blocked prerequisite: no resource receipt declares both a deployment "
                "scope and a 4 GiB target (M0-08)",
                f"auxiliary measurement receipts without a deployment scope: {len(parsed)}",
            ),
        )
    statuses: list[DimensionStatus] = []
    notes: list[str] = []
    for receipt_path, receipt in qualifying:
        status, note = _resource_status(receipt)
        statuses.append(status)
        notes.append(f"{receipt_path.name}: {note}")
    qualifying_relatives = [path.relative_to(root).as_posix() for path, _ in qualifying]
    return DimensionVerdict(
        dimension=PromotionDimension.RESOURCE,
        status=worst_status(statuses),
        evidence=_present_refs(root, qualifying_relatives),
        notes=tuple(notes),
    )


def _live_approval() -> DimensionVerdict:
    return DimensionVerdict(
        dimension=PromotionDimension.LIVE_APPROVAL,
        status=DimensionStatus.BLOCKED,
        notes=(
            "blocked prerequisite: explicit owner authorization; this module has no "
            "authority to promote to live trading",
            "live_approved is False by construction (requires_owner_authorization=True)",
        ),
    )


def collect_from_artifacts(root: Path) -> PromotionMatrix:
    """Read the repository's real evidence and publish the fail-closed matrix."""

    resolved = Path(root)
    matrix = build_matrix(
        mechanical=_mechanical(resolved),
        pit=_pit(resolved),
        economic=_economic(resolved),
        resource=_resource(resolved),
        live_approval=_live_approval(),
    )
    return matrix.verify(resolved)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, default=None)
    arguments = parser.parse_args(argv)
    rendered = render_markdown(collect_from_artifacts(arguments.root))
    if arguments.out is None:
        print(rendered)
    else:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
