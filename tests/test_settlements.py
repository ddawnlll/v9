import hashlib
import json
from decimal import Decimal

import pytest

from v8_next.adapters.settlements import FinalFunding, missing_announced_settlements


def test_accounting_replay_preserves_economic_boundary_and_receipt_separately():
    record = FinalFunding(
        "BTCUSDT-PERP.BINANCE", 10, 20, Decimal("0.001"), Decimal(10000), "test-only"
    )
    mark, funding = record.execution_replay_events(20)
    assert mark.ts_event == funding.ts_event == 10
    assert funding.ts_init == 10
    assert record.received_ns == 20
    with pytest.raises(ValueError, match="cutoff"):
        record.execution_replay_events(19)


def test_future_boundary_cannot_be_disguised_as_final_settlement():
    record = FinalFunding(
        "BTCUSDT-PERP.BINANCE", 30, 20, Decimal("0.001"), Decimal(10000), "test-only"
    )
    with pytest.raises(ValueError, match="settled"):
        record.execution_replay_events(40)


def test_announced_boundary_requires_final_record_without_using_forecast_rate(tmp_path):
    raw = json.dumps(
        {"symbol": "BTCUSDT", "nextFundingTime": 20, "lastFundingRate": "0.987"}
    ).encode()
    (tmp_path / "funding_schedule.json").write_bytes(raw)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "symbol": "BTCUSDT",
                "artifacts": [
                    {
                        "path": "funding_schedule.json",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "received_time_ns": 10_000_000,
                        "source_url": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
                    }
                ],
            }
        )
    )
    assert missing_announced_settlements([manifest], (), 0, 30_000_000) == (20_000_000,)
    final = FinalFunding(
        "BTCUSDT-PERP.BINANCE",
        20_000_000,
        25_000_000,
        Decimal("0.001"),
        Decimal(10000),
        "test-only",
    )
    assert missing_announced_settlements([manifest], (final,), 0, 30_000_000) == ()
    from dataclasses import replace

    # Matching timestamps cannot substitute for the correct instrument or a
    # receipt known at the evaluation cutoff.
    for changed in (
        {"instrument_id": "ETHUSDT-PERP.BINANCE"},
        {"received_ns": 31_000_000},
        {"received_ns": 19_000_000},
    ):
        assert missing_announced_settlements(
            [manifest], (replace(final, **changed),), 0, 30_000_000
        ) == (20_000_000,)


