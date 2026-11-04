import hashlib

import pytest

from v8_next.app.trial import run_trial
from v8_next.domain.config import PaperConfig
from v8_next.evaluation.store import ResearchStore


def test_native_warmup_accumulates_history_without_selection(monkeypatch):
    from decimal import Decimal as D

    from nautilus_trader.model import Bar, BarType, Price, Quantity
    from test_native_engine import run_qualified_engine

    from v8_next.adapters.historical_trial import HistoricalTrial
    from v8_next.domain.market import Candle

    hour = 3600 * 10**9
    instrument = "BTCUSDT-PERP.BINANCE"
    source = tuple(
        Candle(
            instrument,
            i * hour,
            (i + 1) * hour,
            D(10000),
            D(10001),
            D(9999),
            D(10000),
            D(1),
            (i + 1) * hour,
            None,
            "test-only",
        )
        for i in range(4)
    )
    bars = [
        Bar(
            BarType.from_str(instrument + "-1-HOUR-LAST-EXTERNAL"),
            Price.from_str("10000.00"),
            Price.from_str("10001.00"),
            Price.from_str("9999.00"),
            Price.from_str("10000.00"),
            Quantity.from_str("1.000"),
            c.end_ns,
            c.end_ns,
        )
        for c in source
    ]
    seen = []

    def grammar(frame, policy):
        seen.append((frame.decision_ns, len(frame.candles)))
        return None

    monkeypatch.setattr("v8_next.adapters.historical_trial.grammar_opportunity", grammar)
    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="10000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    trial = HistoricalTrial(source, policy, selection_start_ns=3 * hour, selection_end_ns=4 * hour)
    state = run_qualified_engine(strategy=trial, bar_data=bars, standard_assertions=False)
    assert trial.failure is None
    assert seen == [(3 * hour, 3)]
    assert [r["reason"] for r in trial.decisions[:2]] == ["WARMUP_ONLY_SELECTION_NOT_STARTED"] * 2
    assert trial.decisions[-1]["reason"] == "FOLLOWUP_ONLY_SELECTION_CLOSED"
    assert not state["orders"] and not trial.campaigns


def test_development_trial_cannot_consume_registered_holdout(tmp_path):
    manifest = tmp_path / "holdout.json"
    manifest.write_text("{}")
    store = ResearchStore(tmp_path / "research.sqlite")
    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    try:
        store.register_trial(
            "protected",
            "f",
            "policy",
            hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "HOLDOUT",
            1,
        )
        with pytest.raises(ValueError, match="protected holdout"):
            run_trial(manifest, policy, store, "f")
        assert store.family_size("f") == 1
    finally:
        store.close()


def test_callback_failure_is_retained_even_if_native_engine_logs_it():
    from v8_next.adapters.historical_trial import HistoricalTrial

    class Failing(HistoricalTrial):
        def process_bar(self, bar):
            raise ValueError("missing source")

    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    from types import SimpleNamespace

    trial = Failing((), policy)
    trial.instruments = frozenset({"BTC"})
    trial.mark_equity = lambda bar: None
    bar = SimpleNamespace(bar_type=SimpleNamespace(instrument_id="BTC"), ts_event=10, ts_init=10)
    with pytest.raises(ValueError, match="missing source"):
        trial.on_bar(bar)
    assert trial.failure == "ValueError: missing source"
    trial.on_bar(None)
    assert not trial.decisions


