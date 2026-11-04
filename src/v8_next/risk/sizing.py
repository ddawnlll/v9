"""Stop-distance risk budget; native infrastructure owns margin and execution."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.risk.admission import RiskSnapshot


@dataclass(frozen=True)
class StopBudget:
    risk_fraction: Decimal
    max_heat_fraction: Decimal
    max_concurrency: int

    def __post_init__(self) -> None:
        if (
            not self.risk_fraction.is_finite()
            or not self.max_heat_fraction.is_finite()
            or not 0 < self.risk_fraction <= self.max_heat_fraction <= 1
            or type(self.max_concurrency) is not int
            or self.max_concurrency < 1
        ):
            raise ValueError("invalid stop budget policy")


@dataclass(frozen=True)
class StopExposure:
    open_and_reserved_risk: Decimal
    active_and_reserved_campaigns: int
    as_of_ns: int
    reconciled: bool


def stop_budget_notional(
    snapshot: RiskSnapshot,
    exposure: StopExposure,
    policy: StopBudget,
    *,
    price: Decimal,
    stop: Decimal,
    direction: str,
    decision_ns: int,
) -> tuple[Decimal | None, str]:
    """Nominal stop risk is not a maximum loss guarantee (gaps/costs remain).

    Exposure includes pending reservations; missing protection cannot be treated
    as zero risk. Caller must derive/reconcile it against native state.
    """
    values = (
        snapshot.equity,
        exposure.open_and_reserved_risk,
        policy.risk_fraction,
        policy.max_heat_fraction,
        price,
        stop,
    )
    if any(not v.is_finite() or v < 0 for v in values):
        raise ValueError("finite nonnegative stop budget inputs required")
    if (
        not 0 < policy.risk_fraction <= policy.max_heat_fraction <= 1
        or type(policy.max_concurrency) is not int
        or policy.max_concurrency < 1
        or type(exposure.active_and_reserved_campaigns) is not int
        or exposure.active_and_reserved_campaigns < 0
    ):
        raise ValueError("invalid stop budget policy/exposure")
    if not snapshot.reconciled or not exposure.reconciled:
        return None, "UNRECONCILED_STOP_EXPOSURE"
    if exposure.as_of_ns != snapshot.as_of_ns or exposure.as_of_ns != decision_ns:
        return None, "STALE_OR_FUTURE_STOP_EXPOSURE"
    if exposure.active_and_reserved_campaigns >= policy.max_concurrency:
        return None, "CAMPAIGN_CONCURRENCY_LIMIT"
    if direction not in {"LONG", "SHORT"} or price <= 0 or stop <= 0:
        return None, "INVALID_STOP_GEOMETRY"
    distance = (price - stop) * (1 if direction == "LONG" else -1)
    if distance <= 0:
        return None, "INVALID_STOP_GEOMETRY"
    budget = snapshot.equity * policy.risk_fraction
    if budget <= 0:
        return None, "NO_RISK_CAPITAL"
    if exposure.open_and_reserved_risk + budget > snapshot.equity * policy.max_heat_fraction:
        return None, "PORTFOLIO_HEAT_EXCEEDED"
    return budget / distance * price, "STOP_BUDGET_SIZED"
