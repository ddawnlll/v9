"""Closed exposure cannot make an unreconciled online account eligible again."""

from types import SimpleNamespace

from v8_next.adapters import economic_paper
from v8_next.economics.decisions import Opportunity, Stance, StanceKind


def test_closed_position_blocks_calibration_and_readmission(monkeypatch):
    opportunity = Opportunity("next", "exposure", "BTCUSDT-PERP.BINANCE", "LONG", 1, 100)
    stance = Stance("observer", "group", StanceKind.SUPPORT, "test", "next", 10)
    monkeypatch.setattr(economic_paper, "grammar_opportunity", lambda *_: opportunity)
    observed_readings = []

    def selected_stances(*_, readings):
        observed_readings.append(readings)
        return (stance,)

    monkeypatch.setattr(economic_paper, "policy_stances", selected_stances)
    monkeypatch.setattr(economic_paper.PaperCampaignAdapter, "on_quote", lambda *_: None)

    def forbidden_calibration(*_):
        raise AssertionError("unreconciled account must not reach calibration/admission")

    adapter = SimpleNamespace(
        frames={
            10: SimpleNamespace(
                instrument_id="BTCUSDT-PERP.BINANCE",
                decision_ns=10,
                continuous=False,
                candles=[SimpleNamespace(end_ns=9)],
            )
        },
        cache=SimpleNamespace(
            positions_open=lambda: [],
            orders_open=lambda: [],
            positions=lambda: [
                SimpleNamespace(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    ts_opened=1,
                    ts_closed=5,
                    is_closed=True,
                )
            ],
        ),
        calibration=forbidden_calibration,
        decisions=[],
    )

    # Native Strategy instances are required for super(); use a real adapter and
    # replace only its cache boundary with the controlled closed-position view.
    class Harness(economic_paper.EconomicPaperAdapter):
        @property
        def cache(self):
            return adapter.cache

    from decimal import Decimal

    from v8_next.economics.controller import InstrumentConstraints
    from v8_next.risk.admission import RiskLimits

    subject = Harness(
        adapter.frames,
        RiskLimits(Decimal(1), Decimal(1), Decimal(100), 0),
        InstrumentConstraints(Decimal(1), Decimal(1), Decimal(1), Decimal(1)),
        Decimal(100),
        forbidden_calibration,
    )
    subject.on_quote(SimpleNamespace(ts_init=10, instrument_id="BTCUSDT-PERP.BINANCE"))
    assert subject.decisions[-1]["reason"] == "UNRECONCILED_FUNDING_AFTER_EXPOSURE"
    assert not subject.campaigns
    assert observed_readings == [subject.positioning_readings]


def test_economic_callback_failure_persists_and_stops_later_admission():
    from decimal import Decimal

    import pytest

    from v8_next.economics.controller import InstrumentConstraints
    from v8_next.risk.admission import RiskLimits

    class Failing(economic_paper.EconomicPaperAdapter):
        calls = 0

        def process_economic_quote(self, quote):
            self.calls += 1
            raise ValueError("economic calculation unavailable")

    subject = Failing(
        {},
        RiskLimits(Decimal(1), Decimal(1), Decimal(100), 0),
        InstrumentConstraints(Decimal(1), Decimal(1), Decimal(1), Decimal(1)),
        Decimal(100),
    )
    with pytest.raises(ValueError, match="unavailable"):
        subject.on_quote(None)
    assert subject.callback_failure == "ValueError: economic calculation unavailable"
    subject.on_quote(None)
    assert subject.calls == 1
    assert not subject.campaigns


def test_campaign_callback_failure_is_retained_for_accounting_replay():
    from decimal import Decimal

    import pytest

    from v8_next.adapters.campaign import PaperCampaignAdapter

    class Failing(PaperCampaignAdapter):
        def advance_campaigns(self, *args):
            raise ValueError("invalid campaign geometry")

    subject = Failing(())
    quote = SimpleNamespace(
        instrument_id="test",
        ts_init=10,
        ask_price=SimpleNamespace(as_decimal=lambda: Decimal(10)),
        bid_price=SimpleNamespace(as_decimal=lambda: Decimal(10)),
    )
    with pytest.raises(ValueError, match="geometry"):
        subject.on_quote(quote)
    assert subject.callback_failure == "ValueError: invalid campaign geometry"
    subject.on_quote(quote)
    assert not subject.submitted


