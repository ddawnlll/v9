import hashlib
import io
import json
from decimal import Decimal

import pytest
from nautilus_trader.model import Bar, BarType, InstrumentId, Price, Quantity, QuoteTick

from v8_next.app.stream import QuoteRecorder
from v8_next.domain.market import Candle
from v8_next.economics.stream_observation import StreamObservations
from v8_next.evaluation.store import canonical
from v8_next.evaluation.stream_replay import replay_stream


@pytest.mark.parametrize(
    "mutation", [None, "sequence", "observation", "health", "false_halt", "positioning"]
)
def test_stream_replays_interleaved_bars_and_quotes(tmp_path, monkeypatch, mutation):
    hour = 3600 * 10**9
    instrument = "BTCUSDT-PERP.BINANCE"
    source = tmp_path / "warmup.json"
    source.write_text("{}")
    seed = Candle(
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
        "warmup",
    )
    monkeypatch.setattr("v8_next.economics.stream_observation.load_candles", lambda _: (seed,))
    monkeypatch.setattr("v8_next.evaluation.stream_replay.source_hash", lambda: "fixture")
    monkeypatch.setattr("v8_next.app.stream.time.time_ns", lambda: 3 * hour)
    actor = QuoteRecorder(io.StringIO())
    actor.bar_output = io.StringIO()
    actor.observation_output = io.StringIO()
    actor.observations = StreamObservations((source,), "range-breakout-48-v1")

    def quote(clock):
        return QuoteTick(
            InstrumentId.from_str(instrument),
            Price.from_str("100.00"),
            Price.from_str("100.01"),
            Quantity.from_str("1.000"),
            Quantity.from_str("1.000"),
            clock,
            clock,
        )

    actor.on_quote(quote(hour + 10))
    actor.on_bar(
        Bar(
            BarType.from_str(f"{instrument}-1-HOUR-LAST-EXTERNAL"),
            Price.from_str("100.00"),
            Price.from_str("101.00"),
            Price.from_str("99.00"),
            Price.from_str("100.00"),
            Quantity.from_str("1.000"),
            2 * hour - 1_000_000,
            2 * hour + 1,
        )
    )
    if mutation == "positioning":
        actor.positioning_output = io.StringIO()
        actor.apply_positioning_capture((source,), 2 * hour + 5)
        (tmp_path / "positioning.jsonl").write_text(actor.positioning_output.getvalue())
    actor.on_quote(quote(2 * hour + 10))
    (tmp_path / "quotes.jsonl").write_text(actor.output.getvalue())
    (tmp_path / "bars.jsonl").write_text(actor.bar_output.getvalue())
    observations = actor.observation_output.getvalue()
    if mutation == "observation":
        observations = observations.replace('"UNVERIFIED_CALIBRATION"', '"FORGED"')
    (tmp_path / "observations.jsonl").write_text(observations)
    if mutation == "sequence":
        p = tmp_path / "quotes.jsonl"
        p.write_text(p.read_text().replace('"sequence":2', '"sequence":1'))
    (tmp_path / "session.json").write_text(
        canonical(
            dict(
                started_ns=hour,
                code_and_lock_hash="fixture",
                warmup_manifests=[str(source)],
                warmup_manifest_hashes=actor.observations.source_hashes,
                grammar="range-breakout-48-v1",
                instruments=[instrument],
            )
        )
    )
    result = dict(quote_count=2, bar_count=1, ended_ns=3 * hour)
    if mutation == "positioning":
        result.update(
            positioning_count=1,
            positioning_sha256=hashlib.sha256(
                (tmp_path / "positioning.jsonl").read_bytes()
            ).hexdigest(),
        )
    if mutation in {"health", "false_halt"}:
        session = json.loads((tmp_path / "session.json").read_text())
        session.update(started_ns=hour, max_quote_silence_ns=5)
        (tmp_path / "session.json").write_text(canonical(session))
        result.update(
            status="HALTED_QUOTE_SILENCE",
            health_halt=dict(
                checked_ns=3 * hour,
                started_ns=hour,
                threshold_ns=5,
                instruments=[instrument] if mutation == "health" else [],
            ),
        )

    for filename, key in (
        ("quotes.jsonl", "quote_sha256"),
        ("bars.jsonl", "bar_sha256"),
        ("observations.jsonl", "observation_sha256"),
        ("session.json", "session_sha256"),
    ):
        result[key] = hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest()
    (tmp_path / "result.json").write_text(json.dumps(result))
    if mutation in {"sequence", "observation", "false_halt"}:
        with pytest.raises(ValueError):
            replay_stream(tmp_path)
    else:
        report = replay_stream(tmp_path)
        assert report["status"] == (
            "HALTED_PREFIX_REPRODUCED" if mutation == "health" else "OBSERVATIONS_REPRODUCED"
        )
        assert report["event_count"] == (4 if mutation == "positioning" else 3)
        assert report["observation_count"] == 2
        from v8_next.evaluation.stream_replay import restore_stream

        restored = restore_stream(tmp_path)
        assert restored.candles[instrument][-1].end_ns == 2 * hour
        assert restored.observe(instrument, 2 * hour + 20) is None
        child = tmp_path / "child"
        child.mkdir()
        session = json.loads((tmp_path / "session.json").read_text())
        session.update(
            resume_from=str(tmp_path),
            started_ns=4 * hour,
            parent_result_sha256=hashlib.sha256(
                (tmp_path / "result.json").read_bytes()
            ).hexdigest(),
        )
        from dataclasses import replace

        backfill = tmp_path / "backfill.json"
        backfill.write_text('{"kind":"backfill-fixture"}')
        third = replace(
            seed,
            start_ns=2 * hour,
            end_ns=3 * hour,
            received_ns=4 * hour,
            available_ns=None,
            source_hash="third",
        )
        monkeypatch.setattr(
            "v8_next.economics.stream_observation.load_candles",
            lambda p: (third,) if p == backfill else (seed,),
        )
        session.update(
            backfill_manifests=[str(backfill)],
            backfill_manifest_hashes=[hashlib.sha256(backfill.read_bytes()).hexdigest()],
        )
        (child / "session.json").write_text(canonical(session))
        child_result = dict(quote_count=0, bar_count=0, ended_ns=5 * hour)
        for filename, key in (
            ("quotes.jsonl", "quote_sha256"),
            ("bars.jsonl", "bar_sha256"),
            ("observations.jsonl", "observation_sha256"),
            ("session.json", "session_sha256"),
        ):
            if filename != "session.json":
                (child / filename).write_text("")
            child_result[key] = hashlib.sha256((child / filename).read_bytes()).hexdigest()
        (child / "result.json").write_text(canonical(child_result))
        assert replay_stream(child)["event_count"] == 0
        assert restore_stream(child).candles[instrument][-1].end_ns == 3 * hour
        (tmp_path / "result.json").write_text("{}")
        with pytest.raises(ValueError, match="parent changed"):
            replay_stream(child)
