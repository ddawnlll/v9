"""Descriptive simulated cash return; not expected utility or statistical evidence."""

from decimal import Decimal
from typing import Any

from nautilus_trader.model import Money

from v8_next.adapters.settlements import funding_exposure_history, position_funding_query_coverage


def terminal_cash_return(account: dict[str, Any], initial_balance: Decimal) -> dict[str, Any]:
    """Caller must recompute account provenance before calling this projection.

    Native terminal balance already includes commissions and observed funding.
    Subtracting those costs again would double count them. No external cash flows
    are supported by the fixed-capital local simulator.
    """
    result: dict[str, Any] = {
        "return": None,
        "status": "ACCOUNT_INPUTS_UNAVAILABLE",
        "realization": "SIMULATED",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "scope": "DESCRIPTIVE_CASH_RETURN_NOT_INFERENCE_OR_CALIBRATION",
    }
    if not initial_balance.is_finite() or initial_balance <= 0:
        raise ValueError("positive finite initial capital required")
    required = {
        "positions",
        "orders",
        "balance_total",
        "currency",
        "realization",
        "accounting_as_of_ns",
    }
    if not required <= account.keys():
        return result
    if account["realization"] != "SIMULATED" or account["currency"] != "USDT":
        raise ValueError("unsupported account realization or currency")
    if any(not p["is_closed"] for p in account["positions"]):
        return {**result, "status": "OPEN_POSITION_REQUIRES_EQUITY_MARK"}
    if any(
        o["status"] not in {"FILLED", "CANCELED", "REJECTED", "DENIED", "EXPIRED"}
        for o in account["orders"]
    ):
        return {**result, "status": "OPEN_ORDER_REMAINS"}
    exposures = funding_exposure_history(account)
    if exposures:
        coverage = position_funding_query_coverage(
            exposures,
            account.get("funding_query_windows", []),
            account["accounting_as_of_ns"],
        )
        if any(p["query_status"] != "BOUNDED_RESPONSE_COVERS_EXPOSURE" for p in coverage):
            return {**result, "status": "FUNDING_EXPOSURE_NOT_FULLY_QUERIED"}
        if account.get("missing_announced_settlements") != []:
            return {**result, "status": "FUNDING_ANNOUNCEMENT_RECONCILIATION_UNRESOLVED"}
    balance = Money.from_str(account["balance_total"])
    if str(balance.currency) != "USDT":
        raise ValueError("balance currency mismatch")
    value = (balance.as_decimal() - initial_balance) / initial_balance
    return {
        **result,
        "return": str(value),
        "status": "COMPUTED_UNDER_OBSERVED_FUNDING_HISTORY",
        "accounting_as_of_ns": account["accounting_as_of_ns"],
        "position_count": len(account["positions"]),
        "limitation": "VENUE_HISTORY_MAY_BE_REVISED; NOT_CASHFLOW_FINALITY_CERTIFICATION",
    }