def _readmission_subject(monkeypatch, reconciliation):
    from decimal import Decimal

    from v8_next.economics.controller import InstrumentConstraints
    from v8_next.economics.decisions import UtilityInputs
    from v8_next.risk.admission import RiskLimits, RiskSnapshot

    opportunity = Opportunity("next", "exposure", "BTCUSDT-PERP.BINANCE", "LONG", 1, 100)
    stance = Stance("observer", "group", StanceKind.SUPPORT, "test", "next", 10)
    monkeypatch.setattr(economic_paper, "grammar_opportunity", lambda *_: opportunity)
    monkeypatch.setattr(
        economic_paper, "policy_stances", lambda *_, readings: (stance,)
    )
    monkeypatch.setattr(economic_paper.PaperCampaignAdapter, "on_quote", lambda *_: None)

    calls = []

    def fake_portfolio(*_, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            snapshots={
                "exposure": RiskSnapshot(
                    Decimal(10000), Decimal(0), Decimal(0), Decimal(0), 10, True
                )
            },
            stop_exposure=None,
        )

    monkeypatch.setattr(economic_paper, "native_portfolio_risk", fake_portfolio)

    def calibration(*_):
        return (
            UtilityInputs(
                Decimal(10),
                Decimal(1),
                Decimal(1),
                Decimal(1),
                Decimal(1),
                Decimal(1),
                "test-only",
            ),
            True,
        )

    closed = SimpleNamespace(
        instrument_id="BTCUSDT-PERP.BINANCE", ts_opened=1, ts_closed=5, is_closed=True
    )
    adapter_cache = SimpleNamespace(
        positions_open=lambda: [],
        orders_open=lambda: [],
        positions=lambda: [closed],
        account_for_venue=lambda *_: SimpleNamespace(
            balance_total=lambda *_: SimpleNamespace(as_decimal=lambda: Decimal(10000))
        ),
    )

    class Harness(economic_paper.EconomicPaperAdapter):
        @property
        def cache(self):
            return adapter_cache

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
        funding_reconciliation=reconciliation,
    )
    quote = SimpleNamespace(
        ts_init=10,
        ts_event=9,
        instrument_id="BTCUSDT-PERP.BINANCE",
        ask_price=SimpleNamespace(as_decimal=lambda: Decimal(100)),
        bid_price=SimpleNamespace(as_decimal=lambda: Decimal(100)),
    )
    return subject, quote, calls


def test_reconciled_funding_readmits_closed_exposure(monkeypatch):
    seen = []

    def reconciled(lifetimes, decision_ns):
        seen.append((lifetimes, decision_ns))
        return {"reconciled": True, "query_status": "BOUNDED_RESPONSE_COVERS_EXPOSURE"}

    subject, quote, _ = _readmission_subject(monkeypatch, reconciled)
    subject.on_quote(quote)
    assert seen == (
        [
            (
                [
                    {
                        "instrument_id": "BTCUSDT-PERP.BINANCE",
                        "opened_ns": 1,
                        "closed_ns": 5,
                        "is_closed": True,
                    }
                ],
                10,
            )
        ]
    )
    record = subject.decisions[-1]
    assert record["reason"] == "PAPER_CAMPAIGN_ADMITTED"
    assert record["funding_coverage"]["reconciled"] is True
    assert len(subject.campaigns) == 1


def test_unreconciled_verdict_keeps_blocking_after_close(monkeypatch):
    def denied(lifetimes, decision_ns):
        return {"reconciled": False, "query_status": "EXPOSURE_NOT_FULLY_QUERIED"}

    subject, quote, _ = _readmission_subject(monkeypatch, denied)
    subject.on_quote(quote)
    record = subject.decisions[-1]
    assert record["reason"] == "UNRECONCILED_FUNDING_AFTER_EXPOSURE"
    assert not subject.campaigns
