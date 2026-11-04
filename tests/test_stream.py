import io
import json

import pytest
from nautilus_trader.model import InstrumentId, Price, Quantity, QuoteTick

from v8_next.app.stream import QuoteRecorder


@pytest.mark.parametrize("event,received", [(10, 20), (21, 20)])
def test_native_quote_record_preserves_receipt_and_rejects_reversed_clocks(
    monkeypatch, event, received
):
    monkeypatch.setattr("v8_next.app.stream.time.time_ns", lambda: 30)
    output = io.StringIO()
    actor = QuoteRecorder(output)
    quote = QuoteTick(
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
        Price.from_str("100.00"),
        Price.from_str("100.01"),
        Quantity.from_str("1.000"),
        Quantity.from_str("1.000"),
        event,
        received,
    )
    if event > received:
        with pytest.raises(ValueError, match="clocks"):
            actor.on_quote(quote)
        assert actor.failure
        assert not output.getvalue()
    else:
        actor.on_quote(quote)
        row = json.loads(output.getvalue())
        assert (row["event_ns"], row["received_ns"], row["recorded_ns"]) == (10, 20, 30)
        assert actor.count == 1


def test_disk_failure_stops_native_node_once_and_suppresses_later_records(monkeypatch):
    monkeypatch.setattr("v8_next.app.stream.time.time_ns", lambda: 30)

    class BrokenOutput(io.StringIO):
        def write(self, text):
            raise OSError("disk unavailable")

    stopped = []
    actor = QuoteRecorder(BrokenOutput())
    actor.stop_node = lambda: stopped.append(True)
    quote = QuoteTick(
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
        Price.from_str("100.00"),
        Price.from_str("100.01"),
        Quantity.from_str("1.000"),
        Quantity.from_str("1.000"),
        10,
        20,
    )
    with pytest.raises(OSError):
        actor.on_quote(quote)
    actor.on_quote(quote)
    assert stopped == [True]
    assert actor.count == 0
    assert actor.failure == "OSError: disk unavailable"


def test_stream_warmup_uses_receipt_and_expires_at_next_bar(tmp_path, monkeypatch):
    from decimal import Decimal

    from v8_next.domain.market import Candle
    from v8_next.economics.stream_observation import StreamObservations

    hour = 3600 * 10**9
    symbol = "BTCUSDT-PERP.BINANCE"
    source = tmp_path / "manifest.json"
    source.write_text("{}")
    candle = Candle(
        symbol,
        0,
        hour,
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal(100),
        Decimal(1),
        hour + 10,
        None,
        "fixture",
    )
    monkeypatch.setattr("v8_next.economics.stream_observation.load_candles", lambda _: (candle,))
    observer = StreamObservations((source,), "range-breakout-48-v1")
    assert observer.observe(symbol, hour + 9)["warmup_status"] == "WARMUP_UNAVAILABLE"
    row = observer.observe(symbol, hour + 10)
    assert row["warmup_status"] == "READY"
    assert len(row["stances"]) == 64
    assert row["opportunity"] is None
    assert row["admission_status"] == "UNVERIFIED_CALIBRATION"
    assert observer.observe(symbol, hour + 11) is None
    assert observer.observe(symbol, 2 * hour)["warmup_status"] == "NEXT_CLOSED_BAR_REQUIRED"


def test_closed_stream_bar_advances_once_without_revision_or_gap(tmp_path, monkeypatch):
    from dataclasses import replace
    from decimal import Decimal

    from v8_next.domain.market import Candle
    from v8_next.economics.stream_observation import StreamObservations

    hour = 3600 * 10**9
    symbol = "BTCUSDT-PERP.BINANCE"
    observer = StreamObservations((), "range-breakout-48-v1")
    first = Candle(
        symbol,
        0,
        hour,
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal(100),
        Decimal(1),
        hour + 1,
        hour + 1,
        "first",
    )
    assert observer.add_closed_candle(first)
    assert observer.observe(symbol, hour + 1)["latest_closed_bar_ns"] == hour
    assert observer.observe(symbol, 2 * hour)["warmup_status"] == "NEXT_CLOSED_BAR_REQUIRED"
    second = replace(
        first,
        start_ns=hour,
        end_ns=2 * hour,
        received_ns=2 * hour + 1,
        available_ns=2 * hour + 1,
        source_hash="second",
    )
    assert observer.add_closed_candle(second)
    row = observer.observe(symbol, 2 * hour + 1)
    assert row["warmup_status"] == "READY"
    assert row["candle_source_hashes"] == ["first", "second"]
    assert not observer.add_closed_candle(
        replace(second, received_ns=2 * hour + 2, available_ns=2 * hour + 2)
    )
    assert observer.observe(symbol, 2 * hour + 2) is None
    assert observer.candles[symbol][-1].received_ns == 2 * hour + 1
    with pytest.raises(ValueError, match="revision"):
        observer.add_closed_candle(replace(second, close=Decimal(101)))
    with pytest.raises(ValueError, match="gap"):
        observer.add_closed_candle(
            replace(
                second,
                start_ns=3 * hour,
                end_ns=4 * hour,
                received_ns=4 * hour + 1,
                available_ns=4 * hour + 1,
            )
        )