def test_revised_accounting_settles_after_last_quote_before_cutoff(tmp_path, monkeypatch):
    from nautilus_trader.backtest import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig
    from nautilus_trader.model import (
        AccountType,
        CryptoPerpetual,
        Currency,
        InstrumentId,
        Money,
        OmsType,
        Price,
        Quantity,
        Symbol,
        Venue,
    )

    from v8_next.adapters.accounting_replay import replay_frozen_campaigns
    from v8_next.domain.campaign import PaperCampaign

    def test_engine(*args, **kwargs):
        engine = BacktestEngine(BacktestEngineConfig())
        usdt = Currency.from_str("USDT")
        engine.add_venue(
            Venue("BINANCE"),
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money.from_str("10000 USDT")],
            default_leverage=Decimal(1),
        )
        engine.add_instrument(
            CryptoPerpetual(
                instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
                raw_symbol=Symbol("BTCUSDT"),
                base_currency=Currency.from_str("BTC"),
                quote_currency=usdt,
                settlement_currency=usdt,
                is_inverse=False,
                price_precision=2,
                size_precision=3,
                price_increment=Price.from_str("0.01"),
                size_increment=Quantity.from_str("0.001"),
                maker_fee=Decimal("0.001"),
                taker_fee=Decimal("0.001"),
                ts_event=0,
                ts_init=0,
            )
        )
        return engine, {}

    # Only instrument setup is a test fixture. Production capture decoding,
    # settlement cutoff selection and native accounting all run unchanged.
    monkeypatch.setattr("v8_next.adapters.accounting_replay.build_engine", test_engine)
    manifests = []
    for second in (1, 2, 3, 4, 5, 6):
        folder = tmp_path / str(second)
        folder.mkdir()
        artifacts = []
        for name, endpoint, received, payload in (
            (
                "quote.json",
                "ticker/bookTicker",
                second * 10**9,
                {
                    "symbol": "BTCUSDT",
                    "time": second * 1000,
                    "bidPrice": "10000.00",
                    "askPrice": "10000.00",
                    "bidQty": "1.000",
                    "askQty": "1.000",
                },
            ),
            (
                "funding.json",
                "fundingRate",
                4 * 10**9,
                [
                    {
                        "symbol": "BTCUSDT",
                        "fundingTime": 3000,
                        "fundingRate": "0.01",
                        "markPrice": "10000.00",
                    }
                ],
            ),
            (
                "funding_schedule.json",
                "premiumIndex",
                second * 10**9,
                {"symbol": "BTCUSDT", "nextFundingTime": 3000},
            ),
        ):
            raw = json.dumps(payload).encode()
            (folder / name).write_bytes(raw)
            artifacts.append(
                {
                    "path": name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "received_time_ns": received,
                    "source_url": f"https://fapi.binance.com/fapi/v1/{endpoint}?symbol=BTCUSDT",
                }
            )
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps({"symbol": "BTCUSDT", "artifacts": artifacts}))
        manifests.append(manifest)
    campaign = PaperCampaign(
        "cutoff-test",
        "cutoff-opportunity",
        "BTCUSDT-PERP.BINANCE",
        "LONG",
        Decimal("0.010"),
        10**9,
        10 * 10**9,
    )
    config = {"maker_fee": "0.001", "taker_fee": "0.001", "initial_balance": "10000"}
    unknown = replay_frozen_campaigns(manifests[:2], (campaign,), config, 3 * 10**9)
    assert unknown["settlement_sources"] == []
    assert unknown["missing_announced_settlements"] == [3 * 10**9]
    known = replay_frozen_campaigns(manifests[:2], (campaign,), config, 4 * 10**9)
    assert len(known["settlement_sources"]) == 1
    assert known["missing_announced_settlements"] == []
    assert Decimal(unknown["balance_total"].split()[0]) == Decimal("9999.90")
    assert Decimal(known["balance_total"].split()[0]) == Decimal("9998.90")
    assert known["realization"] == "SIMULATED"
    assert known["outcomes"]["missing_outcome_count"] == 1
    assert known["position_closures"] == []
    from dataclasses import replace

    first = replace(campaign, expires_ns=2_500_000_000)
    second = replace(
        campaign,
        campaign_id="second-cycle",
        opportunity_id="second-op",
        decision_ns=3_000_000_000,
        expires_ns=4_500_000_000,
    )
    cycles = replay_frozen_campaigns(manifests, (first, second), config, 6_000_000_000)
    assert {c["campaign_id"] for c in cycles["position_closures"]} == {
        first.campaign_id,
        second.campaign_id,
    }
    assert cycles["outcomes"]["closed_outcome_count"] == 2
    assert Decimal(cycles["outcomes"]["cash_reconciliation_residual"]) == 0
    assert cycles["outcomes"]["calibration_eligible"] is False

    # Two instruments share cash, but never funding/position identity. All cloned
    # capture material below is isolated test data, not a production artifact.
    eth = CryptoPerpetual(
        instrument_id=InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
        raw_symbol=Symbol("ETHUSDT"),
        base_currency=Currency.from_str("ETH"),
        quote_currency=Currency.from_str("USDT"),
        settlement_currency=Currency.from_str("USDT"),
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.001"),
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.001"),
        ts_event=0,
        ts_init=0,
    )
    monkeypatch.setattr(
        "v8_next.adapters.accounting_replay.capture_native_inputs",
        lambda *_: ((), eth, (), Currency.from_str("USDT")),
    )
    eth_manifests = []
    for original in manifests[:2]:
        folder = tmp_path / ("eth-" + original.parent.name)
        folder.mkdir()
        metadata = json.loads(original.read_text().replace("BTCUSDT", "ETHUSDT"))
        for artifact in metadata["artifacts"]:
            raw = (
                (original.parent / artifact["path"])
                .read_text()
                .replace("BTCUSDT", "ETHUSDT")
                .encode()
            )
            (folder / artifact["path"]).write_bytes(raw)
            artifact["sha256"] = hashlib.sha256(raw).hexdigest()
        target = folder / "manifest.json"
        target.write_text(json.dumps(metadata))
        eth_manifests.append(target)
    eth_campaign = replace(
        campaign, campaign_id="eth", opportunity_id="eth-op", instrument_id="ETHUSDT-PERP.BINANCE"
    )
    combined = manifests[:2] + eth_manifests
    portfolio = replay_frozen_campaigns(combined, (campaign, eth_campaign), config, 4 * 10**9)
    assert len(portfolio["positions"]) == 2
    assert len(portfolio["settlement_sources"]) == 2
    assert Decimal(portfolio["balance_total"].split()[0]) == Decimal("9997.80")
    from v8_next.adapters.engine_state import reconcile_replay

    repeated = replay_frozen_campaigns(
        list(reversed(combined)), (campaign, eth_campaign), config, 4 * 10**9
    )
    reconcile_replay(portfolio, repeated)
    with pytest.raises(ValueError, match="duplicate accounting"):
        replay_frozen_campaigns(combined + combined[:1], (), config, 4 * 10**9)


