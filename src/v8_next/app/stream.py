"""Native public Binance quote capture; no execution client or capital authority."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from nautilus_trader.adapters.binance import (
    BinanceDataClientConfig,
    BinanceDataClientFactory,
    BinanceInstrumentProviderConfig,
    BinanceProductType,
)
from nautilus_trader.common import DataActor, Environment
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    Bar,
    BarType,
    InstrumentId,
    QuoteTick,
    TraderId,
    TradeTick,
)

from v8_next.adapters.binance_capture import capture
from v8_next.app.observe import source_hash
from v8_next.domain.config import PositioningPolicy
from v8_next.domain.market import Candle
from v8_next.economics.stream_observation import StreamObservations
from v8_next.evaluation.store import canonical

INSTRUMENTS = ("BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE")


class QuoteRecorder(DataActor):
    def __init__(self, output: TextIO) -> None:
        super().__init__()
        self.output = output
        self.stop_node: Callable[[], None] | None = None
        self.max_quote_silence_ns: int | None = None
        self.health_started_ns: int | None = None
        self.health_halt: dict[str, Any] | None = None
        self.last_quote_ns: dict[str, int] = {}
        self.count = 0
        self.bar_count = 0
        self.positioning_count = 0
        self.trade_count = 0
        self.trade_output: TextIO | None = None
        self.positioning_output: TextIO | None = None
        self.bar_output: TextIO | None = None
        self.observations: StreamObservations | None = None
        self.observation_output: TextIO | None = None
        self.failure: str | None = None

    def apply_positioning_capture(self, manifests: tuple[Path, ...], applied_ns: int) -> None:
        """Record auxiliary application in the same sequence as native callbacks."""
        if self.failure is not None:
            raise ValueError("cannot refresh a failed stream")
        try:
            if self.observations is None or self.positioning_output is None:
                raise ValueError("positioning update requires observation and recording paths")
            if applied_ns > time.time_ns():
                raise ValueError("positioning application reaches future")
            paths = tuple(p.resolve() for p in manifests)
            expected_hashes = tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in paths)
            row = dict(
                sequence=self.count + self.bar_count + self.positioning_count,
                applied_ns=applied_ns,
                manifests=[str(p) for p in paths],
                manifest_hashes=list(expected_hashes),
            )
            self.observations.refresh_positioning(
                paths, applied_ns, expected_hashes=expected_hashes
            )
            self.positioning_output.write(canonical(row) + "\n")
            self.positioning_output.flush()
            self.positioning_count += 1
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
            if self.stop_node is not None:
                self.stop_node()
            raise

    def on_start(self) -> None:
        for name in INSTRUMENTS:
            self.subscribe_quotes(InstrumentId.from_str(name))
            self.subscribe_trades(InstrumentId.from_str(name))
            if self.observations is not None:
                self.subscribe_bars(BarType.from_str(f"{name}-1-HOUR-LAST-EXTERNAL"))
        if self.max_quote_silence_ns is not None:
            self.health_started_ns = self.clock.timestamp_ns()
            self.clock.set_timer_ns(
                "v8-quote-health", self.max_quote_silence_ns, callback=self.on_time_event
            )

    def on_time_event(self, event: object) -> None:
        self.check_quote_health(self.clock.timestamp_ns())

    def check_quote_health(self, now_ns: int) -> None:
        if (
            self.failure is not None
            or self.max_quote_silence_ns is None
            or self.health_started_ns is None
        ):
            return
        stale = [
            name
            for name in INSTRUMENTS
            if now_ns - self.last_quote_ns.get(name, self.health_started_ns)
            >= self.max_quote_silence_ns
        ]
        if stale:
            self.health_halt = dict(
                checked_ns=now_ns,
                started_ns=self.health_started_ns,
                threshold_ns=self.max_quote_silence_ns,
                instruments=stale,
            )
            self.failure = "QUOTE_SILENCE: " + ",".join(stale)
            if self.stop_node is not None:
                self.stop_node()

    def on_stop(self) -> None:
        if self.max_quote_silence_ns is not None:
            self.clock.cancel_timer("v8-quote-health")

    def on_bar(self, bar: Bar) -> None:
        if self.failure is not None:
            return
        try:
            instrument = str(bar.bar_type.instrument_id)
            hour, millisecond = 3600 * 10**9, 10**6
            end = bar.ts_event + millisecond
            if (
                instrument not in INSTRUMENTS
                or str(bar.bar_type) != f"{instrument}-1-HOUR-LAST-EXTERNAL"
                or end % hour != 0
                or not 0 < end <= bar.ts_init <= time.time_ns()
            ):
                raise ValueError("unqualified Binance closed-bar clocks or type")
            if self.bar_output is None or self.observations is None:
                raise ValueError("closed-bar recorder not configured")
            record = dict(
                sequence=self.count + self.bar_count + self.positioning_count,
                instrument_id=instrument,
                start_ns=end - hour,
                end_ns=end,
                native_event_ns=bar.ts_event,
                received_ns=bar.ts_init,
                open=str(bar.open),
                high=str(bar.high),
                low=str(bar.low),
                close=str(bar.close),
                volume=str(bar.volume),
                source="NAUTILUS_BINANCE_CLOSED_KLINE_V2_0_0RC4",
            )
            serialized = canonical(record)
            candle = Candle(
                instrument,
                end - hour,
                end,
                bar.open.as_decimal(),
                bar.high.as_decimal(),
                bar.low.as_decimal(),
                bar.close.as_decimal(),
                bar.volume.as_decimal(),
                bar.ts_init,
                bar.ts_init,
                hashlib.sha256(serialized.encode()).hexdigest(),
            )
            self.bar_output.write(serialized + "\n")
            self.bar_output.flush()
            self.observations.add_closed_candle(candle)
            self.bar_count += 1
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
            if self.stop_node is not None:
                self.stop_node()
            raise

    def on_quote(self, quote: QuoteTick) -> None:
        if self.failure is not None:
            return
        try:
            observed = time.time_ns()
            if (
                str(quote.instrument_id) not in INSTRUMENTS
                or not 0 < quote.ts_event <= quote.ts_init <= observed
            ):
                raise ValueError("unqualified quote identity or clocks")
            if (
                quote.bid_price.as_decimal() <= 0
                or quote.ask_price.as_decimal() < quote.bid_price.as_decimal()
            ):
                raise ValueError("invalid quote prices")
            self.output.write(
                canonical(
                    dict(
                        sequence=self.count + self.bar_count + self.positioning_count,
                        instrument_id=str(quote.instrument_id),
                        event_ns=quote.ts_event,
                        received_ns=quote.ts_init,
                        recorded_ns=observed,
                        bid=str(quote.bid_price),
                        ask=str(quote.ask_price),
                        bid_size=str(quote.bid_size),
                        ask_size=str(quote.ask_size),
                        claim_status="NO_ECONOMIC_CLAIM",
                    )
                )
                + "\n"
            )
            self.output.flush()
            self.last_quote_ns[str(quote.instrument_id)] = quote.ts_init
            self.count += 1
            if self.observations is not None and self.observation_output is not None:
                observation = self.observations.observe(str(quote.instrument_id), quote.ts_init)
                if observation is not None:
                    observation["trigger_sequence"] = (
                        self.count + self.bar_count + self.positioning_count - 1
                    )
                    self.observation_output.write(canonical(observation) + "\n")
                    self.observation_output.flush()
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
            if self.stop_node is not None:
                self.stop_node()
            raise

    def on_trade(self, trade: TradeTick) -> None:
        """Record venue trades verbatim: aggressor evidence, never inferred.

        The aggressor side is the venue's own flag mapped by the capture reader;
        a trade whose side cannot be read is rejected here rather than recorded
        with a guessed direction.
        """
        if self.failure is not None:
            return
        try:
            observed = time.time_ns()
            if (
                str(trade.instrument_id) not in INSTRUMENTS
                or not 0 < trade.ts_event <= trade.ts_init <= observed
            ):
                raise ValueError("unqualified trade identity or clocks")
            if trade.price.as_decimal() <= 0 or trade.size.as_decimal() <= 0:
                raise ValueError("invalid trade price or size")
            if str(trade.aggressor_side) not in ("BUY", "SELL"):
                raise ValueError("trade carries no aggressor side")
            if self.trade_output is None:
                raise ValueError("trade recorder not configured")
            self.trade_output.write(
                canonical(
                    dict(
                        # Trade rows carry their own arrival counter: the quote
                        # sequence is defined over quote/bar/positioning events and
                        # reusing it would make two different feeds collide.
                        sequence=self.trade_count,
                        instrument_id=str(trade.instrument_id),
                        event_ns=trade.ts_event,
                        received_ns=trade.ts_init,
                        recorded_ns=observed,
                        price=str(trade.price),
                        size=str(trade.size),
                        aggressor_side=str(trade.aggressor_side),
                        trade_id=str(trade.trade_id),
                        source="NAUTILUS_BINANCE_TRADE_TICK_V2_0_0RC4",
                        claim_status="NO_ECONOMIC_CLAIM",
                    )
                )
                + "\n"
            )
            self.trade_output.flush()
            self.trade_count += 1
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
            if self.stop_node is not None:
                self.stop_node()
            raise


def capture_missing_warmup(
    destination: Path, observations: StreamObservations, as_of_ns: int
) -> tuple[Path, ...]:
    """Bounded public refresh; native engine still owns live connectivity."""
    hour = 3600 * 10**9
    expected_end = as_of_ns // hour * hour
    paths = []
    for instrument, bars in sorted(observations.candles.items()):
        if instrument not in INSTRUMENTS:
            raise ValueError("unsupported automatic backfill instrument")
        if (
            not bars
            or bars[-1].end_ns < expected_end
            or observations.needs_positioning_refresh(instrument, as_of_ns)
        ):
            symbol = instrument.removesuffix("-PERP.BINANCE")
            paths.append(
                capture(
                    destination / f"backfill-{symbol}",
                    symbol,
                    include_open_interest=observations.positioning_policy.open_interest_max_age_ns
                    is not None,
                    account_ratio_period=observations.positioning_policy.account_ratio_period,
                )
            )
    return tuple(paths)


def capture_positioning_inputs(
    destination: Path, instruments: tuple[str, ...], policy: PositioningPolicy
) -> tuple[Path, ...]:
    return tuple(
        capture(
            destination / instrument.split("-")[0],
            symbol=instrument.split("-")[0],
            include_open_interest=policy.open_interest_max_age_ns is not None,
            account_ratio_period=policy.account_ratio_period,
        )
        for instrument in instruments
    )


async def refresh_positioning_loop(
    actor: QuoteRecorder, destination: Path, interval: int, stopped: asyncio.Event
) -> None:
    try:
        while not stopped.is_set() and actor.failure is None:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            if actor.observations is None:
                raise ValueError("positioning refresh requires observations")
            policy = actor.observations.positioning_policy
            instruments = tuple(sorted(actor.observations.candles))
            paths = await asyncio.to_thread(
                capture_positioning_inputs,
                destination / f"positioning-capture-{time.time_ns()}",
                instruments,
                policy,
            )
            if stopped.is_set() or actor.failure is not None:
                return
            actor.apply_positioning_capture(paths, time.time_ns())
    except Exception as error:
        actor.failure = f"{type(error).__name__}: {error}"
        if actor.stop_node is not None:
            actor.stop_node()


async def capture_stream(
    destination: Path,
    duration_seconds: int,
    *,
    manifests: tuple[Path, ...] = (),
    grammar: str | None = None,
    resume_from: Path | None = None,
    backfill_manifests: tuple[Path, ...] = (),
    refresh_on_resume: bool = False,
    max_quote_silence_ns: int | None = None,
    positioning_policy: PositioningPolicy | None = None,
    positioning_refresh_seconds: int | None = None,
) -> dict[str, Any]:
    if duration_seconds <= 0:
        raise ValueError("positive observation duration required")
    if max_quote_silence_ns is not None and (
        type(max_quote_silence_ns) is not int or max_quote_silence_ns <= 0
    ):
        raise ValueError("positive quote silence threshold required")
    observations: StreamObservations | None
    parent_hash = None
    if resume_from is not None:
        from v8_next.evaluation.stream_replay import restore_stream

        if manifests:
            raise ValueError("resume inherits warmup sources")
        observations = restore_stream(resume_from)
        if grammar is not None and grammar != observations.grammar:
            raise ValueError("resume cannot change grammar")
        if positioning_policy is not None and positioning_policy != observations.positioning_policy:
            raise ValueError("resume cannot change positioning freshness policy")
        grammar = observations.grammar
        parent_session = json.loads((resume_from / "session.json").read_text())
        inherited_interval = parent_session.get("positioning_refresh_seconds")
        if (
            positioning_refresh_seconds is not None
            and positioning_refresh_seconds != inherited_interval
        ):
            raise ValueError("resume cannot change positioning refresh interval")
        positioning_refresh_seconds = inherited_interval
        manifests = tuple(Path(p) for p in parent_session["warmup_manifests"])
        parent_raw = (resume_from / "result.json").read_bytes()
        if json.loads(parent_raw)["ended_ns"] > time.time_ns():
            raise ValueError("resume source reaches future")
        parent_hash = hashlib.sha256(parent_raw).hexdigest()
    else:
        grammar = grammar or "range-breakout-48-v1"
        observations = (
            StreamObservations(manifests, grammar, positioning_policy) if manifests else None
        )
    if positioning_refresh_seconds is not None:
        if type(positioning_refresh_seconds) is not int or positioning_refresh_seconds <= 0:
            raise ValueError("positive positioning refresh interval required")
        if observations is None or not any(
            v is not None for v in observations.positioning_policy.model_dump().values()
        ):
            raise ValueError("positioning refresh requires warmup and enabled auxiliary inputs")
    if refresh_on_resume and (resume_from is None or observations is None or backfill_manifests):
        raise ValueError("automatic refresh requires resume without explicit backfills")
    destination.mkdir(parents=True, exist_ok=False)
    if refresh_on_resume:
        assert observations is not None
        backfill_manifests = await asyncio.to_thread(
            capture_missing_warmup, destination, observations, time.time_ns()
        )
    started_ns = time.time_ns()
    if backfill_manifests:
        if resume_from is None or observations is None:
            raise ValueError("backfill requires a resumed observation session")
        observations.backfill(backfill_manifests, started_ns)
    (destination / "session.json").write_text(
        canonical(
            dict(
                instruments=INSTRUMENTS,
                resume_from=str(resume_from.resolve()) if resume_from else None,
                parent_result_sha256=parent_hash,
                grammar=grammar,
                positioning_policy=observations.positioning_policy.model_dump(mode="json")
                if observations
                else {},
                refresh_on_resume=refresh_on_resume,
                positioning_refresh_seconds=positioning_refresh_seconds,
                max_quote_silence_ns=max_quote_silence_ns,
                warmup_manifest_hashes=sorted(
                    hashlib.sha256(p.read_bytes()).hexdigest() for p in manifests
                ),
                backfill_manifests=[str(p.resolve()) for p in backfill_manifests],
                backfill_manifest_hashes=sorted(
                    hashlib.sha256(p.read_bytes()).hexdigest() for p in backfill_manifests
                ),
                warmup_manifests=[str(p.resolve()) for p in manifests],
                started_ns=started_ns,
                duration_seconds=duration_seconds,
                code_and_lock_hash=source_hash(),
                scope="PUBLIC_NATIVE_QUOTES_NOT_ECONOMIC_OPERATION",
                claim_status="NO_ECONOMIC_CLAIM",
            )
        )
        + "\n"
    )
    builder = LiveNode.builder("v8-market-observer", TraderId("V8-001"), Environment.LIVE)
    builder.add_data_client(
        "BINANCE",
        BinanceDataClientFactory(),
        BinanceDataClientConfig(
            product_type=BinanceProductType.USD_M,
            instrument_provider=BinanceInstrumentProviderConfig(load_ids=list(INSTRUMENTS)),
        ),
    )
    node = builder.build()
    with (
        (destination / "quotes.jsonl").open("x") as output,
        (destination / "observations.jsonl").open("x") as observation_output,
        (destination / "bars.jsonl").open("x") as bar_output,
        (destination / "positioning.jsonl").open("x") as positioning_output,
        (destination / "trades.jsonl").open("x") as trade_output,
    ):
        actor = QuoteRecorder(output)
        actor.stop_node = node.handle().stop
        actor.max_quote_silence_ns = max_quote_silence_ns
        actor.observations = observations
        actor.observation_output = observation_output
        actor.bar_output = bar_output
        actor.positioning_output = positioning_output
        actor.trade_output = trade_output
        node.add_actor(actor)

        refresh_stopped = asyncio.Event()

        async def stop_after() -> None:
            await asyncio.sleep(duration_seconds)
            refresh_stopped.set()
            node.handle().stop()

        stopper = asyncio.create_task(stop_after())
        refresher = (
            asyncio.create_task(
                refresh_positioning_loop(
                    actor, destination, positioning_refresh_seconds, refresh_stopped
                )
            )
            if positioning_refresh_seconds is not None
            else None
        )
        try:
            await node.run_async()
            refresh_stopped.set()
            if refresher is not None:
                refresher.cancel()
                await asyncio.gather(refresher, return_exceptions=True)
            if actor.failure and actor.health_halt is None:
                raise ValueError(actor.failure)
            positioning_output.flush()
            os.fsync(positioning_output.fileno())
            trade_output.flush()
            os.fsync(trade_output.fileno())
            bar_output.flush()
            os.fsync(bar_output.fileno())
            output.flush()
            os.fsync(output.fileno())
            observation_output.flush()
            os.fsync(observation_output.fileno())
            with (destination / "quotes.jsonl").open("rb") as recorded:
                quotes_hash = hashlib.file_digest(recorded, "sha256").hexdigest()
            result = dict(
                quote_sha256=quotes_hash,
                bar_count=actor.bar_count,
                positioning_count=actor.positioning_count,
                positioning_sha256=hashlib.sha256(
                    (destination / "positioning.jsonl").read_bytes()
                ).hexdigest(),
                bar_sha256=hashlib.sha256((destination / "bars.jsonl").read_bytes()).hexdigest(),
                observation_sha256=hashlib.sha256(
                    (destination / "observations.jsonl").read_bytes()
                ).hexdigest(),
                session_sha256=hashlib.sha256(
                    (destination / "session.json").read_bytes()
                ).hexdigest(),
                trades_sha256=hashlib.sha256(
                    (destination / "trades.jsonl").read_bytes()
                ).hexdigest(),
                trade_count=actor.trade_count,
                quote_count=actor.count,
                ended_ns=time.time_ns(),
                status="HALTED_QUOTE_SILENCE"
                if actor.health_halt
                else ("OBSERVED" if actor.count else "NO_QUOTES_OBSERVED"),
                trade_capture_scope="PUBLIC_VENUE_TRADES_WITH_AGGRESSOR_SIDE",
                health_halt=actor.health_halt,
                claim_status="NO_ECONOMIC_CLAIM",
            )
            (destination / "result.json").write_text(canonical(result) + "\n")
            return result
        except Exception as error:
            (destination / "failure.json").write_text(
                canonical(
                    dict(
                        status="FAILED",
                        error_type=type(error).__name__,
                        callback_failure=actor.failure,
                        completed_quote_count=actor.count,
                        ended_ns=time.time_ns(),
                        claim_status="NO_ECONOMIC_CLAIM",
                    )
                )
                + "\n"
            )
            raise
        finally:
            refresh_stopped.set()
            if refresher is not None:
                refresher.cancel()
                await asyncio.gather(refresher, return_exceptions=True)
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            node.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--warmup-manifest", type=Path, action="append", default=[])
    parser.add_argument("--grammar")
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--backfill-manifest", type=Path, action="append", default=[])
    parser.add_argument("--refresh-on-resume", action="store_true")
    parser.add_argument("--max-quote-silence-ns", type=int)
    parser.add_argument("--positioning-policy", type=Path)
    parser.add_argument("--positioning-refresh-seconds", type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                capture_stream(
                    args.destination,
                    args.duration_seconds,
                    manifests=tuple(args.warmup_manifest),
                    grammar=args.grammar,
                    resume_from=args.resume_from,
                    backfill_manifests=tuple(args.backfill_manifest),
                    refresh_on_resume=args.refresh_on_resume,
                    max_quote_silence_ns=args.max_quote_silence_ns,
                    positioning_refresh_seconds=args.positioning_refresh_seconds,
                    positioning_policy=PositioningPolicy.model_validate_json(
                        args.positioning_policy.read_text()
                    )
                    if args.positioning_policy
                    else None,
                )
            )
        )
    )


if __name__ == "__main__":
    main()