@pytest.mark.parametrize("offset", [0, 1_000_000])
def test_native_binance_bar_boundary_reaches_stream_frame(monkeypatch, offset):
    from nautilus_trader.model import Bar, BarType

    from v8_next.economics.stream_observation import StreamObservations

    hour = 3600 * 10**9
    monkeypatch.setattr("v8_next.app.stream.time.time_ns", lambda: hour + 100)
    actor = QuoteRecorder(io.StringIO())
    actor.observations = StreamObservations((), "range-breakout-48-v1")
    actor.bar_output = io.StringIO()
    actor.observation_output = io.StringIO()
    bar = Bar(
        BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        Price.from_str("100.00"),
        Price.from_str("101.00"),
        Price.from_str("99.00"),
        Price.from_str("100.00"),
        Quantity.from_str("1.000"),
        hour - 1_000_000 + offset,
        hour + 10,
    )
    if offset:
        with pytest.raises(ValueError, match="closed-bar"):
            actor.on_bar(bar)
        assert actor.bar_count == 0
        return
    actor.on_bar(bar)
    stored = actor.observations.candles["BTCUSDT-PERP.BINANCE"][0]
    assert (stored.start_ns, stored.end_ns, stored.received_ns) == (0, hour, hour + 10)
    actor.on_quote(
        QuoteTick(
            InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            Price.from_str("100.00"),
            Price.from_str("100.01"),
            Quantity.from_str("1.000"),
            Quantity.from_str("1.000"),
            hour + 15,
            hour + 20,
        )
    )
    observation = json.loads(actor.observation_output.getvalue())
    assert observation["latest_closed_bar_ns"] == hour
    assert observation["candle_source_hashes"] == [stored.source_hash]
    assert len(observation["stances"]) == 64


@pytest.mark.parametrize("gap", [False, True])
def test_restart_backfill_is_receipt_qualified_and_atomic(tmp_path, monkeypatch, gap):
    from dataclasses import replace
    from decimal import Decimal

    from v8_next.domain.market import Candle
    from v8_next.economics.stream_observation import StreamObservations

    hour = 3600 * 10**9
    instrument = "BTCUSDT-PERP.BINANCE"
    observer = StreamObservations((), "range-breakout-48-v1")
    first = Candle(
        instrument,
        0,
        hour,
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal(100),
        Decimal(1),
        hour + 1,
        hour + 1,
        "first",
    )
    observer.add_closed_candle(first)
    receipt = 5 * hour + 1 if gap else 3 * hour + 1
    second = replace(
        first,
        start_ns=hour,
        end_ns=2 * hour,
        received_ns=receipt,
        available_ns=None,
        source_hash="backfill",
    )
    third = replace(second, start_ns=(3 if gap else 2) * hour, end_ns=(4 if gap else 3) * hour)
    path = tmp_path / "backfill.json"
    path.write_text("{}")
    monkeypatch.setattr(
        "v8_next.economics.stream_observation.load_candles", lambda _: (second, third)
    )
    if gap:
        with pytest.raises(ValueError, match="gap"):
            observer.backfill((path,), receipt)
        assert observer.candles[instrument] == (first,)
        assert observer.source_hashes == []
    else:
        with pytest.raises(ValueError, match="unknown"):
            observer.backfill((path,), receipt - 1)
        observer.backfill((path,), receipt)
        assert observer.observe(instrument, receipt - 1)["latest_closed_bar_ns"] == hour
        row = observer.observe(instrument, receipt)
        assert row["latest_closed_bar_ns"] == 3 * hour
        assert row["warmup_status"] == "READY"
        assert len(observer.source_hashes) == 1