@pytest.mark.parametrize("followup_only", [False, True])
def test_historical_native_thesis_exit_precedes_timeout_and_replays(followup_only):
    from decimal import Decimal

    from nautilus_trader.model import Bar, BarType, Price, Quantity
    from test_native_engine import run_qualified_engine

    from v8_next.adapters.engine_state import reconcile_replay
    from v8_next.adapters.historical_trial import HistoricalTrial
    from v8_next.domain.campaign import PaperCampaign
    from v8_next.domain.market import Candle

    hour = 3600 * 10**9
    source = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            i * hour,
            (i + 1) * hour,
            Decimal(price),
            Decimal(price + 1),
            Decimal(price - 1),
            Decimal(price),
            Decimal(1),
            (i + 1) * hour,
            None,
            "isolated-test",
        )
        for i, price in enumerate((10000, 9995, 9996))
    )
    bars = [
        Bar(
            BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
            Price.from_str(format(c.open, ".2f")),
            Price.from_str(format(c.high, ".2f")),
            Price.from_str(format(c.low, ".2f")),
            Price.from_str(format(c.close, ".2f")),
            Quantity.from_str("1.000"),
            c.end_ns,
            c.end_ns,
        )
        for c in source
    ]
    policy = PaperConfig(
        maker_fee=".001",
        taker_fee=".001",
        initial_balance="10000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    campaign = PaperCampaign(
        "historical-thesis",
        "op",
        "BTCUSDT-PERP.BINANCE",
        "LONG",
        Decimal(".01"),
        0,
        10 * hour,
        Decimal(9900),
        Decimal(10100),
        Decimal(9995),
    )
    results = []
    for _ in range(2):
        trial = HistoricalTrial(source, policy, selection_end_ns=hour if followup_only else None)
        trial.campaigns = (PaperCampaign.from_record(campaign.to_record()),)
        state = run_qualified_engine(strategy=trial, bar_data=bars, standard_assertions=False)
        assert trial.failure is None
        assert trial.thesis_invalidated == {campaign.campaign_id: 2 * hour}
        assert trial.exit_requested == {campaign.campaign_id}
        assert state["positions"][0]["is_closed"]
        assert state["positions"][0]["closed_ns"] < campaign.expires_ns
        assert len(trial.equity_marks) == 3
        if followup_only:
            assert all(r["reason"] == "FOLLOWUP_ONLY_SELECTION_CLOSED" for r in trial.decisions)
            assert len(trial.campaigns) == 1
        results.append(state)
    reconcile_replay(*results)


def test_portfolio_boundary_waits_for_all_instruments_once():
    from types import SimpleNamespace

    from v8_next.adapters.historical_trial import HistoricalTrial

    class Recording(HistoricalTrial):
        def mark_equity(self, bar):
            self.events.append("equity")

        def process_bar(self, bar):
            self.events.append(str(bar.bar_type.instrument_id))

    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    trial = Recording((), policy)
    trial.instruments = frozenset({"BTC", "ETH"})
    trial.events = []

    def bar(symbol, clock=10):
        return SimpleNamespace(
            bar_type=SimpleNamespace(instrument_id=symbol), ts_event=clock, ts_init=clock
        )

    trial.on_bar(bar("ETH"))
    assert trial.events == []
    trial.on_bar(bar("BTC"))
    assert trial.events == ["equity", "BTC", "ETH"]
    trial.on_bar(bar("BTC", 20))
    with pytest.raises(ValueError, match="incomplete"):
        trial.on_bar(bar("ETH", 30))


