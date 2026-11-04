from decimal import Decimal

import pytest

from v8_next.domain.campaign import PaperCampaign


@pytest.mark.parametrize(
    "stop,target",
    [
        (None, Decimal(110)),
        (Decimal(90), None),
        (Decimal(110), Decimal(90)),
        (Decimal("NaN"), Decimal(110)),
        (Decimal(0), Decimal(110)),
    ],
)
def test_invalid_protection_is_rejected_before_engine(stop, target):
    with pytest.raises(ValueError):
        PaperCampaign("c", "o", "i", "LONG", Decimal(1), 1, 10, stop, target)


def test_legacy_timeout_only_record_and_protected_decimal_roundtrip():
    record = {
        "campaign_id": "c",
        "opportunity_id": "o",
        "instrument_id": "i",
        "direction": "LONG",
        "quantity": "0.01",
        "decision_ns": 1,
        "expires_ns": 10,
    }
    campaign = PaperCampaign.from_record(record)
    assert campaign.stop_price is None and campaign.target_price is None
    protected = PaperCampaign.from_record(
        {**record, "stop_price": "99.25", "target_price": "102.50"}
    )
    assert PaperCampaign.from_record(protected.to_record()) == protected
    assert protected.to_record()["target_price"] == "102.50"