def test_automatic_refresh_only_requests_stale_instruments(tmp_path, monkeypatch):
    from decimal import Decimal

    from v8_next.app.stream import capture_missing_warmup
    from v8_next.domain.market import Candle
    from v8_next.economics.stream_observation import StreamObservations

    hour = 3600 * 10**9
    observer = StreamObservations((), "range-breakout-48-v1")
    for symbol, end in (("BTCUSDT", hour), ("ETHUSDT", 2 * hour)):
        observer.add_closed_candle(
            Candle(
                f"{symbol}-PERP.BINANCE",
                end - hour,
                end,
                Decimal(100),
                Decimal(101),
                Decimal(99),
                Decimal(100),
                Decimal(1),
                end + 1,
                end + 1,
                symbol,
            )
        )
    requested = []

    def capture(path, symbol, **kwargs):
        requested.append(symbol)
        return path / "manifest.json"

    monkeypatch.setattr("v8_next.app.stream.capture", capture)
    paths = capture_missing_warmup(tmp_path, observer, 2 * hour + 100)
    assert requested == ["BTCUSDT"]
    assert paths == (tmp_path / "backfill-BTCUSDT" / "manifest.json",)


def test_native_health_policy_checks_each_instrument_and_stops_once():
    from v8_next.app.stream import INSTRUMENTS

    actor = QuoteRecorder(io.StringIO())
    stopped = []
    actor.stop_node = lambda: stopped.append(True)
    actor.max_quote_silence_ns = 10
    actor.health_started_ns = 100
    actor.last_quote_ns[INSTRUMENTS[0]] = 109
    actor.check_quote_health(109)
    assert actor.failure is None
    actor.check_quote_health(110)
    assert actor.failure == "QUOTE_SILENCE: " + INSTRUMENTS[1]
    actor.check_quote_health(120)
    assert stopped == [True]
    assert actor.count == 0


def test_stream_funding_observation_enters_at_receipt_and_abstains_on_expiry(tmp_path, monkeypatch):
    from decimal import Decimal

    from v8_next.domain.config import PositioningPolicy
    from v8_next.domain.market import Candle
    from v8_next.domain.positioning import PositioningReading
    from v8_next.economics.stream_observation import StreamObservations

    hour = 3600 * 10**9
    end = 100 * hour
    symbol = "BTCUSDT-PERP.BINANCE"
    bars = tuple(
        Candle(
            symbol,
            i * hour,
            (i + 1) * hour,
            Decimal(100),
            Decimal(101),
            Decimal(89 if i == 99 else 99),
            Decimal(90 if i == 99 else 100),
            Decimal(1),
            end + 1,
            None,
            "bars",
        )
        for i in range(100)
    )
    reading = PositioningReading(
        symbol, "settled_funding_rate", Decimal(".002"), end, end + 5, end + 5, end + 20, "funding"
    )
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    monkeypatch.setattr("v8_next.economics.stream_observation.load_candles", lambda _: bars)
    monkeypatch.setattr(
        "v8_next.economics.stream_observation.load_settled_funding", lambda *a, **k: (reading,)
    )
    observer = StreamObservations(
        (path,), "trend-continuation-v2", PositioningPolicy(funding_max_age_ns=20)
    )

    def stance(clock):
        row = observer.observe(symbol, clock)
        return next(
            s
            for s in row["stances"]
            if s["behavior_family"] == "funding-crowding-reversal" and s["variant_id"] == "a"
        )

    assert stance(end + 4)["kind"] == "ABSTAIN"
    assert stance(end + 5)["kind"] == "SUPPORT"
    assert stance(end + 20)["kind"] == "ABSTAIN"
    assert observer.needs_positioning_refresh(symbol, end + 20)


def test_auxiliary_refresh_is_atomic_and_available_only_after_application(tmp_path, monkeypatch):
    from decimal import Decimal

    from v8_next.domain.positioning import PositioningReading, positioning_at
    from v8_next.economics.stream_observation import StreamObservations

    observer = StreamObservations((), "range-breakout-48-v1")
    observer.candles = {"BTC": ()}
    path = tmp_path / "capture.json"
    path.write_text("{}")
    reading = PositioningReading("BTC", "open_interest", Decimal(10), 10, 11, 11, 30, "source")
    monkeypatch.setattr(observer, "load_positioning", lambda _: (reading,))
    bars = observer.candles
    observer.refresh_positioning((path,), 20)
    assert observer.candles is bars
    assert positioning_at(observer.readings, "BTC", "open_interest", 19) is None
    assert positioning_at(observer.readings, "BTC", "open_interest", 20) == 10
    assert positioning_at(observer.readings, "BTC", "open_interest", 30) is None
    original = (observer.readings, observer.source_hashes, observer.positioning_update_ns)
    conflict = PositioningReading("BTC", "open_interest", Decimal(11), 10, 21, 21, 30, "changed")
    monkeypatch.setattr(observer, "load_positioning", lambda _: (conflict,))
    with pytest.raises(ValueError, match="conflicting"):
        observer.refresh_positioning((path,), 22)
    assert (observer.readings, observer.source_hashes, observer.positioning_update_ns) == original
    with pytest.raises(ValueError, match="clock"):
        observer.refresh_positioning((path,), 20)
    future = PositioningReading("BTC", "open_interest", Decimal(11), 10, 25, 25, 30, "later")
    monkeypatch.setattr(observer, "load_positioning", lambda _: (future,))
    with pytest.raises(ValueError, match="future"):
        observer.refresh_positioning((path,), 23)
    assert (observer.readings, observer.source_hashes, observer.positioning_update_ns) == original


