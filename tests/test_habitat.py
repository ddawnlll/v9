"""Versioned habitat discipline: causal, abstention-only, never retroactive."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from v8_next.economics.decisions import Opportunity, Stance, StanceKind
from v8_next.economics.habitat import HABITAT_VERSION, apply_habitat, habitat_status
from v8_next.economics.regime import RegimeObservation


def _regime(trend=None, volatility=None):
    return RegimeObservation(
        "BTCUSDT-PERP.BINANCE", 10, None, None, None, None,
        trend=trend, volatility=volatility,
    )


def _stance(observer="squeeze-swing", kind=StanceKind.SUPPORT, family="compression-breakout"):
    return Stance(
        observer, "ohlcv-compression-v1", kind, "COMPRESSION_BREAKOUT", "opp", 10,
        behavior_family=family,
    )


def _opportunity():
    return Opportunity("opp", "exposure", "BTCUSDT-PERP.BINANCE", "LONG", 1, 100)


def test_squeeze_requires_measured_compression():
    opportunity = _opportunity()
    assert habitat_status(_stance(), _regime("ChopRange", "LowVolSqueeze"), opportunity) == (
        "IN_HABITAT"
    )
    # Trend is agnostic for the compression thesis; missing cells abstain.
    assert habitat_status(_stance(), _regime("BullTrend", "LowVolSqueeze"), opportunity) == (
        "IN_HABITAT"
    )
    for volatility in ("NormalVol", "HighVol", None):
        assert habitat_status(
            _stance(), _regime("ChopRange", volatility), opportunity
        ) == "OUT_OF_HABITAT"


def test_habitat_demotion_is_symmetric_abstention_without_rewriting_history():
    opportunity = _opportunity()
    regime = _regime("ChopRange", "NormalVol")
    for kind in (StanceKind.SUPPORT, StanceKind.CONTRADICT):
        original = _stance(kind=kind)
        adjusted, report = apply_habitat((original,), regime, opportunity)
        assert adjusted[0].kind == StanceKind.ABSTAIN
        assert adjusted[0].reason == "OUT_OF_HABITAT"
        assert original.kind == kind  # input untouched; record keeps both
        assert report[0]["habitat"] == "OUT_OF_HABITAT"
        assert report[0]["habitat_version"] == HABITAT_VERSION


def test_unqualified_families_and_unbound_stances_pass_through():
    opportunity = _opportunity()
    regime = _regime("ChopRange", "HighVol")
    other = _stance(observer="donchian-breakout", family="channel-breakout")
    adjusted, report = apply_habitat((other,), regime, opportunity)
    assert adjusted[0] == other
    assert report[0]["habitat"] == "UNQUALIFIED"
    abstain = _stance(kind=StanceKind.ABSTAIN)
    adjusted, report = apply_habitat((abstain,), regime, opportunity)
    assert adjusted[0] == abstain
    assert report[0]["habitat"] == "NOT_APPLICABLE"
    # Misaligned regime inputs never evaluate habitat.
    foreign = replace(_regime("ChopRange", "LowVolSqueeze"), instrument_id="ETHUSDT-PERP.BINANCE")
    assert habitat_status(_stance(), foreign, opportunity) == "NOT_APPLICABLE"
    assert habitat_status(_stance(), _regime("ChopRange", "LowVolSqueeze"), None) == (
        "NOT_APPLICABLE"
    )


def _adapter_decision(monkeypatch, volatility):
    from v8_next.adapters import economic_paper
    from v8_next.economics.controller import InstrumentConstraints
    from v8_next.economics.decisions import UtilityInputs
    from v8_next.risk.admission import RiskLimits, RiskSnapshot

    opportunity = _opportunity()
    stance = _stance()
    monkeypatch.setattr(economic_paper, "grammar_opportunity", lambda *_: opportunity)
    monkeypatch.setattr(economic_paper, "policy_stances", lambda *_, readings: (stance,))
    monkeypatch.setattr(
        economic_paper,
        "observe_regime",
        lambda *_, **__: _regime("ChopRange", volatility),
    )
    monkeypatch.setattr(economic_paper.PaperCampaignAdapter, "on_quote", lambda *_: None)
    monkeypatch.setattr(
        economic_paper,
        "native_portfolio_risk",
        lambda *_, **__: SimpleNamespace(
            snapshots={
                "exposure": RiskSnapshot(
                    Decimal(10000), Decimal(0), Decimal(0), Decimal(0), 10, True
                )
            },
            stop_exposure=None,
        ),
    )

    def calibration(*_):
        return (
            UtilityInputs(
                Decimal(10), Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1),
                "test-only",
            ),
            True,
        )

    flat = SimpleNamespace(
        positions_open=lambda: [],
        orders_open=lambda: [],
        positions=lambda: [],
        account_for_venue=lambda *_: SimpleNamespace(
            balance_total=lambda *_: SimpleNamespace(as_decimal=lambda: Decimal(10000))
        ),
    )

    class Harness(economic_paper.EconomicPaperAdapter):
        @property
        def cache(self):
            return flat

    subject = Harness(
        {
            10: SimpleNamespace(
                instrument_id="BTCUSDT-PERP.BINANCE",
                decision_ns=10,
                continuous=False,
                candles=[SimpleNamespace(end_ns=9)],
            )
        },
        RiskLimits(Decimal(1), Decimal(1), Decimal(100), 0),
        InstrumentConstraints(Decimal(1), Decimal(1), Decimal(1), Decimal(1)),
        Decimal(100),
        calibration,
    )
    subject.on_quote(
        SimpleNamespace(
            ts_init=10,
            ts_event=9,
            instrument_id="BTCUSDT-PERP.BINANCE",
            ask_price=SimpleNamespace(as_decimal=lambda: Decimal(100)),
            bid_price=SimpleNamespace(as_decimal=lambda: Decimal(100)),
        )
    )
    return subject.decisions[-1]


def test_paper_adapter_abstains_out_of_habitat_despite_test_calibration(monkeypatch):
    record = _adapter_decision(monkeypatch, "NormalVol")
    assert record["reason"] == "ABSTAIN"
    assert record["habitat"][0]["habitat"] == "OUT_OF_HABITAT"
    assert record["habitat_version"] == HABITAT_VERSION
    assert record["admitted_stances"][0]["kind"] == "ABSTAIN"
    assert record["stances"][0]["kind"] == "SUPPORT"  # original preserved


def test_paper_adapter_admits_in_habitat_compression(monkeypatch):
    record = _adapter_decision(monkeypatch, "LowVolSqueeze")
    assert record["reason"] == "PAPER_CAMPAIGN_ADMITTED"
    assert record["habitat"][0]["habitat"] == "IN_HABITAT"