def test_funding_query_coverage_uses_actual_exposure_lifetime():
    from v8_next.adapters.settlements import position_funding_query_coverage

    position = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "opened_ns": 10,
        "closed_ns": 20,
        "is_closed": True,
    }
    window = {
        "instrument_id": position["instrument_id"],
        "start_inclusive_ns": 5,
        "end_inclusive_ns": 25,
        "received_ns": 30,
        "source_sha256": "test-only",
    }
    closed = position_funding_query_coverage([position], [window], 40)[0]
    assert closed["query_status"] == "BOUNDED_RESPONSE_COVERS_EXPOSURE"
    assert closed["cashflow_finality"] == "UNQUALIFIED"
    for changed in (
        {"is_closed": False, "closed_ns": None},
        {"opened_ns": 4},
        {"instrument_id": "ETHUSDT-PERP.BINANCE"},
    ):
        row = position_funding_query_coverage([{**position, **changed}], [window], 40)[0]
        assert row["query_status"] == "EXPOSURE_NOT_FULLY_QUERIED"
    assert position_funding_query_coverage([position], [window], 29)[0]["source_sha256"] == []
    with pytest.raises(ValueError, match="exposure interval"):
        position_funding_query_coverage([{**position, "closed_ns": None}], [window], 40)


def test_funding_coverage_combines_known_overlapping_windows_without_bridging_gaps():
    from v8_next.adapters.settlements import position_funding_query_coverage

    position = {"instrument_id": "BTC", "opened_ns": 10, "closed_ns": 30, "is_closed": True}
    first = {
        "instrument_id": "BTC",
        "start_inclusive_ns": 5,
        "end_inclusive_ns": 20,
        "received_ns": 25,
        "source_sha256": "first",
    }
    second = {
        "instrument_id": "BTC",
        "start_inclusive_ns": 20,
        "end_inclusive_ns": 35,
        "received_ns": 40,
        "source_sha256": "second",
    }
    row = position_funding_query_coverage([position], [second, first], 40)[0]
    assert row["source_sha256"] == ["first", "second"]
    assert row["cashflow_finality"] == "UNQUALIFIED"
    for changed in ({"start_inclusive_ns": 22}, {"received_ns": 41}, {"instrument_id": "ETH"}):
        row = position_funding_query_coverage([position], [first, second | changed], 40)[0]
        assert row["query_status"] == "EXPOSURE_NOT_FULLY_QUERIED"
        assert row["source_sha256"] == []


def test_funding_coverage_retains_prior_netting_campaign():
    from v8_next.adapters.settlements import funding_exposure_history
    from v8_next.evaluation.cash_return import terminal_cash_return

    latest = dict(instrument_id="BTC", opened_ns=30, closed_ns=40, is_closed=True)
    old = dict(instrument_id="BTC", opened_ns=10, closed_ns=20)
    account = dict(
        positions=[latest],
        position_closures=[old, latest],
        orders=[],
        balance_total="100 USDT",
        currency="USDT",
        realization="SIMULATED",
        accounting_as_of_ns=50,
        missing_announced_settlements=[],
        funding_query_windows=[
            dict(
                instrument_id="BTC",
                start_inclusive_ns=30,
                end_inclusive_ns=40,
                received_ns=45,
                source_sha256="recent",
            )
        ],
    )
    assert len(funding_exposure_history(account)) == 2
    assert (
        terminal_cash_return(account, Decimal(100))["status"]
        == "FUNDING_EXPOSURE_NOT_FULLY_QUERIED"
    )
    account["funding_query_windows"].append(
        dict(
            instrument_id="BTC",
            start_inclusive_ns=10,
            end_inclusive_ns=20,
            received_ns=45,
            source_sha256="prior",
        )
    )
    assert (
        terminal_cash_return(account, Decimal(100))["status"]
        == "COMPUTED_UNDER_OBSERVED_FUNDING_HISTORY"
    )


def test_no_exposure_is_not_funding_finality_and_fills_prevent_exemption():
    from v8_next.adapters.accounting_replay import funding_coverage_status

    account = dict(positions=[], position_closures=[], orders=[])
    assert funding_coverage_status(account, (10,)) == "NOT_APPLICABLE_NO_POSITION_EXPOSURE"
    account["orders"] = [{"filled_qty": "0.001"}]
    assert funding_coverage_status(account, (10,)) == "INCOMPLETE_ANNOUNCED_SETTLEMENT_MISSING"
    assert (
        funding_coverage_status(account, ())
        == "OBSERVED_FINAL_RECORDS_ONLY_NOT_COMPLETENESS_CERTIFIED"
    )
    account["orders"] = [{"filled_qty": "NaN"}]
    with pytest.raises(ValueError, match="filled quantity"):
        funding_coverage_status(account, ())

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
