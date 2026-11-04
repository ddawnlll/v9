"""Capture-prefix cash diagnostics; no imputation, inference or claim promotion."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.adapters.accounting_replay import replay_frozen_campaigns
from v8_next.app.paper import replay_account
from v8_next.domain.campaign import PaperCampaign
from v8_next.evaluation.cash_return import terminal_cash_return


def cash_trajectory(
    manifests: list[Path], config: dict[str, str], frozen_ns: int, decision_ns: int
) -> dict[str, Any]:
    """Caller verifies frozen policy/lineage before exposing this diagnostic.

    Each prefix uses only records received by that capture's cutoff. Later funding
    revisions cannot modify earlier rows. Full-prefix replay is intentionally
    simple and expensive; use this optional research path on bounded sessions.
    """
    previous_ns = frozen_ns
    previous: dict[str, Decimal | None] = {"breakout_baseline": Decimal(0), "squeeze": Decimal(0)}
    rows = []
    for count, manifest in enumerate(manifests, 1):
        metadata = json.loads(manifest.read_text())
        cutoff = max(int(a["received_time_ns"]) for a in metadata["artifacts"])
        if not 0 <= previous_ns < cutoff < decision_ns:
            raise ValueError("capture chronology crosses evaluation boundary")
        row: dict[str, Any] = {
            "start_ns": previous_ns,
            "end_ns": cutoff,
            "capture_prefix_count": count,
            "last_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
        for observer in previous:
            if config.get("experiment_window"):
                state = replay_account(
                    manifests[:count],
                    config,
                    observer=observer,
                    experiment_frozen_ns=frozen_ns,
                )
            else:
                state = replay_account(manifests[:count], config, observer=observer)
            campaigns = tuple(PaperCampaign.from_record(c) for c in state["campaigns"])
            account = replay_frozen_campaigns(manifests[:count], campaigns, config, cutoff)
            outcome = terminal_cash_return(account, Decimal(config["initial_balance"]))
            current = Decimal(outcome["return"]) if outcome["return"] is not None else None
            prior = previous[observer]
            row[observer] = {
                "cumulative": outcome,
                "incremental_cash_return": str(current - prior)
                if current is not None and prior is not None
                else None,
            }
            previous[observer] = current
        rows.append(row)
        previous_ns = cutoff
    return {
        "rows": rows,
        "comparison_policies": ["breakout_baseline", "squeeze"],
        "session_execution_policy": config.get("observer_policy", "squeeze"),
        "claim_status": "NO_ECONOMIC_CLAIM",
        "scope": "CAPTURE_PREFIX_CASH_DIAGNOSTIC_NOT_QUALIFIED_LOSS_SAMPLE",
        "initial_cash_change": "0",  # Funded simulator has no events before its first capture.
        "inference_eligible": False,
        "limitations": [
            "irregular_capture_intervals",
            "observed_funding_history_may_be_revised",
            "open_equity_marks_unqualified",
        ],
    }
