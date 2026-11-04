"""MECHANICS ONLY: cost-source, venue identity and component-conservation checks."""

from decimal import Decimal
from pathlib import Path

from v8_next.evaluation.costs import (
    OperatingExpense,
    VenueFeeSchedule,
    VenueFillCost,
    calibrate_venue_cost,
    compute_operating_net,
)
from v8_next.evaluation.parity import ArtifactBinding


def _artifact(tmp_path: Path, name: str) -> ArtifactBinding:
    path = tmp_path / name
    path.write_bytes(b"physical test artifact")
    return ArtifactBinding.from_file("test", path)


def _schedule(tmp_path: Path) -> VenueFeeSchedule:
    return VenueFeeSchedule(
        venue="BINANCE-USDM",
        fee_tier="VIP0",
        effective_date="2026-09-01",
        source_artifact=_artifact(tmp_path, "fee-schedule.json"),
    )


def test_venue_cost_calibration_carries_identity_and_split_roles(tmp_path: Path) -> None:
    receipt = calibrate_venue_cost(
        (
            VenueFillCost(
                quantity=Decimal("2"),
                fee=Decimal("0.2"),
                fill_price=Decimal("101"),
                reference_mid=Decimal("100"),
                liquidity_role="TAKER",
            ),
            VenueFillCost(
                quantity=Decimal("2"),
                fee=Decimal("0"),
                fill_price=Decimal("99"),
                reference_mid=Decimal("100"),
                liquidity_role="MAKER",
            ),
        ),
        _schedule(tmp_path),
    )
    assert receipt.status == "CALIBRATED"
    assert (receipt.venue, receipt.fee_tier, receipt.fee_effective_date) == (
        "BINANCE-USDM",
        "VIP0",
        "2026-09-01",
    )
    assert receipt.maker_fee_rate == Decimal("0")
    assert receipt.taker_fee_rate == Decimal("0.1")
    assert receipt.fee_rate == Decimal("0.05")
    assert receipt.fill_vs_mid_bps == Decimal("100")
    assert receipt.calibration_version == "venue-cost.v2"


def test_venue_cost_calibration_keeps_identity_when_data_blocked(tmp_path: Path) -> None:
    receipt = calibrate_venue_cost((), _schedule(tmp_path))
    assert receipt.status == "DATA_BLOCKED"
    assert receipt.venue == "BINANCE-USDM"
    assert receipt.taker_fee_rate is None
    assert receipt.fee_rate is None


def test_venue_cost_calibration_refuses_unbound_schedule(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    (tmp_path / "fee-schedule.json").write_bytes(b"tampered")
    try:
        calibrate_venue_cost((), schedule)
    except ValueError as exc:
        assert "DATA_BLOCKED_COST_ARTIFACT" in str(exc)
    else:  # pragma: no cover - the guard must fail closed
        raise AssertionError("calibration accepted a schedule whose artifact changed")


def _expense(tmp_path: Path) -> OperatingExpense:
    return OperatingExpense(
        amount=Decimal("10"),
        currency="USDT",
        period_start_ns=1,
        period_end_ns=2,
        source_artifact=_artifact(tmp_path, "invoice.json"),
    )


def test_operating_net_separates_strategy_and_fixed_cost(tmp_path: Path) -> None:
    result = compute_operating_net(
        transaction_pnl=Decimal("100"),
        funding=Decimal("-2"),
        explicit_fees=Decimal("3"),
        expenses=(_expense(tmp_path),),
        currency="USDT",
    )
    assert result.strategy_net == Decimal("95")
    assert result.operating_net == Decimal("85")
    assert result.status == "NO_ECONOMIC_CLAIM"
    assert result.price_pnl_already_net_of_friction is False


def test_operating_net_refuses_unbacked_friction_flag(tmp_path: Path) -> None:
    """A caller-declared net-of-friction basis must be bound to an artifact."""

    result = compute_operating_net(
        transaction_pnl=Decimal("100"),
        funding=Decimal("-2"),
        explicit_fees=Decimal("3"),
        expenses=(_expense(tmp_path),),
        currency="USDT",
        price_pnl_already_net_of_friction=True,
    )
    assert result.status == "DATA_BLOCKED"
    assert result.reason == "FRICTION_NET_BASIS_NOT_EVIDENCED"


def test_operating_net_honours_evidenced_friction_flag(tmp_path: Path) -> None:
    result = compute_operating_net(
        transaction_pnl=Decimal("100"),
        funding=Decimal("-2"),
        explicit_fees=Decimal("3"),
        expenses=(_expense(tmp_path),),
        currency="USDT",
        price_pnl_already_net_of_friction=True,
        friction_net_evidence=_artifact(tmp_path, "friction-basis.json"),
    )
    assert result.status == "NO_ECONOMIC_CLAIM"
    assert result.strategy_net == Decimal("98")
    assert result.operating_net == Decimal("88")
    assert result.price_pnl_already_net_of_friction is True


def test_operating_net_refuses_tampered_friction_evidence(tmp_path: Path) -> None:
    evidence = _artifact(tmp_path, "friction-basis.json")
    (tmp_path / "friction-basis.json").write_bytes(b"tampered")
    try:
        compute_operating_net(
            transaction_pnl=Decimal("100"),
            funding=Decimal("-2"),
            explicit_fees=Decimal("3"),
            expenses=(_expense(tmp_path),),
            currency="USDT",
            price_pnl_already_net_of_friction=True,
            friction_net_evidence=evidence,
        )
    except ValueError as exc:
        assert "DATA_BLOCKED_COST_ARTIFACT" in str(exc)
    else:  # pragma: no cover - the guard must fail closed
        raise AssertionError("operating net accepted tampered friction evidence")
