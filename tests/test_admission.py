from dataclasses import replace
from decimal import Decimal as D

from v8_next.risk.admission import RiskLimits, RiskSnapshot, admit


def decide(snapshot, **kwargs):
    return admit(
        snapshot,
        RiskLimits(D(1), D("0.5"), D(1000), 10),
        100,
        D(100),
        D(500),
        D("0.03"),
        D("0.03"),
        D(10),
        D(10),
        **kwargs,
    )


def test_reservations_and_exposure_limit_bound_quantity():
    snapshot = RiskSnapshot(D(1000), D(100), D(100), D(100), 100, True)
    result = decide(snapshot)
    assert result.quantity == D(3)
    assert decide(replace(snapshot, reserved_notional=D(400))).quantity is None


def test_unreconciled_stale_and_future_snapshots_cannot_authorize():
    snapshot = RiskSnapshot(D(1000), D(0), D(0), D(0), 100, True)
    for changed in (
        replace(snapshot, reconciled=False),
        replace(snapshot, as_of_ns=89),
        replace(snapshot, as_of_ns=101),
    ):
        assert decide(changed).quantity is None


def test_rounding_never_increases_risk_to_meet_minimum():
    snapshot = RiskSnapshot(D(10), D(0), D(0), D(0), 100, True)
    assert decide(snapshot).reason == "BELOW_VENUE_MINIMUM"
