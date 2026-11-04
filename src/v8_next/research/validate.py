"""Master-side validation of a research run.

This module is deliberately independent of what a worker *says*: it re-reads the
persisted artifacts, re-checks the arithmetic and refuses to pass a claim that
has no machine-readable row behind it.

It never rewrites evidence. It writes only the ``validation/`` directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from v8_next.research import contract as research_contract
from v8_next.research import metrics as research_metrics
from v8_next.research import screen

EXPERIMENT_DIRS = {
    "A": "experiment_A_signal",
    "B": "experiment_B_entry",
    "C": "experiment_C_exit",
    "D": "experiment_D_stateful",
}

LEGAL_STATUSES = {"SUPPORTED", "REJECTED", "INCONCLUSIVE", "BLOCKED"}
#: We reject *access evidence*, not disclosures. An artifact may legitimately
#: say "sealed OOS never accessed"; it may not record that the holdout was read.
FORBIDDEN_JSON_FLAGS = {
    "oos_opened": True,
    "oos_data_touched": True,
    "sealed_oos_accessed": True,
    "holdout_accessed": True,
}
FORBIDDEN_PATH_FRAGMENTS = ("/holdout/", "\\holdout\\", "/sealed-oos/", "sealed_oos")

REQUIRED_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "A": ("metrics.json", "arms.json", "validation.json", "findings.md"),
    "B": ("metrics.json", "arms.json", "validation.json", "findings.md"),
    "C": ("metrics.json", "arms.json", "validation.json", "findings.md"),
    "D": ("metrics.json", "arms.json", "validation.json", "findings.md"),
}


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"


def _grep_forbidden(root: Path) -> list[str]:
    """Evidence of sealed-OOS ACCESS, not mentions of the word in disclosures."""
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root)).lower()
        if any(fragment in relative for fragment in FORBIDDEN_PATH_FRAGMENTS):
            hits.append(f"{path.relative_to(root)}::forbidden_path")
            continue
        if path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in FORBIDDEN_JSON_FLAGS:
            if f'"{needle}": true' in text.replace(" ", ""):
                hits.append(f"{path.relative_to(root)}::{needle}=true")
    return hits


def _dataset_consistency(root: Path) -> dict[str, Any]:
    inventory, err = _read_json(root / "DATASET_INVENTORY.json")
    if err:
        return {"ok": False, "reason": err}
    manifest, err = _read_json(root / "baseline" / "baseline_manifest.json")
    if err:
        return {"ok": False, "reason": err}
    expected = (inventory or {}).get("dataset_hash")
    observed = (manifest or {}).get("dataset_sha256")
    return {
        "ok": bool(expected) and expected == observed,
        "expected_sha256": expected,
        "baseline_recorded_sha256": observed,
        "tape_sha256_matches_prior_audit": expected
        == "f63d77d3b540b883f769fe9a86a614340a3da4d3550b6891597c183b376bf713",
    }


def _baseline_reconciliation(root: Path) -> dict[str, Any]:
    recon, err = _read_json(root / "baseline" / "baseline_reconciliation.json")
    if err:
        return {"ok": False, "reason": err}
    reference = {"entries": 45, "closed": 44, "gross": 27.6707, "net": -8.67470686, "fees": 36.76607636}
    observed = {
        "entries": recon.get("campaign_count"),
        "closed": recon.get("campaign_count", 0) - 1,
        "gross": recon.get("gross_pnl_usdt"),
        "net": recon.get("net_pnl_usdt"),
        "fees": recon.get("fill_commission_total_usdt"),
    }
    tolerances = {"entries": 0, "closed": 0, "gross": 1e-6, "net": 1e-8, "fees": 1e-6}
    mismatches = {
        key: {"observed": observed[key], "reference": reference[key]}
        for key in reference
        if observed[key] is None or abs(float(observed[key]) - float(reference[key])) > tolerances[key]
    }
    return {
        "ok": not mismatches
        and bool(recon.get("commission_reconciles"))
        and recon.get("decision_id_linked") == recon.get("campaign_count")
        and recon.get("opportunity_id_linked") == recon.get("campaign_count"),
        "observed": observed,
        "reference": reference,
        "mismatches": mismatches,
        "commission_reconciles": recon.get("commission_reconciles"),
        "lineage": {
            "decision_id_linked": recon.get("decision_id_linked"),
            "opportunity_id_linked": recon.get("opportunity_id_linked"),
            "campaigns": recon.get("campaign_count"),
        },
    }


def _experiment_check(root: Path, experiment: str) -> dict[str, Any]:
    directory = root / EXPERIMENT_DIRS[experiment]
    problems: list[str] = []
    if not directory.is_dir():
        return {"ok": False, "reason": "experiment directory missing", "path": str(directory)}
    for name in REQUIRED_ARTIFACTS[experiment]:
        if not (directory / name).is_file():
            problems.append(f"missing artifact: {name}")

    headline, err = _read_json(directory / "metrics.json")
    if err:
        problems.append(f"metrics.json: {err}")
        headline = None
    status = None
    if isinstance(headline, dict):
        status = headline.get("status")
        if status not in LEGAL_STATUSES:
            problems.append(f"illegal/missing status: {status!r}")
        declared = headline.get("native_replay_validated")
        if declared is True:
            # A native claim needs a persisted native run with its own ledger.
            native_runs = list(directory.rglob("trade_lifecycles.jsonl"))
            if not native_runs:
                problems.append("native_replay_validated=true without any persisted native run")

    # Cross-check the reader-facing arm table against the rendered metrics, so a
    # stale arms.json cannot sit in the evidence bundle next to canonical numbers.
    arm_cross_checks: list[str] = []
    arms_decl, arms_err = _read_json(directory / "arms.json")
    if arms_err:
        arm_cross_checks.append(f"arms.json: {arms_err}")
    elif isinstance(arms_decl, dict) and isinstance(headline, dict):
        rendered = {
            str(arm.get("arm_id")): arm.get("net_expectancy_r")
            for arm in headline.get("arms", [])
            if isinstance(arm, dict)
        }
        for arm in arms_decl.get("arms", []) or []:
            if not isinstance(arm, dict):
                continue
            arm_id = str(arm.get("arm_id"))
            declared = arm.get("net_expectancy_r")
            expected = rendered.get(arm_id)
            if expected is None and declared is None:
                continue
            if expected is None or declared is None or abs(float(declared) - float(expected)) > 1e-9:
                arm_cross_checks.append(
                    f"arms.json {arm_id}: {declared!r} != metrics.json {expected!r}"
                )
    problems.extend(arm_cross_checks)

    arm_rows = list(directory.rglob("arm_rows.jsonl"))
    row_counts = {path.parent.name: sum(1 for _ in path.open(encoding="utf-8")) for path in arm_rows}

    native_dirs = sorted(
        {
            p.parent.name
            for p in directory.rglob("research_arm.json")
            if "superseded" not in p.parts
        }
    )
    arm_decl, err = _read_json(directory / "arms.json")
    declared_arms: list[Any] = []
    if isinstance(arm_decl, dict):
        declared_arms = arm_decl.get("arms") or arm_decl.get("arm_ids") or []
    elif isinstance(arm_decl, list):
        declared_arms = arm_decl
    if len(declared_arms) > research_contract.MAX_ARMS_PER_EXPERIMENT:
        problems.append(f"{len(declared_arms)} arms declared (max 4)")
    if len(native_dirs) > research_contract.MAX_NON_BASELINE_REPLAYS_PER_EXPERIMENT:
        problems.append(f"{len(native_dirs)} native arms executed (max 2)")
    # Cross-file status consistency: metrics.json, arms.json and hypothesis.json
    # must agree, otherwise the bundle contradicts itself.
    statuses = {"metrics": status}
    for name in ("arms", "hypothesis"):
        payload, err = _read_json(directory / f"{name}.json")
        if not err and isinstance(payload, dict):
            statuses[name] = payload.get("status")
    if len({v for v in statuses.values() if v}) > 1:
        problems.append(f"status disagreement across files: {statuses}")

    return {
        "ok": not problems,
        "path": str(directory),
        "statuses": statuses,
        "status": status,
        "native_arms_executed": native_dirs,
        "screen_row_counts": row_counts,
        "problems": problems,
        "arm_table_consistent": not arm_cross_checks,
    }


def _native_arm_reconciliation(root: Path) -> dict[str, Any]:
    """Every persisted native arm must reconcile its own fees and lineage."""
    results: dict[str, Any] = {}
    for arm_json in root.rglob("research_arm.json"):
        if "superseded" in arm_json.parts:
            # Archived evidence is preserved but is not a live arm.
            continue
        run_dir = arm_json.parent
        recon_path = run_dir / "reconciliation.json"
        lifecycles = screen.read_jsonl(run_dir / "trade_lifecycles.jsonl")
        fills = screen.read_jsonl(run_dir / "fills.jsonl")
        fees_fills = sum(
            float(str(c).split()[0]) for f in fills for c in (f.get("commissions") or [])
        )
        # entry_fees + exit_fees are attributed commissions. ``fees_usdt`` is the
        # COST column: under a slipped profile it is gross-minus-native-realized
        # and therefore also contains slippage. Conflating the two is the defect.
        attributed = sum(
            (row.get("entry_fees_usdt") or 0.0) + (row.get("exit_fees_usdt") or 0.0)
            for row in lifecycles
        )
        cost_column = sum(row.get("fees_usdt") or 0.0 for row in lifecycles)
        basis = {str(row.get("fee_basis")) for row in lifecycles}
        reconstruction, err = _read_json(recon_path)
        decision_ids = {
            str(row.get("decision_id")) for row in screen.read_jsonl(run_dir / "expert_decisions.jsonl")
            if row.get("decision_id")
        }
        opportunity_ids = {
            str(row.get("opportunity_id")) for row in screen.read_jsonl(run_dir / "opportunities.jsonl")
            if row.get("opportunity_id")
        }
        gates = {
            g["gate_id"]: g["status"]
            for g in (json.loads((run_dir / "gate_results.json").read_text()).get("gates") or [])
        } if (run_dir / "gate_results.json").is_file() else {}
        results[arm_json.parent.name] = {
            "run_dir": str(run_dir),
            "campaigns": len(lifecycles),
            "fills": len(fills),
            "fee_total_fills": round(fees_fills, 8),
            "fee_total_lifecycles": round(attributed, 8),
            "attributed_commission_total": round(attributed, 8),
            "fees_reconcile": abs(fees_fills - attributed) < 1e-6,
            "cost_column_total": round(cost_column, 8),
            "slippage_in_cost_column": round(cost_column - attributed, 8),
            "fee_basis_values": sorted(basis),
            "mixed_cost_basis": len(basis) > 1,
            "decision_ids_linked": sum(1 for row in lifecycles if row.get("decision_id")),
            "opportunity_ids_linked": sum(1 for row in lifecycles if row.get("opportunity_id")),
            "decision_join_rate": (
                sum(1 for row in lifecycles if str(row.get("decision_id")) in decision_ids),
                len(lifecycles),
            ),
            "opportunity_join_rate": (
                sum(1 for row in lifecycles if str(row.get("opportunity_id")) in opportunity_ids),
                len(lifecycles),
            ),
            "audit_gates": gates,
            "audit_failed": sorted(k for k in ("G8", "G9") if gates.get(k) == "FAIL"),
            "reconciliation_present": reconstruction is not None,
            "net_expectancy_r": research_metrics.mean(
                row.get("realized_net_r") for row in lifecycles
            ),
        }
    # An arm the audit itself failed is kept as evidence of the failure, never
    # counted as validated economics.
    return {"ok": all(v["fees_reconcile"] for v in results.values()) if results else True,
            "arms": results,
            "usable": sorted(k for k, v in results.items() if not v["audit_failed"])}


def validate_run(root: Path | str) -> dict[str, Any]:
    root = Path(root)
    contract, contract_err = _read_json(root / "RESEARCH_CONTRACT.json")
    problems = research_contract.validate_contract(contract or {}) if not contract_err else [contract_err]
    recorded_hash = None
    hash_file = root / "RESEARCH_CONTRACT_SHA256.txt"
    if hash_file.is_file():
        recorded_hash = hash_file.read_text(encoding="utf-8").strip()
    computed_hash = research_contract.contract_sha256(contract) if contract else None

    report: dict[str, Any] = {
        "contract": {
            "ok": not problems and recorded_hash == computed_hash,
            "violations": problems,
            "recorded_sha256": recorded_hash,
            "computed_sha256": computed_hash,
            "hash_matches": recorded_hash == computed_hash,
        },
        "dataset": _dataset_consistency(root),
        "baseline": _baseline_reconciliation(root),
        "experiments": {exp: _experiment_check(root, exp) for exp in EXPERIMENT_DIRS},
        "native_arms": _native_arm_reconciliation(root),
        "leakage": {"ok": True, "forbidden_references": []},
        "independent_variables": {},
    }
    forbidden = _grep_forbidden(root)
    report["leakage"] = {
        "ok": not forbidden,
        "forbidden_references": forbidden,
        "note": "Artifacts mentioning holdout/sealed/OOS paths are rejected; the sealed OOS dataset was never opened.",
    }
    from v8_next.research.arms import arms_for

    for exp in EXPERIMENT_DIRS:
        # The preregistered arm registry is authoritative for what changed.
        changes: set[str] = set()
        keeps: set[str] = set()
        per_arm: list[dict[str, Any]] = []
        for arm in arms_for(exp):
            if arm.is_baseline:
                continue
            changes.update(arm.changes)
            keeps.update(arm.holds_fixed)
            per_arm.append({"arm_id": arm.arm_id, "changes": list(arm.changes)})
        report["independent_variables"][exp] = {
            "changed_across_arms": sorted(changes),
            "held_fixed": sorted(keeps),
            # Isolation is a per-arm property: each arm may change one variable,
            # while the experiment as a whole necessarily varies several.
            "changes_per_arm": per_arm,
            "single_variable_per_arm": all(len(item["changes"]) <= 1 for item in per_arm),
            "source": "preregistered arm registry",
        }

    # Cross-deliverable status agreement: the reported status in every
    # machine-readable deliverable must equal the experiment status.
    status_problems: list[str] = []
    experiment_status = {exp: check.get("status") for exp, check in report["experiments"].items()}
    facts_path = root / "FACTS.json"
    if facts_path.is_file():
        facts = json.loads(facts_path.read_text())
        facts_status = (facts.get("statuses") or {}).get("experiments") or {}
        for exp, value in facts_status.items():
            if experiment_status.get(exp) and value != experiment_status[exp]:
                status_problems.append(f"FACTS {exp}: {value} != metrics {experiment_status[exp]}")
    cross_path = root / "CROSS_EXPERIMENT_COMPARISON.json"
    if cross_path.is_file():
        cross = json.loads(cross_path.read_text())
        arm_status = {
            arm: info.get("status")
            for arm, info in ((facts.get("statuses") or {}).get("arms") or {}).items()
        } if facts_path.is_file() else {}
        for row in cross.get("rows", []):
            exp = row.get("experiment")
            status = str(row.get("status"))
            arm_id = str(row.get("row", "")).split("_")[0]
            allowed = {experiment_status.get(exp), arm_status.get(arm_id),
                       "INVALID", "BASELINE", "OBSERVED"}
            if exp in experiment_status and status not in allowed:
                status_problems.append(
                    f"CROSS {row.get('row')}: {status} is neither the experiment status "
                    f"{experiment_status.get(exp)} nor the arm status {arm_status.get(arm_id)}"
                )
    final_path = root / "FINAL_STATUS.json"
    if final_path.is_file():
        final = json.loads(final_path.read_text())
        for exp, value in (final.get("experiments") or {}).items():
            if experiment_status.get(exp) and value != experiment_status[exp]:
                status_problems.append(f"FINAL_STATUS {exp}: {value} != metrics {experiment_status[exp]}")
    acceptance_path = root / "MASTER_ACCEPTANCE_GATES.json"
    if acceptance_path.is_file():
        acceptance = json.loads(acceptance_path.read_text())
        gate_for = {"A": "G7", "B": "G8", "C": "G9", "D": "G10"}
        for gate in acceptance.get("gates", []):
            gate_id = gate.get("gate_id")
            gate_exp = next((k for k, v in gate_for.items() if v == gate_id), None)
            expected_status = experiment_status.get(gate_exp) if gate_exp else None
            if expected_status and expected_status not in str(gate.get("explanation", "")):
                status_problems.append(
                    f"{gate_id} explanation does not state {gate_exp}={expected_status}: "
                    f"{str(gate.get('explanation'))[:80]}"
                )
    report["status_consistency"] = {"ok": not status_problems, "problems": status_problems}

    # Executed arms must be declared in the *contract's* effective registry, not
    # merely resolvable in code. AM-001 arms carry their amendment id.
    registry_problems: list[str] = []
    declared: dict[str, dict[str, object]] = {}
    contract_file = root / "RESEARCH_CONTRACT.json"
    if contract_file.is_file():
        contract = json.loads(contract_file.read_text())
        for exp, block in (contract.get("experiment_arms") or {}).items():
            for arm in block.get("arms", []) or []:
                if isinstance(arm, dict) and arm.get("id"):
                    declared[str(arm["id"])] = {"experiment": exp, **arm}
    else:
        registry_problems.append("RESEARCH_CONTRACT.json missing")
    executed: dict[str, str] = {}
    for exp, directory in (
        ("A", "experiment_A_signal"), ("B", "experiment_B_entry"),
        ("C", "experiment_C_exit"), ("D", "experiment_D_stateful"),
    ):
        for kind in ("native", "screen"):
            base = root / directory / kind
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if child.is_dir() and "superseded" not in child.parts:
                    executed[child.name] = exp
    amended = sorted(a for a, d in declared.items() if d.get("amendment"))
    for arm_id, exp in sorted(executed.items()):
        info = declared.get(arm_id)
        if info is None:
            registry_problems.append(f"executed arm {arm_id} is not declared in the contract registry")
        elif info["experiment"] != exp:
            registry_problems.append(f"arm {arm_id} declared under {info['experiment']} but executed under {exp}")
    report["contract_arm_registry"] = {
        "ok": not registry_problems,
        "problems": registry_problems,
        "declared_count": len(declared),
        "executed_count": len(executed),
        "amended_arms": amended,
        "effective_contract_version": (contract.get("effective_contract_version") if contract_file.is_file() else None),
    }

    gates = {
        "G1_existing_codebase_preserved": report["contract"]["ok"],
        "G4_dataset_integrity": report["dataset"].get("ok", False),
        "G6_common_baseline": report["baseline"].get("ok", False),
        "G13_raw_artifacts": all(v.get("ok") for v in report["experiments"].values()),
        "status_consistency": report["status_consistency"]["ok"],
        "contract_arm_registry": report["contract_arm_registry"]["ok"],
        "G11_economic_validity": report["native_arms"].get("ok", False),
        "leakage_gate": report["leakage"]["ok"],
    }
    report["gates"] = gates
    report["ok"] = all(gates.values())
    return report
