from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from v8_next.adapters.campaign import PaperCampaignAdapter
from v8_next.adapters.economic_paper import EconomicPaperAdapter
from v8_next.domain.market import CausalFrame
from v8_next.economics.controller import InstrumentConstraints
from v8_next.risk.admission import RiskLimits


def test_simultaneous_instruments_do_not_overwrite_or_route_each_others_frames(monkeypatch):
    instruments = ["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"]
    frames = {(i, 10): CausalFrame(i, 10, ()) for i in instruments}
    constraints = {i: InstrumentConstraints(D(1), D(1), D(10), D(1)) for i in instruments}
    adapter = EconomicPaperAdapter(frames, RiskLimits(D(1), D(1), D(100), 0), constraints, D(10))
    monkeypatch.setattr(PaperCampaignAdapter, "on_quote", lambda *_: None)
    for instrument in reversed(instruments):
        adapter.on_quote(SimpleNamespace(instrument_id=instrument, ts_init=10))
    assert [d["regime"]["instrument_id"] for d in adapter.decisions] == list(reversed(instruments))
    assert adapter.validity_frames == frames
    assert not adapter.campaigns
    with pytest.raises(ValueError, match="constraints"):
        EconomicPaperAdapter(frames, adapter.limits, {}, D(10))
    with pytest.raises(ValueError, match="identity"):
        EconomicPaperAdapter(
            {(instruments[0], 11): frames[(instruments[0], 10)]}, adapter.limits, constraints, D(10)
        )