def test_portfolio_trial_cannot_wrap_overlapping_holdout(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import v8_next.app.trial as module

    first, second = tmp_path / "btc.json", tmp_path / "eth.json"
    first.write_text('{"source":"btc"}')
    second.write_text('{"source":"eth"}')
    monkeypatch.setattr(
        module,
        "load_candles",
        lambda path: (
            SimpleNamespace(
                instrument_id="BTC" if path == first else "ETH", start_ns=10, end_ns=20
            ),
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("must reject holdout before native execution")

    monkeypatch.setattr(module, "build_portfolio_engine", forbidden)
    store = ResearchStore(tmp_path / "research.sqlite")
    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    try:
        store.register_trial("h", "holdout", "p", "different-capture", "HOLDOUT", 1)
        store.register_dataset_window("different-capture", "ETH", 15, 25)
        with pytest.raises(ValueError, match="overlapping"):
            run_trial(
                first,
                policy,
                store,
                "portfolio",
                additional_manifests=(second,),
                accounting_as_of_ns=30,
            )
        # Failed attempts remain in search history.
        assert store.family_size("portfolio") == 1
    finally:
        store.close()


def test_portfolio_trial_requires_cutoff_and_unique_sources(tmp_path):
    first = tmp_path / "capture.json"
    first.write_text("{}")
    store = ResearchStore(tmp_path / "research.sqlite")
    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    try:
        with pytest.raises(ValueError, match="explicit known accounting cutoff"):
            run_trial(first, policy, store, "p", additional_manifests=(first,))
        with pytest.raises(ValueError, match="duplicate capture"):
            run_trial(
                first, policy, store, "p", additional_manifests=(first,), accounting_as_of_ns=1
            )
        assert store.family_size("p") == 0
    finally:
        store.close()


def test_component_plan_is_registered_before_execution_and_changes_trial_identity(
    tmp_path, monkeypatch
):
    import v8_next.app.trial as module

    manifest = tmp_path / "capture.json"
    manifest.write_text("{}")
    policy = PaperConfig(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    store = ResearchStore(tmp_path / "research.sqlite")

    def stop_before_engine(path):
        assert store.family_size("planned") >= 1
        raise ValueError("test capture unavailable")

    monkeypatch.setattr(module, "load_candles", stop_before_engine)
    try:
        for plan in ((2, 99, 7), (2, 99, 8), (2, 99, 7)):
            with pytest.raises(ValueError, match="test capture"):
                run_trial(manifest, policy, store, "planned", component_plan=plan)
        assert store.family_size("planned") == 2
        with pytest.raises(ValueError, match="protected holdout"):
            module._run_trial(manifest, policy, store, "h", "HOLDOUT", component_plan=(2, 99, 7))
        assert store.family_size("h") == 0
    finally:
        store.close()


def test_submitted_campaign_occupies_instrument_before_native_cache_update():
    from decimal import Decimal
    from types import SimpleNamespace

    from v8_next.adapters.campaign import PaperCampaignAdapter
    from v8_next.domain.campaign import PaperCampaign

    campaign = PaperCampaign("queued", "op", "ETHUSDT-PERP.BINANCE", "LONG", Decimal(1), 1, 10)
    state = SimpleNamespace(
        campaigns=(campaign,),
        position_closures={},
        expired=set(),
        invalidated=set(),
        cache=SimpleNamespace(order=lambda _: None),
    )
    assert PaperCampaignAdapter.has_unresolved_campaign(state, campaign.instrument_id)
    assert not PaperCampaignAdapter.has_unresolved_campaign(state, "BTCUSDT-PERP.BINANCE")
    state.position_closures = {"closed": {"campaign_id": "queued"}}
    assert not PaperCampaignAdapter.has_unresolved_campaign(state, campaign.instrument_id)
    state.position_closures = {}
    state.cache.order = lambda _: SimpleNamespace(
        status="CANCELED", filled_qty=SimpleNamespace(as_decimal=lambda: Decimal(0))
    )
    assert not PaperCampaignAdapter.has_unresolved_campaign(state, campaign.instrument_id)
    state.cache.order = lambda _: SimpleNamespace(
        status="CANCELED", filled_qty=SimpleNamespace(as_decimal=lambda: Decimal(".5"))
    )
    assert PaperCampaignAdapter.has_unresolved_campaign(state, campaign.instrument_id)


def test_cross_instrument_risk_waits_for_submitted_entry_projection():
    from decimal import Decimal
    from types import SimpleNamespace

    from v8_next.adapters.campaign import PaperCampaignAdapter

    state = SimpleNamespace(
        submitted={"BTC-entry"},
        position_closures={},
        cache=SimpleNamespace(order=lambda _: None, positions_open=lambda: []),
    )
    assert PaperCampaignAdapter.has_unprojected_submission(state)
    state.cache.order = lambda _: SimpleNamespace(
        status="FILLED", filled_qty=SimpleNamespace(as_decimal=lambda: Decimal(1))
    )
    assert PaperCampaignAdapter.has_unprojected_submission(state)
    state.cache.positions_open = lambda: [SimpleNamespace(opening_order_id="BTC-entry")]
    assert not PaperCampaignAdapter.has_unprojected_submission(state)
    state.cache.positions_open = lambda: []
    state.position_closures = {"closed": {"campaign_id": "BTC-entry"}}
    assert not PaperCampaignAdapter.has_unprojected_submission(state)
    state.position_closures = {}
    state.cache.order = lambda _: SimpleNamespace(
        status="REJECTED", filled_qty=SimpleNamespace(as_decimal=lambda: Decimal(0))
    )
    assert not PaperCampaignAdapter.has_unprojected_submission(state)
