"""Read-only evaluation of recorded observations; no inference or promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from v8_next.adapters.binance_capture import verify


def evaluate(run: Path) -> dict[str, Any]:
    frozen = json.loads((run / "policy.json").read_text())
    policy_hash = hashlib.sha256(
        json.dumps(
            frozen["policy"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if policy_hash != frozen["policy_hash"]:
        raise ValueError("policy hash mismatch")
    connection = sqlite3.connect(
        f"{(run / 'research.sqlite').resolve().as_uri()}?mode=ro", uri=True
    )
    decisions = []
    try:
        # One SQLite snapshot binds access checks to the outcomes subsequently read.
        connection.execute("BEGIN")
        protected = connection.execute(
            "SELECT t.family,t.dataset_hash,t.registered_ns,b.burned_ns "
            "FROM trials t LEFT JOIN burns b ON b.lineage=t.family "
            "AND b.dataset_hash=t.dataset_hash WHERE t.role='HOLDOUT'"
        ).fetchall()
        now = time.time_ns()
        if any(
            burned is None or not registered <= burned <= now
            for _, _, registered, burned in protected
        ):
            raise ValueError(
                "protected holdout requires a prior lineage-specific consumption record"
            )
        for text, digest in connection.execute(
            "SELECT payload,digest FROM decisions ORDER BY decision_id"
        ):
            if hashlib.sha256(text.encode()).hexdigest() != digest:
                raise ValueError("decision hash mismatch")
            decision = json.loads(text)
            if decision["policy_hash"] != policy_hash:
                raise ValueError("decision belongs to another policy")
            if decision["claim_status"] != "NO_ECONOMIC_CLAIM":
                raise ValueError("unauthorized claim promotion")
            manifest = Path(decision["source_manifest"])
            if (
                hashlib.sha256(manifest.read_bytes()).hexdigest()
                != decision["source_manifest_sha256"]
            ):
                raise ValueError("source manifest hash mismatch")
            verify(manifest)
            decisions.append(decision)
        trials = [
            dict(
                zip(
                    ("trial_id", "family", "policy_hash", "dataset_hash", "role", "registered_ns"),
                    row,
                    strict=True,
                )
            )
            for row in connection.execute("SELECT * FROM trials ORDER BY family,trial_id")
        ]
        if any(t["policy_hash"] != policy_hash for t in trials):
            raise ValueError("trial belongs to another policy")
        lifecycle = [
            dict(
                zip(
                    ("opportunity_id", "sequence", "state", "decision_ns", "reason"),
                    row,
                    strict=True,
                )
            )
            for row in connection.execute(
                "SELECT * FROM lifecycle ORDER BY opportunity_id,sequence"
            )
        ]
        campaign_observations = []
        has_campaign_table = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_observations'"
            ).fetchone()
            is not None
        )
        if has_campaign_table:
            for identity, observed_ns, text, digest in connection.execute(
                "SELECT * FROM campaign_observations ORDER BY observed_ns,campaign_id"
            ):
                if hashlib.sha256(text.encode()).hexdigest() != digest:
                    raise ValueError("campaign observation hash mismatch")
                observation = json.loads(text)
                if observation.get("realization") != "SIMULATED":
                    raise ValueError("unauthorized campaign realization upgrade")
                if observation.get("campaign_id") != identity:
                    raise ValueError("campaign observation identity mismatch")
                campaign_observations.append({"observed_ns": observed_ns, **observation})
        burns = [
            dict(zip(("lineage", "dataset_hash", "burned_ns"), row, strict=True))
            for row in connection.execute("SELECT * FROM burns ORDER BY lineage,dataset_hash")
        ]
    finally:
        connection.close()
    return {
        "schema_version": 1,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "claim_class": "DIAGNOSTIC_SIGNAL",
        "policy_hash": policy_hash,
        "decision_count": len(decisions),
        "campaign_history": {
            "status": "RECORDED" if has_campaign_table else "UNAVAILABLE_LEGACY_SCHEMA",
            "observations": campaign_observations,
            "authority": "SIMULATED_SNAPSHOTS_NOT_VENUE_SETTLEMENT",
        },
        "research_lineage": {
            "trials": trials,
            "registered_family_sizes": dict(sorted(Counter(t["family"] for t in trials).items())),
            "holdout_burns": burns,
            "holdout_access": "PRIOR_CONSUMPTION_REQUIRED; NO_PRISTINE_HOLDOUT_CERTIFICATION",
            "scope": "LOCAL_REGISTER_NOT_PROOF_OF_COMPLETE_SEARCH_HISTORY",
            "multiplicity_adjustment": None,
        },
        "opportunity_book": {
            "unique_observed_count": len(
                {d["opportunity"]["opportunity_id"] for d in decisions if d.get("opportunity")}
            ),
            "lifecycle_events": lifecycle,
            "scope": "RECORDED_ECONOMIC_LIFECYCLE_NOT_ORDER_STATE",
        },
        "reasons": dict(sorted(Counter(d["reason"] for d in decisions).items())),
        "comparison": {
            "baseline_breakout_observations": sum(d["baseline"] == "BREAKOUT" for d in decisions),
            "squeeze_support_observations": sum(
                d["stance"]["kind"] == "SUPPORT" for d in decisions
            ),
            "unit": "recorded_snapshot_not_independent_opportunity_or_trial",
            "economic_comparison": None,
        },
        "statistical_evaluation": {
            "status": "NOT_COMPUTED_NO_QUALIFIED_OUTCOME_SAMPLE",
            "wrc": None,
            "spa": None,
            "dsr": None,
            "pbo": None,
        },
        "promotion": "BLOCKED_REQUIRED_EVIDENCE_AND_AUTHORITY_ABSENT",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.run_directory), indent=2))


if __name__ == "__main__":
    main()
