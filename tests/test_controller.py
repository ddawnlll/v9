from dataclasses import replace
from decimal import Decimal as D

from v8_next.economics.controller import InstrumentConstraints, decide_campaign
from v8_next.economics.decisions import Opportunity, Stance, StanceKind, UtilityInputs
from v8_next.risk.admission import RiskLimits, RiskSnapshot


def decision(
    *,
    verified=True,
    allocated=frozenset(),
    stance_ns=10,
    expires=100,
    identity="CANONICAL",
    protection=None,
    required=False,
    stop_budget=None,
    stop_exposure=None,
):
    opportunity = Opportunity("o", "btc", "BTCUSDT-PERP.BINANCE", "LONG", 1, expires)
    opportunity = replace(opportunity, identity_status=identity)
    stance = Stance("observer", "group", StanceKind.SUPPORT, "setup", "o", stance_ns)
    return decide_campaign(
        opportunity,
        (stance, replace(stance, observer_id="clone")),
        UtilityInputs(D(10), D(1), D(1), D(1), D(1), D(1), "test-only"),
        RiskSnapshot(D(1000), D(0), D(0), D(0), 10, True),
        RiskLimits(D(1), D("0.5"), D(100), 10),
        InstrumentConstraints(D("0.01"), D("0.01"), D(100), D(10)),
        10,
        D(100),
        D(100),
        allocated,
        calibration_verified=verified,
        protection=protection,
        protection_required=required,
        stop_budget=stop_budget,
        stop_exposure=stop_exposure,
    )


def test_complete_admission_chain_creates_one_campaign():
    result = decision()
    assert result.campaign.quantity == D(1)
    assert result.campaign.opportunity_id == "o"
    assert decision(allocated=frozenset({"o"})).reason == "DUPLICATE_OPPORTUNITY"


def test_missing_authority_future_evidence_and_expiry_reject():
    assert decision(verified=False).reason == "UNVERIFIED_CALIBRATION"
    assert decision(stance_ns=11).reason == "FUTURE_EVIDENCE"
    assert decision(expires=10).reason == "EXPIRED"


def test_ambiguous_identity_cannot_be_overridden_by_calibration():
    for identity in ("AMBIGUOUS", "UNKNOWN"):
        assert decision(identity=identity).reason == "UNRESOLVED_OPPORTUNITY_IDENTITY"


def test_geometry_is_bound_to_opportunity_clocks_and_price():
    from v8_next.economics.protection import CampaignProtection

    protection = CampaignProtection(
        "pandf:a:v2", "o", "BTCUSDT-PERP.BINANCE", "LONG", 10, 50, D(90), D(110)
    )
    assert decision(required=True).reason == "MISSING_CAMPAIGN_GEOMETRY"
    assert (
        decision(protection=replace(protection, opportunity_id="other")).reason
        == "MISMATCHED_CAMPAIGN_GEOMETRY"
    )
    assert (
        decision(protection=replace(protection, observed_ns=11)).reason
        == "INVALID_CAMPAIGN_GEOMETRY_CLOCK"
    )
    assert (
        decision(protection=replace(protection, stop_price=D(100))).reason
        == "PRICE_OUTSIDE_CAMPAIGN_GEOMETRY"
    )
    campaign = decision(protection=protection, required=True).campaign
    assert (campaign.stop_price, campaign.target_price, campaign.expires_ns) == (90, 110, 50)


def test_stop_risk_sizing_reaches_venue_rounding_without_bypassing_calibration():
    from v8_next.economics.protection import CampaignProtection
    from v8_next.risk.sizing import StopBudget, StopExposure

    protection = CampaignProtection(
        "pandf:a:v2", "o", "BTCUSDT-PERP.BINANCE", "LONG", 10, 50, D(90), D(110)
    )
    kwargs = dict(
        protection=protection,
        stop_budget=StopBudget(D(".00333"), D(".05"), 3),
        stop_exposure=StopExposure(D(0), 0, 10, True),
    )
    result = decision(**kwargs)
    assert result.campaign.quantity == D(".33")
    assert result.campaign.quantity * D(10) <= D("3.33")
    assert decision(verified=False, **kwargs).reason == "UNVERIFIED_CALIBRATION"
    kwargs["stop_exposure"] = None
    assert decision(**kwargs).reason == "MISSING_STOP_RISK_INPUTS"
