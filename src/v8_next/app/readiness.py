"""Readiness audit for the locked benchmark (docs/contracts/V87_READINESS_BENCHMARK_SPEC.md).

Answers "how production-ready are we" with four separately printed factors, never one
number on its own. Every factor is derived from artifacts on disk:

* ``gate_factor``   - gates PASS / 10, from the resolved battery artifact when one is
                      published under its own producer's contract (#447), else from the
                      latest ledger receipt's gate vector
* ``pillar_factor`` - measured pillars / 3: P1 gate battery, P2 four-year paper-trade
                      report, P3 synthetic hypothesis scenarios. A pillar whose artifacts
                      are absent is MISSING and contributes 0 - it is never filled in.
* ``risk_factor``   - 1 only when every declared risk limit is measured against the
                      paper-trade record and respected; 0 on any breach, and 0 on a limit
                      whose source publishes no number, or publishes it on a different
                      basis (UNMEASURED, named); MISSING when there is no record at all
* ``target_factor`` - protected monthly return / 10 % (the red apple), capped at 1. It is
                      diagnostic-only (contributes 0) on a window that is not protected.

Nothing here mints authority: the verdict stays NO_ECONOMIC_CLAIM and the report names
exactly which measurement is missing next.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from v8_next.evaluation.benchmark_receipt import (
    EVIDENCE_CLASS_UNDECLARED,
    NO_EVIDENTIAL_LEDGER_ENTRY,
)

#: declared risk limits for the paper-trade pillar (breach => risk factor 0). The limits are
#: fractions/multiples; a measured excursion published as a percentage is normalised before
#: the comparison (#449). #470: the limit and the observation must also share a *basis* -- a
#: capital fraction may not be compared against a return-sum percentage -- so a limit with
#: no same-basis number is UNMEASURED (named ``RISK_LIMIT_BASIS_MISMATCH``), never a
#: vacuous BREACH or a vacuous RESPECTED.
RISK_LIMITS = {
    "max_drawdown": -0.25,
    "max_fee_drag_vs_gross": 2.0,
    "max_exposure": 1.0,
}

#: the basis each declared limit above is stated in (#470). ``capital_fraction`` is a
#: fraction of the capital base; ``ratio`` is a multiple of a measured reference quantity
#: (fee cost against gross return). A number is compared only against the limit declared in
#: the basis that number is published on.
LIMIT_BASES: dict[str, str] = {
    "max_drawdown": "capital_fraction",
    "max_fee_drag_vs_gross": "ratio",
    "max_exposure": "capital_fraction",
}

#: the basis a source declares when it measures an excursion against its own return-sum
#: path: percent of that path's own running peak, in return-sum units. It is not the capital
#: fraction the drawdown limit is declared in, so the two numbers are not one quantity.
RETURN_SUM_BASIS = "return_sum_percent"

#: a published number whose source declares no basis at all (#470): the reader names that
#: instead of assuming the number is on the limit's basis.
BASIS_UNKNOWN = "unknown"

#: named reason a declared limit is left UNMEASURED: the numbers read are published on a
#: basis the limit is not declared in, so no BREACH/RESPECTED cell may be derived (#470).
RISK_LIMIT_BASIS_MISMATCH = "RISK_LIMIT_BASIS_MISMATCH"

#: the red apple: the far target, never a claim
RED_APPLE_MONTHLY_RETURN = 0.10

#: artifacts that prove each pillar exists and was measured
PILLAR_ARTIFACTS = {
    "P1_gate_battery": ("docs/evidence/v87-r2/NX09/fold_results.json",),
    "P2_paper_trade_4y": (
        "docs/evidence/v87-r3/RECONCILE/engine_replay_reconciliation.json",
        "docs/evidence/v87-r3/COST/cost_lane.json",
        "docs/evidence/v87-r3/PAPER_4Y/paper_trade_4y.json",
    ),
    "P3_synthetic_scenarios": (
        "docs/evidence/v87-r3/SCENARIOS/scenario_results.json",
    ),
}


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _ledger_read_line(publication: Any | None, receipt: Any | None) -> str:
    """One line naming the entry the audit read and the class it carries (#448)."""
    if publication is not None:
        return str(publication.describe())
    if receipt is None:
        return "no ledger receipt read"
    evidence_class = (
        receipt.evidence_class()
        if hasattr(receipt, "evidence_class")
        else EVIDENCE_CLASS_UNDECLARED
    )
    digest = getattr(receipt, "receipt_digest", "")
    version = getattr(receipt, "digest_version", "")
    return (
        f"evidence class: {evidence_class}  receipt: {digest} (digest_version={version}) "
        "[no ledger read behind this call]"
    )


def _vector_publication_refusal(publication: Any | None, receipt: Any | None) -> str:
    """Named reason the read entry's gate vector may not be published (#448).

    Empty when the entry declares an evidential window class. A receipt that declares no
    class, or one that proves no economic evidence, contributes a named MISSING factor
    instead of a PASS-shaped vector: a smoke/liveness vector is not a measurement of the
    gates, and #444's rule for the number applies to the vector that travels with it.
    """
    if publication is not None:
        return str(getattr(publication, "refusal_reason", "") or "")
    if receipt is None:
        return ""
    declares = getattr(receipt, "declares_evidential_window", None)
    if declares is None or declares():
        return ""
    evidence_class = (
        receipt.evidence_class()
        if hasattr(receipt, "evidence_class")
        else EVIDENCE_CLASS_UNDECLARED
    )
    return (
        f"{NO_EVIDENTIAL_LEDGER_ENTRY}: the gate vector is published only from an entry that "
        f"declares an evidential window class (window_evidence.economic_evidence=true); the "
        f"entry read carries class {evidence_class} "
        f"(digest_version={getattr(receipt, 'digest_version', '')}), so its vector is not "
        "published"
    )


#: the readiness audit resolves the battery on a real window; when that artifact is present
#: it is the better source than a ledger entry whose gate vector was never resolved
GATE_AUDIT_REL = "docs/evidence/v87/READINESS/readiness_audit.json"

#: #458. Named reason the resolved battery's vector may not be published: the artifact was
#: not written by the producer that owns this cell, so no measurement stands behind its
#: states. It is a refusal, not a deletion -- the artifact stays on disk as read-only
#: evidence and is named every time the factor is read.
GATE_AUDIT_CONTRACT_MISMATCH = "GATE_AUDIT_CONTRACT_MISMATCH"

#: the structural cells the current producer publishes beside the input they were derived
#: from and the named reason their state carries (``tools/nx_readiness_audit.py``, #447).
GATE_AUDIT_CONTRACT_FIELDS = ("g0_identity", "g1_causal_pit", "g2_determinism_ledger")

#: the producer's own vocabulary for "this input was not measured". An entry that names one
#: of these is not a measurement, so the same cell may not be published as PASS (#458.d).
GATE_AUDIT_MEASURED_STATUS = "MEASURED"


def _gate_audit_contract_refusal(audit_artifact: dict[str, Any]) -> str:
    """Named reason the resolved battery's vector may not be published (#458), else ``""``.

    The battery artifact is a measurement this reader may count only when the producer
    that owns it says what the vector was measured from: ``gate_coverage.structural_gates``
    carries, for each of the three structural cells, the input and the named reason its
    state carries (#447). An artifact without that block -- e.g. one written before #447 --
    publishes ``g1_causal_pit = PASS`` / ``g2_determinism_ledger = PASS`` with no
    measurement this path can stand behind, so its vector is withheld and the mismatch is
    named instead of counted.

    The reader does not re-derive the states (no second ontology); it checks the artifact
    against itself: every structural entry must name a reason and a status, and a cell the
    entry itself names as not measured may not be published as PASS.
    """
    coverage = audit_artifact.get("gate_coverage")
    if not isinstance(coverage, dict) or not coverage:
        return (
            f"{GATE_AUDIT_CONTRACT_MISMATCH}: {GATE_AUDIT_REL} publishes no 'gate_coverage' "
            "block, so it carries no gate vector to publish"
        )
    structural = coverage.get("structural_gates")
    if not isinstance(structural, dict) or not structural:
        counted = len(coverage.get("states") or {})
        return (
            f"{GATE_AUDIT_CONTRACT_MISMATCH}: {GATE_AUDIT_REL} publishes no "
            "'gate_coverage.structural_gates' block, so the g0/g1/g2 cells behind its "
            f"{counted} counted states carry no measured input and no named reason (the "
            "#447 producer contract); no cell is published from it"
        )
    raw_states = coverage.get("states")
    states: dict[str, Any] = raw_states if isinstance(raw_states, dict) else {}
    for field in GATE_AUDIT_CONTRACT_FIELDS:
        entry = structural.get(field)
        if not isinstance(entry, dict) or not entry:
            return (
                f"{GATE_AUDIT_CONTRACT_MISMATCH}: {GATE_AUDIT_REL} publishes a "
                f"'gate_coverage.structural_gates' block without a '{field}' entry, so that "
                "cell carries no measured input and no named reason (the #447 producer "
                "contract); no cell is published from it"
            )
        reason = entry.get("reason")
        status = entry.get("status")
        if not isinstance(reason, str) or not reason.strip():
            return (
                f"{GATE_AUDIT_CONTRACT_MISMATCH}: {GATE_AUDIT_REL} publishes a "
                f"'gate_coverage.structural_gates.{field}' entry without a named reason, so "
                "its state stands without a measurement; no cell is published from it"
            )
        if not isinstance(status, str) or not status.strip():
            return (
                f"{GATE_AUDIT_CONTRACT_MISMATCH}: {GATE_AUDIT_REL} publishes a "
                f"'gate_coverage.structural_gates.{field}' entry without a status, so its "
                "state stands without a measurement; no cell is published from it"
            )
        if states.get(field) == "PASS" and status != GATE_AUDIT_MEASURED_STATUS:
            return (
                f"{GATE_AUDIT_CONTRACT_MISMATCH}: {GATE_AUDIT_REL} publishes "
                f"{field} = PASS while its own producer entry names status {status!r} "
                f"({reason}), so the PASS is not carried by a measurement; no cell is "
                "published from it"
            )
    return ""


def gate_factor(
    receipt: Any | None,
    repo_root: Path | None = None,
    *,
    publication: Any | None = None,
) -> dict[str, Any]:
    """PASS gates / 10, from the resolved battery when one exists, else from the receipt.

    #448: the receipt leg publishes a factor only from an entry that declares an
    evidential window class. A receipt that declares no class -- or one whose class proves
    no economic evidence -- is named, and its vector contributes a MISSING/0.0 factor
    rather than a PASS-shaped one.

    #458: the resolved-battery leg is a measurement of *this* reader's producer only when
    the artifact carries that producer's contract (``gate_coverage.structural_gates``, the
    g0/g1/g2 input and the named reason behind each state, #447). An artifact that does not
    -- e.g. the pre-#447 battery, which publishes ``g1_causal_pit = PASS`` and
    ``g2_determinism_ledger = PASS`` with nothing measured behind them -- is REFUSED by
    name and contributes no cell; the refusal is never routed around through the ledger
    receipt leg, which would publish a different measurement as this one.

    Either way the audit reports which ledger entry it read and the class that entry
    carries, so the factor is never read apart from the class it came from.
    """
    refusal = _vector_publication_refusal(publication, receipt)
    ledger_read = _ledger_read_line(publication, receipt)
    resolved: dict[str, Any] | None = None
    artifact_refusal = ""
    if repo_root is not None:
        audit_artifact = _load(repo_root / GATE_AUDIT_REL)
        if audit_artifact:
            artifact_refusal = _gate_audit_contract_refusal(audit_artifact)
            if not artifact_refusal:
                coverage = audit_artifact.get("gate_coverage") or {}
                states = coverage.get("states") or {}
                if states:
                    resolved = {
                        "factor": round(
                            float(coverage.get("passed", 0)) / max(1, len(states)), 4
                        ),
                        "passed": int(coverage.get("passed", 0)),
                        "required": len(states),
                        "states": states,
                        "status": "MEASURED",
                        "source": GATE_AUDIT_REL,
                        "verdict": coverage.get("readiness_status"),
                        "window": (audit_artifact.get("window") or {}).get("start_utc"),
                        "ledger_read": ledger_read,
                        "ledger_publication": _publication_dict(publication),
                    }
    if resolved is not None:
        return resolved
    if artifact_refusal:
        # #458: the preferred source is on disk but stands under no measurement this path
        # can reproduce, so its vector is withheld by name and no cell is counted from it.
        return {
            "factor": 0.0,
            "passed": 0,
            "required": 10,
            "states": {},
            "status": "REFUSED",
            "source": f"{GATE_AUDIT_REL} (refused: producer contract mismatch)",
            "refusal": artifact_refusal,
            "ledger_read": ledger_read,
            "ledger_publication": _publication_dict(publication),
        }
    if receipt is None:
        return {
            "factor": 0.0,
            "passed": 0,
            "required": 10,
            "states": {},
            "status": "MISSING",
            "ledger_read": ledger_read,
            "ledger_publication": _publication_dict(publication),
        }
    if refusal:
        # the vector is not published: a non-evidential (or class-undeclared) receipt
        # carries no measurement of the gates, so nothing is counted from it
        return {
            "factor": 0.0,
            "passed": 0,
            "required": 10,
            "states": {},
            "status": "MISSING",
            "source": "ledger receipt (refused: no evidential window class)",
            "refusal": refusal,
            "ledger_read": ledger_read,
            "ledger_publication": _publication_dict(publication),
        }
    gates = receipt.gates
    fields = list(gates.__class__.model_fields)
    states = {name: getattr(gates, name).name for name in fields}
    passed = sum(1 for state in states.values() if state == "PASS")
    return {
        "factor": round(passed / len(fields), 4),
        "passed": passed,
        "required": len(fields),
        "states": states,
        "status": "MEASURED",
        "source": "ledger receipt (gate vector as recorded)",
        "refusal": "",
        "ledger_read": ledger_read,
        "ledger_publication": _publication_dict(publication),
    }


def _publication_dict(publication: Any | None) -> dict[str, Any] | None:
    """The ledger read as data, when the caller supplied one (#448)."""
    if publication is None or not hasattr(publication, "as_dict"):
        return None
    data = publication.as_dict()
    return dict(data) if isinstance(data, dict) else None


def pillar_factor(repo_root: Path) -> dict[str, Any]:
    """Measured pillars / 3; an absent pillar is MISSING, never a silent zero."""
    pillars: dict[str, Any] = {}
    measured = 0
    for name, artifacts in PILLAR_ARTIFACTS.items():
        present = [rel for rel in artifacts if (repo_root / rel).is_file()]
        missing = [rel for rel in artifacts if rel not in present]
        if present and not missing:
            declared_pass = True
            for rel in present:
                data = _load(repo_root / rel) or {}
                if "passed" in data and not data["passed"]:
                    declared_pass = False
            if not declared_pass:
                pillars[name] = {
                    "status": "PRESENT_BUT_NOT_PASSING",
                    "artifacts": present,
                    "note": "the artifact exists but declares passed=false; it is not counted",
                }
                continue
            measured += 1
            pillars[name] = {"status": "MEASURED", "artifacts": present}
        elif present:
            pillars[name] = {"status": "PARTIAL", "artifacts": present, "missing": missing}
        else:
            pillars[name] = {"status": "MISSING", "missing": missing}
    total = len(PILLAR_ARTIFACTS)
    return {
        "factor": round(measured / total, 4),
        "measured": measured,
        "required": total,
        "pillars": pillars,
    }


#: The source every declared risk limit is read from, by name (#449). A limit whose source
#: publishes no number in the P2 artifact set is reported UNMEASURED and contributes 0.0:
#: an unmeasured quantity is never published as respected. Two of these limits were
#: consulted by no code at all before #449. #470: the basis each alternative source
#: publishes on, so a number on another basis is never read as this limit's measurement.
LIMIT_SOURCES: dict[str, str] = {
    "max_drawdown": (
        "families.<policy>.robustness_vector.max_adverse_excursion_pct (percent of the "
        "policy's own return-sum path -- not a capital fraction), or a published key "
        "containing 'drawdown' (fraction of the capital base)"
    ),
    "max_fee_drag_vs_gross": (
        "abs(fee_cost_return_sum) / abs(gross_return_sum) for the same policy"
    ),
    "max_exposure": "exposure or average_exposure (fraction)",
}


def _number(value: Any) -> float | None:
    """The published number, or None when the slot holds no measurement."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_fraction(value: Any, unit: str) -> float | None:
    """One comparison unit (#449): a published value expressed as a fraction."""
    number = _number(value)
    if number is None:
        return None
    return number / 100.0 if unit == "percent" else number


def _family_entries(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Both spellings the record uses: ``families`` (4-year report) and ``family`` (cost lane)."""
    entries: dict[str, dict[str, Any]] = {}
    for key in ("families", "family"):
        block = data.get(key)
        if isinstance(block, dict):
            for name, entry in block.items():
                if isinstance(entry, dict):
                    entries[str(name)] = entry
    return entries


def _declared_basis(*texts: Any) -> str | None:
    """The basis a source's own declaration names (#470), or None when it names neither.

    A declaration is the artifact's own unit/basis text, read as published -- never inferred
    from the field's name. The P2 record declares ``percent of that path's own running peak,
    in return-sum units (not capital percent)`` for ``max_adverse_excursion_pct``, and that
    is a different quantity from the capital fraction the limit is stated in.
    """
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        lowered = text.lower()
        if "return-sum" in lowered or "return sum" in lowered or "not capital" in lowered:
            return RETURN_SUM_BASIS
        if "capital" in lowered:
            return "capital_fraction"
    return None


def _declared_basis_for(entry: dict[str, Any], field: str) -> str | None:
    """The basis one family entry declares for a published field (#470), or None."""
    texts: list[Any] = []
    vector_basis = entry.get("robustness_vector_basis")
    if isinstance(vector_basis, dict):
        declared = vector_basis.get(field)
        if isinstance(declared, dict):
            texts += [declared.get("unit"), declared.get("basis")]
    drawdown_basis = entry.get("drawdown_basis")
    if isinstance(drawdown_basis, dict):
        fields = drawdown_basis.get("fields")
        named = fields if isinstance(fields, list) else []
        if not named or field in named:
            texts += [drawdown_basis.get("unit"), drawdown_basis.get("basis")]
    return _declared_basis(*texts)


def _fraction_drawdown_basis(data: dict[str, Any], key: str) -> str:
    """The basis a fraction-unit ``drawdown`` key is published in (#470).

    A ``drawdown`` key published as a fraction is the unit the declared limit is stated in,
    unless the family that published it declares another basis for the field -- the
    declaration always wins over the field's name.
    """
    entry = _family_entries(data).get(key.split(".", 1)[0])
    if isinstance(entry, dict):
        declared = _declared_basis_for(entry, key.rsplit(".", 1)[-1])
        if declared is not None:
            return declared
    return LIMIT_BASES["max_drawdown"]


def _observation(
    artifact: str,
    source: str,
    published: Any,
    unit: str,
    measured: float,
    source_basis: str,
    limit_basis: str,
) -> dict[str, Any]:
    """One published number, named, in the unit the limit is compared in and on its basis.

    #470: ``basis`` is what the source publishes the number on and ``limit_basis`` is what
    the limit is declared in. Both travel with the observation so no reader has to assume
    the comparison was made between two quantities of the same kind.
    """
    return {
        "artifact": artifact,
        "source": source,
        "published": published,
        "unit": unit,
        "measured": measured,
        "basis": source_basis,
        "limit_basis": limit_basis,
    }


def _drawdown_observations(
    rel: str, data: dict[str, Any], metrics: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every drawdown the artifact publishes, each with the basis it publishes it on (#470)."""
    limit_basis = LIMIT_BASES["max_drawdown"]
    observations: list[dict[str, Any]] = []
    for key, value in metrics.items():
        if "drawdown" not in key:
            continue
        fraction = _as_fraction(value, "fraction")
        if fraction is not None:
            observations.append(
                _observation(
                    rel,
                    key,
                    value,
                    "fraction",
                    abs(fraction),
                    _fraction_drawdown_basis(data, key),
                    limit_basis,
                )
            )
    for policy, entry in _family_entries(data).items():
        vector = entry.get("robustness_vector")
        if not isinstance(vector, dict):
            continue
        published = vector.get("max_adverse_excursion_pct")
        fraction = _as_fraction(published, "percent")
        if fraction is None:
            continue
        source = f"families.{policy}.robustness_vector.max_adverse_excursion_pct"
        # A1: the source's own declared basis, or "unknown" when it declares none -- a
        # percent excursion is never assumed to be a capital fraction
        basis = _declared_basis_for(entry, "max_adverse_excursion_pct") or BASIS_UNKNOWN
        observations.append(
            _observation(rel, source, published, "percent", abs(fraction), basis, limit_basis)
        )
    return observations


def _fee_drag_observations(rel: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Published fee cost against published gross return, per policy and slippage basis."""
    observations: list[dict[str, Any]] = []
    for policy, entry in _family_entries(data).items():
        rows: list[tuple[str, dict[str, Any]]] = [("", entry)]
        totals_by_basis = entry.get("totals_by_slippage_fraction")
        if isinstance(totals_by_basis, dict):
            for basis, totals in totals_by_basis.items():
                if isinstance(totals, dict):
                    rows.append((f".totals_by_slippage_fraction.{basis}", totals))
        for suffix, row in rows:
            fee = _number(row.get("fee_cost_return_sum"))
            gross = _number(row.get("gross_return_sum"))
            if fee is None or gross is None or gross == 0.0:
                continue
            ratio = abs(fee) / abs(gross)
            source = f"families.{policy}{suffix}: abs(fee_cost_return_sum)/abs(gross_return_sum)"
            observations.append(
                _observation(
                    rel,
                    source,
                    ratio,
                    "ratio",
                    ratio,
                    LIMIT_BASES["max_fee_drag_vs_gross"],
                    LIMIT_BASES["max_fee_drag_vs_gross"],
                )
            )
    return observations


def _exposure_observations(rel: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """A published exposure fraction, when one exists anywhere in the artifact."""
    observations: list[dict[str, Any]] = []
    limit_basis = LIMIT_BASES["max_exposure"]
    scopes: list[tuple[str, dict[str, Any]]] = [("", data)]
    for policy, entry in _family_entries(data).items():
        scopes.append((f"families.{policy}.", entry))
    for prefix, scope in scopes:
        for key in ("exposure", "average_exposure"):
            published = scope.get(key)
            fraction = _as_fraction(published, "fraction")
            if fraction is None:
                continue
            basis = _declared_basis_for(scope, key) or limit_basis
            observations.append(
                _observation(
                    rel, f"{prefix}{key}", published, "fraction", abs(fraction), basis, limit_basis
                )
            )
    return observations


def risk_factor(repo_root: Path, paper_artifacts: Sequence[str]) -> dict[str, Any]:
    """Declared risk limits against the measured paper-trade record, fail-closed (#449/#470).

    Every limit in ``RISK_LIMITS`` is measured from the source ``LIMIT_SOURCES`` names it
    from, in one unit (a percentage excursion is normalised to a fraction) *and* on one
    basis: a number is compared only against the limit declared in the basis that number is
    published on. The P2 record measures its excursion as a percent of its own return-sum
    path, which is not the capital fraction ``max_drawdown`` is declared in, so the two are
    not one quantity: that limit is ``UNMEASURED``, named ``RISK_LIMIT_BASIS_MISMATCH``, and
    contributes 0.0 -- it is never published as a BREACH or a RESPECTED derived from a
    mismatched pair. A limit whose source publishes no number is likewise ``UNMEASURED``,
    named in ``unmeasured``. A breach is named with the limit, the normalised value and the
    source that broke it. The factor is 1.0 only when every declared limit is measured on its
    own basis and respected.
    """
    metrics: dict[str, Any] = {}
    observations: dict[str, list[dict[str, Any]]] = {name: [] for name in RISK_LIMITS}
    read: list[str] = []
    for rel in paper_artifacts:
        data = _load(repo_root / rel)
        if not isinstance(data, dict):
            continue
        read.append(rel)
        local: dict[str, Any] = {}
        for key in ("max_drawdown", "fee_cost_return_sum", "net_return_measured_only_sum",
                    "exposure", "average_exposure"):
            if key in data:
                local[key] = data[key]
            if "family" in data:
                for policy, entry in data["family"].items():
                    for basis, totals in (entry.get("totals_by_slippage_fraction") or {}).items():
                        local[f"{policy}.slippage_{basis}.max_drawdown"] = totals.get("max_drawdown")
                        local[f"{policy}.slippage_{basis}.net_measured"] = totals.get(
                            "net_return_measured_only_sum"
                        )
        metrics.update(local)
        observations["max_drawdown"] += _drawdown_observations(rel, data, local)
        observations["max_fee_drag_vs_gross"] += _fee_drag_observations(rel, data)
        observations["max_exposure"] += _exposure_observations(rel, data)
    if not read:
        # no record was read at all: the module contract's MISSING, named
        return {
            "factor": 0.0,
            "status": "MISSING",
            "metrics": {},
            "breaches": [],
            "unmeasured": [f"{name}: no paper-trade record read" for name in RISK_LIMITS],
            "limits": RISK_LIMITS,
            "limit_sources": LIMIT_SOURCES,
            "limit_assessments": {
                name: {
                    "status": "UNMEASURED",
                    "limit": RISK_LIMITS[name],
                    "limit_basis": LIMIT_BASES[name],
                    "source": LIMIT_SOURCES[name],
                    "basis_pairs": [],
                    "observations": [],
                    "breaches": [],
                    "reason": "no paper-trade record was read",
                    "reason_detail": "",
                }
                for name in RISK_LIMITS
            },
            "artifacts_read": read,
        }
    breaches: list[str] = []
    unmeasured: list[str] = []
    assessments: dict[str, Any] = {}
    for name, limit in RISK_LIMITS.items():
        limit_basis = LIMIT_BASES[name]
        measured_rows = observations[name]
        # #470: only a number published on the limit's own basis may carry a verdict; a
        # number on another basis is recorded and named, never compared
        same_basis = [row for row in measured_rows if row["basis"] == limit_basis]
        other_basis = [row for row in measured_rows if row["basis"] != limit_basis]
        limit_breaches = [
            f"{name} {row['measured']:.8f} beyond {limit} "
            f"({row['artifact']}: {row['source']}, published {row['published']} as {row['unit']}, "
            f"basis {row['basis']} = the limit's own basis)"
            for row in same_basis
            if row["measured"] > abs(limit)
        ]
        reason = ""
        reason_detail = ""
        if limit_breaches:
            status = "BREACH"
            reason_detail = "the number is published on the limit's own basis and exceeds it"
            breaches += limit_breaches
        elif same_basis:
            status = "RESPECTED"
            reason_detail = "the number is published on the limit's own basis and is within it"
        else:
            status = "UNMEASURED"
            if other_basis:
                published_bases = ", ".join(sorted({row["basis"] for row in other_basis}))
                reason = RISK_LIMIT_BASIS_MISMATCH
                reason_detail = (
                    f"the limit is declared on basis {limit_basis}; every number read is "
                    f"published on basis {published_bases}, so no pair on one basis exists "
                    "and the two are not compared"
                )
                unmeasured.append(f"{name}: {reason} -- {reason_detail}")
            else:
                reason = f"no artifact read publishes {LIMIT_SOURCES[name]}"
                unmeasured.append(f"{name}: {reason}")
        assessments[name] = {
            "status": status,
            "limit": limit,
            "limit_basis": limit_basis,
            "source": LIMIT_SOURCES[name],
            # A1: the (limit basis, source basis) pair every observation was read as
            "basis_pairs": [
                {
                    "limit_basis": limit_basis,
                    "source_basis": row["basis"],
                    "artifact": row["artifact"],
                    "source": row["source"],
                    "matches": row["basis"] == limit_basis,
                }
                for row in measured_rows
            ],
            "observations": measured_rows,
            "breaches": limit_breaches,
            "reason": reason,
            "reason_detail": reason_detail,
        }
    status = "BREACH" if breaches else ("UNMEASURED" if unmeasured else "RESPECTED")
    return {
        "factor": 0.0 if status != "RESPECTED" else 1.0,
        "status": status,
        "metrics": metrics,
        "breaches": breaches,
        "unmeasured": unmeasured,
        "limits": RISK_LIMITS,
        "limit_sources": LIMIT_SOURCES,
        "limit_assessments": assessments,
        "artifacts_read": read,
    }


def target_factor(repo_root: Path) -> dict[str, Any]:
    """Red-apple progress; counted only when the return came from a protected window."""
    protected = (repo_root / "docs/evidence/v87/NX01/burn_map.json").is_file() and bool(
        (_load(repo_root / "docs/evidence/v87/NX01/burn_map.json") or {}).get(
            "protected_final_available", False
        )
    )
    monthly: float | None = None
    cost_lane = _load(repo_root / "docs/evidence/v87-r3/COST/cost_lane.json")
    if cost_lane:
        months = max(
            1,
            round(
                (cost_lane.get("window", {}).get("window_bars", 744)) / (24 * 30),
            ),
        )
        totals = []
        for entry in (cost_lane.get("family") or {}).values():
            for row in (entry.get("totals_by_slippage_fraction") or {}).values():
                value = row.get("net_return_measured_only_sum")
                if isinstance(value, (int, float)):
                    totals.append(value)
        if totals:
            monthly = max(totals) / months
    if monthly is None:
        return {"factor": 0.0, "status": "MISSING", "monthly_return": None,
                "target_monthly_return": RED_APPLE_MONTHLY_RETURN}
    factor = 0.0 if not protected else min(1.0, max(0.0, monthly / RED_APPLE_MONTHLY_RETURN))
    return {
        "factor": round(factor, 4),
        "status": "COUNTED" if protected else "DIAGNOSTIC_ONLY_NOT_PROTECTED",
        "monthly_return": round(monthly, 6),
        "target_monthly_return": RED_APPLE_MONTHLY_RETURN,
        "note": "a target measured on an unprotected window is reported, never counted",
    }


#: declared tolerances for the regression comparison. A metric may not drop beyond its
#: tolerance; the readiness score may not drop at all.
REGRESSION_TOLERANCES = {
    "readiness": 0.0,
    "gate_passes": 0,
    "pillars_measured": 0,
    "net_measured_sum": 0.0005,
    "win_rate_pct": 0.5,
}

BASELINE_REL = "docs/evidence/v87-r3/BASELINE/readiness_baseline.json"

#: Declared corrections to a pinned baseline metric (#458 Target (3), decision D-166).
#:
#: The baseline is a landed artifact that is read, never silently re-pinned: a metric whose
#: pinned value counted something this path cannot measure is corrected by *declaring* the
#: ``superseded -> corrected`` pair here, together with the authority that decided it and the
#: named reason the pinned number was wrong. A declaration is honoured for the exact declared
#: pair only -- any further drop is still a REGRESSION, and a metric with no declared
#: correction is compared strictly against the pinned value.
DECLARED_CORRECTIONS: dict[str, dict[str, Any]] = {
    "gate_passes": {
        "superseded": 4,
        "corrected": 2,
        "authority": "D-166",
        "reason": (
            "the pinned 4 counted two cells as PASS that no measurement carried "
            "(g1_causal_pit, g2_determinism_ledger); resolve_structural_gates cannot emit "
            "PASS for them on this path, so the battery's own producer measures 2"
        ),
    },
}


def _declared_correction(
    declared: dict[str, Any] | None, before: Any, after: Any
) -> dict[str, Any] | None:
    """The declared correction for a metric, and only when the observed pair is exactly it."""
    if not declared:
        return None
    try:
        matched = (
            float(before) == float(declared["superseded"])
            and float(after) == float(declared["corrected"])
        )
    except (KeyError, TypeError, ValueError):
        return None
    return declared if matched else None


def snapshot(
    repo_root: Path,
    ledger_path: Path,
    latest_receipt: Any | None,
    *,
    publication: Any | None = None,
) -> dict[str, Any]:
    """The comparable state of a run: score, factors and the paper-trade metrics."""
    report = audit(repo_root, ledger_path, latest_receipt, publication=publication)
    risk = report["factors"]["risk_factor"]
    paper = _load(repo_root / PILLAR_ARTIFACTS["P2_paper_trade_4y"][2]) or {}
    per_policy = {
        name: {
            "net_measured_sum": entry.get("net_measured_sum"),
            "win_rate_pct": entry.get("win_rate_pct"),
            "campaigns": entry.get("campaigns"),
            "conservation_verified": entry.get("conservation_verified"),
        }
        for name, entry in (paper.get("families") or {}).items()
    }
    return {
        "readiness": report["readiness"],
        "gate_passes": report["factors"]["gate_factor"]["passed"],
        "pillars_measured": report["factors"]["pillar_factor"]["measured"],
        "risk_factor": report["factors"]["risk_factor"]["factor"],
        # #449: the baseline carries the per-limit verdict, so a breached or unmeasured
        # limit travels in the published artifact instead of a bare factor.
        "risk_limits": {
            "status": risk["status"],
            "breaches": risk["breaches"],
            "unmeasured": risk["unmeasured"],
            "assessments": {
                name: {
                    "status": row["status"],
                    "limit": row["limit"],
                    "source": row["source"],
                }
                for name, row in risk["limit_assessments"].items()
            },
        },
        "target_factor": report["factors"]["target_factor"]["factor"],
        "per_policy": per_policy,
        "claim_status": report["claim_status"],
    }


#: the metrics the regression comparison reads, by name: a top-level metric or a
#: ``<policy>.<metric>`` paper-trade cell. A declared correction naming anything else would
#: never be consulted, and a correction that cannot be read is not a correction, so it is
#: rejected by name rather than silently ignored.
COMPARED_METRICS = (
    "readiness",
    "gate_passes",
    "pillars_measured",
    "net_measured_sum",
    "win_rate_pct",
)


def _compare_metric(
    name: str,
    before: Any,
    after: Any,
    declared: dict[str, Any] | None,
    *,
    digits: int,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Compare one metric: (finding, regression line or None, honoured correction or None)."""
    tolerance = REGRESSION_TOLERANCES[name.rpartition(".")[2]]
    delta = round(float(after) - float(before), digits)
    correction = _declared_correction(declared, before, after)
    if correction is not None:
        # the declared delta, named with its authority: recorded and printed, never absorbed
        # and never a verdict -- the value it supersedes stays a property of this record
        record = {
            "metric": name,
            "superseded": correction["superseded"],
            "corrected": correction["corrected"],
            "authority": correction["authority"],
            "reason": correction["reason"],
        }
        finding = {"metric": name, "baseline": before, "current": after, "delta": delta,
                   "tolerance": tolerance, "regressed": False, "correction": record}
        return finding, None, record
    dropped = delta < -abs(tolerance)
    finding = {"metric": name, "baseline": before, "current": after, "delta": delta,
               "tolerance": tolerance, "regressed": dropped}
    return finding, (f"{name} {before} -> {after}" if dropped else None), None


def regression_against(
    baseline: dict[str, Any],
    current: dict[str, Any],
    corrections: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per-metric deltas and a REGRESSION verdict; nothing is re-baselined here.

    A metric covered by a declared correction (:data:`DECLARED_CORRECTIONS`, or the
    ``corrections`` argument, which may be passed empty to compare strictly) is compared
    against its declared pair instead of the superseded value: the declared delta is
    reported as a CORRECTION with its authority and named reason, it does not set the
    verdict, and a drop beyond the declared pair is still a REGRESSION. Every other metric
    is compared strictly, and the baseline is never moved.
    """
    declared = DECLARED_CORRECTIONS if corrections is None else corrections
    for name in declared:
        cell = name.rpartition(".")[2]
        if name not in COMPARED_METRICS and cell not in COMPARED_METRICS:
            raise ValueError(
                f"declared correction names {name!r}, which this comparison never reads; "
                f"declarable metrics are {COMPARED_METRICS} (top level) or "
                "'<policy>.<metric>' paper-trade cells"
            )
    findings: list[dict[str, Any]] = []
    regressions: list[str] = []
    honoured: list[dict[str, Any]] = []
    for metric in ("readiness", "gate_passes", "pillars_measured"):
        before, after = baseline.get(metric), current.get(metric)
        if before is None or after is None:
            continue
        finding, regression, record = _compare_metric(
            metric, before, after, declared.get(metric), digits=6
        )
        findings.append(finding)
        if regression:
            regressions.append(regression)
        if record:
            honoured.append(record)
    for policy, before_row in (baseline.get("per_policy") or {}).items():
        after_row = (current.get("per_policy") or {}).get(policy)
        if not after_row:
            regressions.append(f"{policy}: missing from the current run")
            continue
        for metric in ("net_measured_sum", "win_rate_pct"):
            before, after = before_row.get(metric), after_row.get(metric)
            if before is None or after is None:
                continue
            finding, regression, record = _compare_metric(
                f"{policy}.{metric}", before, after, declared.get(f"{policy}.{metric}"), digits=8
            )
            findings.append(finding)
            if regression:
                regressions.append(regression)
            if record:
                honoured.append(record)
    return {
        "verdict": "REGRESSION" if regressions else "OK",
        "regressions": regressions,
        "findings": findings,
        "tolerances": REGRESSION_TOLERANCES,
        "corrections": honoured,
        "note": (
            "a regression is reported, never repaired by moving the baseline: re-pinning "
            "requires an entry in the decision register, and a count this tree cannot "
            "re-measure is declared as a correction with its authority rather than absorbed"
        ),
    }


def write_scenario_report(repo_root: Path, out_rel: str | None = None) -> Path:
    """Produce the P3 artifact: model candles + SNU ledger + the overfitting hypothesis test."""
    import hashlib

    from v8_next.evaluation.scenarios import scenario_results

    target = repo_root / (out_rel or PILLAR_ARTIFACTS["P3_synthetic_scenarios"][0])
    target.parent.mkdir(parents=True, exist_ok=True)
    results = scenario_results()
    results["artifact_sha256_self"] = "computed_after_write"
    payload = json.dumps(results, indent=2, sort_keys=True, default=str) + "\n"
    target.write_text(payload)
    digest = hashlib.sha256(payload.replace("computed_after_write", "").encode()).hexdigest()
    results["artifact_sha256_self"] = digest
    target.write_text(json.dumps(results, indent=2, sort_keys=True, default=str) + "\n")
    return target


def audit(
    repo_root: Path,
    ledger_path: Path,
    latest_receipt: Any | None,
    *,
    publication: Any | None = None,
) -> dict[str, Any]:
    gates = gate_factor(latest_receipt, repo_root, publication=publication)
    pillars = pillar_factor(repo_root)
    risk = risk_factor(repo_root, PILLAR_ARTIFACTS["P2_paper_trade_4y"])
    target = target_factor(repo_root)
    readiness = round(
        100.0 * gates["factor"] * pillars["factor"] * risk["factor"] * target["factor"], 2
    )
    # when every pillar is present the next requirement is no longer a pillar: it is the
    # unresolved gates, and failing that a protected window for the target factor
    unresolved_gates = [
        name for name, state in gates["states"].items() if state != "PASS"
    ]
    if any(info["status"] != "MEASURED" for info in pillars["pillars"].values()):
        next_measurement = next(
            name for name, info in pillars["pillars"].items() if info["status"] != "MEASURED"
        )
    elif gates["status"] != "MEASURED":
        # #458: a gate vector that was not published is the next thing to measure. Naming a
        # later requirement here would read as if the gates had been resolved -- the gate
        # factor is 0.0 because nothing was published, not because the cells were counted.
        next_measurement = (
            f"gate_battery ({gates['status']}): no gate vector was published "
            "(refusal in factors.gate_factor.refusal)"
        )
    elif unresolved_gates:
        next_measurement = "gate_battery: " + ", ".join(unresolved_gates)
    elif target["status"] != "COUNTED":
        next_measurement = "protected_final_window (the target factor cannot be counted without one)"
    else:
        next_measurement = None
    return {
        "readiness": readiness,
        "factors": {
            "gate_factor": gates,
            "pillar_factor": pillars,
            "risk_factor": risk,
            "target_factor": target,
        },
        "formula": "100 * gate_factor * pillar_factor * risk_factor * target_factor",
        "next_required_measurement": next_measurement,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "ledger": str(ledger_path),
        #: #448: which entry was read and the class it carries, so the gate factor is
        #: never read apart from the class of the entry that produced it.
        "ledger_read": gates.get("ledger_read", ""),
        "ledger_publication": gates.get("ledger_publication"),
        "non_authority": (
            "the four factors are printed together on purpose: the composite is an audit of "
            "measured state and grants no capital or publication authority"
        ),
    }
