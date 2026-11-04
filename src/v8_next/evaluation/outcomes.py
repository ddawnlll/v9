"""Observed native campaign outcomes, not estimated edge or a calibration receipt."""

import hashlib
from decimal import Decimal
from typing import Any

import numpy as np
from nautilus_trader.model import Money

from v8_next.evaluation.store import canonical


def observed_outcomes(
    campaigns: list[dict[str, Any]],
    closures: list[dict[str, Any]],
    account: dict[str, Any],
    initial_balance: Decimal,
    *,
    economic_policy: dict[str, Any] | None = None,
    campaign_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Caller verifies/replays sources. Native net PnL already includes costs.

    Closed-position events retain separate campaigns when a netting cache ID is
    reused. Unfilled/open selections are kept as missing returns, never zeros.
    """
    if not initial_balance.is_finite() or initial_balance <= 0:
        raise ValueError("positive initial capital required")
    if account.get("currency") != "USDT" or account.get("realization") != "SIMULATED":
        raise ValueError("unsupported outcome account")
    by_id = {c["campaign_id"]: c for c in campaigns}
    if len(by_id) != len(campaigns):
        raise ValueError("duplicate outcome campaign")
    observations = {o["campaign_id"]: o for o in campaign_observations or []}
    if len(observations) != len(campaign_observations or []) or observations.keys() - by_id.keys():
        raise ValueError("duplicate or unknown campaign observation")
    terminal_without_entry = set()
    for key, observation in observations.items():
        if (
            observation["opportunity_id"] != by_id[key]["opportunity_id"]
            or observation["realization"] != "SIMULATED"
        ):
            raise ValueError("campaign observation identity mismatch")
        if observation["invalidated_before_submission"] or observation["expired_before_submission"]:
            if (
                observation["submitted"]
                or observation["entry_order"] is not None
                or observation["entry_events"]
                or observation["exit_orders"]
                or observation["position_closures"]
                or any(o["client_order_id"] == key for o in account["orders"])
            ):
                raise ValueError("terminal unsubmitted campaign has execution evidence")
            terminal_without_entry.add(key)
        entry = observation["entry_order"]
        if entry is not None and entry["status"] in {"REJECTED", "DENIED", "CANCELED", "EXPIRED"}:
            quantity = Decimal(entry["filled_qty"])
            if not quantity.is_finite() or quantity < 0:
                raise ValueError("invalid terminal entry filled quantity")
            if quantity == 0:
                matching = [o for o in account["orders"] if o["client_order_id"] == key]
                if (
                    matching != [entry]
                    or entry["instrument_id"] != by_id[key]["instrument_id"]
                    or observation["position_closures"]
                    or any(e["event_type"] == "OrderFilled" for e in observation["entry_events"])
                    or any(
                        o["status"] not in {"REJECTED", "DENIED", "CANCELED", "EXPIRED"}
                        or Decimal(o["filled_qty"]) != 0
                        for o in observation["exit_orders"]
                    )
                ):
                    raise ValueError("terminal zero-fill entry has contradictory native evidence")
                terminal_without_entry.add(key)
    closed = {}
    for event in closures:
        key = event["campaign_id"]
        if key not in by_id or key in closed or key in terminal_without_entry:
            raise ValueError("unknown or repeated campaign closure")
        campaign = by_id[key]
        if (
            event["instrument_id"] != campaign["instrument_id"]
            or event["opening_order_id"] != key
            or event["entry_side"] != ("BUY" if campaign["direction"] == "LONG" else "SELL")
            or not campaign["decision_ns"]
            < event["opened_ns"]
            <= event["closed_ns"]
            <= event["observed_ns"]
        ):
            raise ValueError("closure ownership or timing mismatch")
        allocated = Decimal(campaign["quantity"])
        peak = Decimal(event["peak_quantity"])
        if not allocated.is_finite() or not peak.is_finite() or not 0 < peak <= allocated:
            raise ValueError("native closure quantity exceeds campaign ownership or is invalid")
        closed[key] = event

    def cash(value: str) -> Decimal:
        money = Money.from_str(value)
        if str(money.currency) != "USDT":
            raise ValueError("outcome currency mismatch")
        return money.as_decimal()

    policy_hash = (
        hashlib.sha256(canonical(economic_policy).encode()).hexdigest() if economic_policy else None
    )
    rows, returns = [], []
    net_total = Decimal(0)
    for key, campaign in by_id.items():
        row: dict[str, Any] = {
            "campaign_id": key,
            "economic_policy_sha256": policy_hash,
            "opportunity_id": campaign["opportunity_id"],
            "decision_ns": campaign["decision_ns"],
            "decision_regime": campaign.get("decision_regime"),
            "instrument_id": campaign["instrument_id"],
            "direction": campaign["direction"],
            "status": "TERMINAL_WITHOUT_ENTRY"
            if key in terminal_without_entry
            else "NO_CLOSED_NATIVE_OUTCOME",
            "net_return_on_entry_notional": None,
            "initial_filled_stop_risk": None,
            "net_r": None,
            "r_status": "NO_CLOSED_PROTECTED_OUTCOME",
        }
        outcome = closed.get(key)
        if outcome is not None:
            notional = Decimal(outcome["average_open_price"]) * Decimal(outcome["peak_quantity"])
            if not notional.is_finite() or notional <= 0:
                raise ValueError("invalid native entry notional")
            net = cash(outcome["realized_pnl"])
            fees = sum((cash(c) for c in outcome["commissions"]), Decimal(0))
            funding = sum(
                (
                    cash(a["pnl_change"])
                    for a in outcome["adjustments"]
                    if a["adjustment_type"] == "FUNDING" and a["pnl_change"] is not None
                ),
                Decimal(0),
            )
            other_adjustments = sum(
                (
                    cash(a["pnl_change"])
                    for a in outcome["adjustments"]
                    if a["adjustment_type"] != "FUNDING" and a["pnl_change"] is not None
                ),
                Decimal(0),
            )
            funding_known = all(
                a["pnl_change"] is not None
                for a in outcome["adjustments"]
                if a["adjustment_type"] == "FUNDING"
            )
            other_known = all(
                a["pnl_change"] is not None
                for a in outcome["adjustments"]
                if a["adjustment_type"] != "FUNDING"
            )
            components_known = funding_known and other_known
            price_pnl = net + fees - funding - other_adjustments if components_known else None
            value = net / notional
            returns.append(float(value))
            net_total += net
            if campaign.get("stop_price") is not None:
                stop = Decimal(campaign["stop_price"])
                entry = Decimal(outcome["average_open_price"])
                quantity = Decimal(outcome["peak_quantity"])
                distance = (entry - stop) * (1 if campaign["direction"] == "LONG" else -1)
                if not stop.is_finite() or stop <= 0:
                    raise ValueError("invalid original campaign stop")
                if distance > 0:
                    risk = distance * quantity
                    row.update(
                        initial_filled_stop_risk=str(risk),
                        net_r=str(net / risk),
                        r_status="COMPUTED_FROM_FILLED_ENTRY_AND_ORIGINAL_STOP",
                    )
                else:
                    row["r_status"] = "ENTRY_AT_OR_BEYOND_ORIGINAL_STOP"
            else:
                row["r_status"] = "ORIGINAL_STOP_UNAVAILABLE"

            row.update(
                status="CLOSED_UNDER_NATIVE_MODEL",
                entry_notional=str(notional),
                native_net_pnl=str(net),
                observed_commissions=str(fees),
                observed_funding_pnl=str(funding) if funding_known else None,
                observed_other_adjustment_pnl=str(other_adjustments) if other_known else None,
                native_price_pnl=str(price_pnl) if price_pnl is not None else None,
                price_return_on_entry_notional=str(price_pnl / notional)
                if price_pnl is not None
                else None,
                commission_fraction=str(fees / notional),
                funding_return_on_entry_notional=str(funding / notional) if funding_known else None,
                other_adjustment_return_on_entry_notional=str(other_adjustments / notional)
                if other_known
                else None,
                component_status="DECOMPOSED_NATIVE_PNL"
                if components_known
                else "MISSING_ADJUSTMENT_PNL",
                net_return_on_entry_notional=str(value),
                opened_ns=outcome["opened_ns"],
                closed_ns=outcome["closed_ns"],
                observed_ns=outcome["observed_ns"],
            )
        rows.append(row)
    cash_change = cash(account["balance_total"]) - initial_balance
    residual = cash_change - net_total
    terminal = all(p["is_closed"] for p in account["positions"]) and all(
        o["status"] in {"FILLED", "CANCELED", "REJECTED", "DENIED", "EXPIRED"}
        for o in account["orders"]
    )
    reconciled = terminal and residual == 0
    unresolved = len(rows) - len(returns) - len(terminal_without_entry)
    selection_complete = bool(rows) and unresolved == 0 and reconciled
    risk_values = [Decimal(row["net_r"]) for row in rows if row["net_r"] is not None]
    risk_complete = bool(rows) and len(risk_values) == len(rows) and reconciled

    return {
        "rows": rows,
        "selected_campaign_count": len(rows),
        "selection_cash_scorecard": {
            "status": "COMPLETE_SELECTED_COHORT"
            if selection_complete
            else "INCOMPLETE_OR_UNRECONCILED_COHORT",
            "initial_capital": str(initial_balance),
            "total_return_on_initial_capital": str(cash_change / initial_balance)
            if selection_complete
            else None,
            "mean_cash_return_per_selection": str(cash_change / initial_balance / len(rows))
            if selection_complete
            else None,
            "denominator": "fixed_initial_capital_times_all_selected_campaigns",
            "scope": "DESCRIPTIVE_SELECTION_CASH_NOT_R_OR_EXPECTED_UTILITY",
        },
        "risk_unit_scorecard": {
            "mean_net_r": str(sum(risk_values, Decimal(0)) / len(risk_values))
            if risk_complete
            else None,
            "observed_r_count": len(risk_values),
            "missing_r_count": len(rows) - len(risk_values),
            "status": "COMPLETE_SELECTED_COHORT"
            if risk_complete
            else "INCOMPLETE_OR_UNRECONCILED_COHORT",
            "weighting": "equal_per_selected_campaign",
            "basis": "actual_filled_entry_to_original_stop",
            "scope": "DESCRIPTIVE_REALIZED_MODEL_R_NOT_EXPECTED_EDGE",
        },
        "closed_outcome_count": len(returns),
        "missing_outcome_count": len(rows) - len(returns),
        "terminal_without_entry_count": len(terminal_without_entry),
        "unresolved_campaign_count": unresolved,
        "native_closed_net_pnl": str(net_total),
        "native_cash_change": str(cash_change),
        "cash_reconciliation_residual": str(residual),
        "reconciliation": "CLOSED_CASH_RECONCILED" if reconciled else "OPEN_OR_UNRECONCILED",
        "conditional_mean_closed_return": float(np.mean(returns))
        if returns and reconciled
        else None,
        "calibration_eligible": False,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "scope": "DESCRIPTIVE_NATIVE_MODEL_OUTCOMES_NOT_POLICY_EDGE",
        "limitations": [
            "closed_sample_conditions_on_execution_and_completion",
            "correlated_or_overlapping_samples_not_independent",
            "observed_funding_is_not_venue_finality",
            "native_execution_assumptions_not_measured_costs",
        ],
    }