@pytest.mark.parametrize("mode", ["apply", "stop_during_capture", "failure"])
def test_background_positioning_capture_applies_only_to_running_session(
    tmp_path, monkeypatch, mode
):
    import asyncio
    from types import SimpleNamespace

    from v8_next.app.stream import refresh_positioning_loop
    from v8_next.domain.config import PositioningPolicy

    async def scenario():
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        applied, stops = [], []
        actor = SimpleNamespace(
            failure=None,
            stop_node=lambda: stops.append(True),
            observations=SimpleNamespace(
                candles={"BTCUSDT-PERP.BINANCE": ()},
                positioning_policy=PositioningPolicy(open_interest_max_age_ns=100),
            ),
        )

        def capture_inputs(destination, instruments, policy):
            assert instruments == ("BTCUSDT-PERP.BINANCE",)
            if mode == "failure":
                raise ValueError("capture failed")
            if mode == "stop_during_capture":
                loop.call_soon_threadsafe(stopped.set)
            return (tmp_path / "manifest.json",)

        def apply(paths, applied_ns):
            applied.append(paths)
            stopped.set()

        actor.apply_positioning_capture = apply
        monkeypatch.setattr("v8_next.app.stream.capture_positioning_inputs", capture_inputs)
        await refresh_positioning_loop(actor, tmp_path, 0, stopped)
        assert len(applied) == (1 if mode == "apply" else 0)
        assert bool(stops) == (mode == "failure")
        assert (actor.failure is not None) == (mode == "failure")

    asyncio.run(scenario())


def test_resumed_stream_inherits_refresh_interval_and_rejects_change(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from v8_next.app.stream import capture_stream
    from v8_next.domain.config import PositioningPolicy
    from v8_next.economics.stream_observation import StreamObservations

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "session.json").write_text(
        json.dumps({"warmup_manifests": [], "positioning_refresh_seconds": 30})
    )
    (parent / "result.json").write_text(json.dumps({"ended_ns": 1}))
    observer = StreamObservations(
        (), "range-breakout-48-v1", PositioningPolicy(open_interest_max_age_ns=100)
    )
    monkeypatch.setattr("v8_next.evaluation.stream_replay.restore_stream", lambda _: observer)

    def stop_before_native(*args, **kwargs):
        raise ValueError("native sentinel")

    monkeypatch.setattr("v8_next.app.stream.LiveNode", SimpleNamespace(builder=stop_before_native))
    child = tmp_path / "child"
    with pytest.raises(ValueError, match="native sentinel"):
        asyncio.run(capture_stream(child, 1, resume_from=parent))
    assert json.loads((child / "session.json").read_text())["positioning_refresh_seconds"] == 30
    with pytest.raises(ValueError, match="cannot change positioning refresh"):
        asyncio.run(
            capture_stream(
                tmp_path / "changed", 1, resume_from=parent, positioning_refresh_seconds=40
            )
        )
    assert not (tmp_path / "changed").exists()


def test_positioning_manifest_mutation_rejects_before_state_commit(tmp_path, monkeypatch):
    import hashlib

    from v8_next.economics.stream_observation import StreamObservations

    observer = StreamObservations((), "range-breakout-48-v1")
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    expected = (hashlib.sha256(path.read_bytes()).hexdigest(),)

    def changed(_):
        path.write_text('{"changed":true}')
        return ()

    monkeypatch.setattr(observer, "load_positioning", changed)
    with pytest.raises(ValueError, match="during application"):
        observer.refresh_positioning((path,), 1, expected_hashes=expected)
    assert observer.positioning_update_ns == 0
    assert observer.source_hashes == [] and observer.readings == ()
    with pytest.raises(ValueError, match="before application"):
        observer.refresh_positioning((path,), 1, expected_hashes=expected)
