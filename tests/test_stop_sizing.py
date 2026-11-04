from dataclasses import replace
from decimal import Decimal as D

from v8_next.risk.admission import RiskSnapshot
from v8_next.risk.sizing import StopBudget, StopExposure, stop_budget_notional


def test_long_short_stop_distance_and_reserved_heat():
    snapshot = RiskSnapshot(D(1000), D(0), D(0), D(0), 10, True)
    exposure = StopExposure(D(20), 1, 10, True)
    policy = StopBudget(D(".01"), D(".05"), 3)
    for direction, stop in [("LONG", D(95)), ("SHORT", D(105))]:
        amount, reason = stop_budget_notional(
            snapshot, exposure, policy, price=D(100), stop=stop, direction=direction, decision_ns=10
        )
        assert amount == D(200)  # 10 USDT nominal risk / 5 USDT distance.
        assert reason == "STOP_BUDGET_SIZED"
    amount, reason = stop_budget_notional(
        snapshot,
        replace(exposure, open_and_reserved_risk=D(45)),
        policy,
        price=D(100),
        stop=D(95),
        direction="LONG",
        decision_ns=10,
    )
    assert amount is None and reason == "PORTFOLIO_HEAT_EXCEEDED"


def test_unverified_stale_and_reserved_concurrency_cannot_size():
    snapshot = RiskSnapshot(D(1000), D(0), D(0), D(0), 10, True)
    policy = StopBudget(D(".01"), D(".05"), 3)
    for exposure, expected in [
        (StopExposure(D(0), 0, 10, False), "UNRECONCILED_STOP_EXPOSURE"),
        (StopExposure(D(0), 0, 9, True), "STALE_OR_FUTURE_STOP_EXPOSURE"),
        (StopExposure(D(0), 3, 10, True), "CAMPAIGN_CONCURRENCY_LIMIT"),
    ]:
        amount, reason = stop_budget_notional(
            snapshot, exposure, policy, price=D(100), stop=D(95), direction="LONG", decision_ns=10
        )
        assert amount is None and reason == expected
