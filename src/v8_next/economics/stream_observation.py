"""Receipt-qualified warmup observations triggered by native quotes."""

import hashlib
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from v8_next.adapters.captured_market import (
    load_account_ratio,
    load_candles,
    load_open_interest,
    load_settled_funding,
)
from v8_next.domain.config import PositioningPolicy
from v8_next.domain.market import Candle, frame_at
from v8_next.domain.positioning import Metric, PositioningReading, positioning_at
from v8_next.economics.grammar import POLICIES, grammar_opportunity
from v8_next.economics.regime import observe_regime
from v8_next.evaluation.store import canonical
from v8_next.experts.catalog import observe_all


class StreamObservations:
    def __init__(
        self,
        manifests: tuple[Path, ...],
        grammar: str,
        positioning_policy: PositioningPolicy | None = None,
    ) -> None:
        if grammar not in POLICIES:
            raise ValueError("unknown stream grammar")
        self.grammar = grammar
        self.positioning_policy = positioning_policy or PositioningPolicy()
        self.readings: tuple[PositioningReading, ...] = ()
        self.positioning_update_ns = 0
        self.candles: dict[str, tuple[Candle, ...]] = {}
        self.source_hashes = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in manifests)
        self.emitted: dict[str, tuple[str, str, int | None, str]] = {}
        for path in manifests:
            self.readings += self.load_positioning(path)
            bars = load_candles(path)
            if not bars or bars[0].instrument_id in self.candles:
                raise ValueError("empty or duplicate stream warmup instrument")
            self.candles[bars[0].instrument_id] = tuple(
                replace(c, available_ns=c.received_ns) for c in bars
            )

    def load_positioning(self, path: Path) -> tuple[PositioningReading, ...]:
        policy = self.positioning_policy
        readings: tuple[PositioningReading, ...] = ()
        if policy.funding_max_age_ns is not None:
            readings += load_settled_funding(path, max_age_ns=policy.funding_max_age_ns)
        if policy.open_interest_max_age_ns is not None:
            readings += load_open_interest(path, max_age_ns=policy.open_interest_max_age_ns)
        if policy.account_ratio_period is not None and policy.account_ratio_max_age_ns is not None:
            readings += load_account_ratio(
                path, period=policy.account_ratio_period, max_age_ns=policy.account_ratio_max_age_ns
            )
        return readings

    def refresh_positioning(
        self,
        manifests: tuple[Path, ...],
        applied_ns: int,
        *,
        expected_hashes: tuple[str, ...] | None = None,
    ) -> None:
        """Apply verified auxiliary captures without touching native candle history.

        Runtime caller must durably record this application event for replay.
        Availability includes application time; receipt and expiry stay unchanged.
        """
        if type(applied_ns) is not int or applied_ns <= self.positioning_update_ns:
            raise ValueError("positioning application clock must advance")
        actual_hashes = tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in manifests)
        if expected_hashes is not None and expected_hashes != actual_hashes:
            raise ValueError("positioning manifest changed before application")
        readings = self.readings
        hashes = set(self.source_hashes)
        for path in manifests:
            added = self.load_positioning(path)
            if any(
                r.instrument_id not in self.candles or r.received_ns > applied_ns for r in added
            ):
                raise ValueError("unknown instrument or future positioning receipt")
            readings += tuple(
                replace(r, available_ns=max(r.available_ns, applied_ns))
                if r.available_ns is not None
                else r
                for r in added
            )
        if tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in manifests) != actual_hashes:
            raise ValueError("positioning manifest changed during application")
        hashes.update(actual_hashes)
        for instrument in self.candles:
            metrics: tuple[Metric, ...] = (
                "settled_funding_rate",
                "open_interest",
                "long_short_ratio",
            )
            for metric in metrics:
                positioning_at(readings, instrument, metric, applied_ns)
        self.readings = readings
        self.source_hashes = sorted(hashes)
        self.positioning_update_ns = applied_ns

    def needs_positioning_refresh(self, instrument: str, as_of_ns: int) -> bool:
        enabled: tuple[tuple[Metric, int | None], ...] = (
            ("settled_funding_rate", self.positioning_policy.funding_max_age_ns),
            ("open_interest", self.positioning_policy.open_interest_max_age_ns),
            ("long_short_ratio", self.positioning_policy.account_ratio_max_age_ns),
        )
        return any(
            age is not None and positioning_at(self.readings, instrument, metric, as_of_ns) is None
            for metric, age in enabled
        )

    def add_closed_candle(self, candle: Candle) -> bool:
        """Caller supplies a verified closed-bar source; never infer close from a quote."""
        hour = 3600 * 10**9
        if (
            candle.end_ns - candle.start_ns != hour
            or candle.start_ns % hour != 0
            or candle.available_ns != candle.received_ns
            or candle.received_ns < candle.end_ns
            or not candle.source_hash
        ):
            raise ValueError("unqualified live closed candle")
        previous = self.candles.get(candle.instrument_id, ())
        for existing in previous:
            if existing.start_ns == candle.start_ns:
                fields = ("end_ns", "open", "high", "low", "close", "volume")
                if any(getattr(existing, key) != getattr(candle, key) for key in fields):
                    raise ValueError("live candle revision requires explicit policy")
                return False  # Keep the original receipt/availability on reconnect replay.
        if previous and candle.start_ns != previous[-1].end_ns:
            raise ValueError("live candle gap or backwards update")
        self.candles[candle.instrument_id] = (*previous, candle)
        return True

    def backfill(self, manifests: tuple[Path, ...], as_of_ns: int) -> None:
        """Apply verified received bars atomically without revising old observations."""
        original = self.candles
        self.candles = dict(original)
        hashes = set(self.source_hashes)
        readings = self.readings
        try:
            for path in manifests:
                added = self.load_positioning(path)
                if any(r.received_ns > as_of_ns for r in added):
                    raise ValueError("positioning backfill unknown at restart")
                readings += added
                bars = load_candles(path)
                if not bars or bars[0].instrument_id not in original:
                    raise ValueError("backfill requires an existing instrument")
                for bar in bars:
                    if bar.received_ns > as_of_ns:
                        raise ValueError("backfill unknown at restart")
                    # Older history outside retained warmup is not a new update.
                    if bar.end_ns <= original[bar.instrument_id][0].start_ns:
                        continue
                    self.add_closed_candle(replace(bar, available_ns=bar.received_ns))
                hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        except Exception:
            self.candles = original
            raise
        self.source_hashes = sorted(hashes)
        self.readings = readings

    def observe(self, instrument: str, received_ns: int) -> dict[str, Any] | None:
        frame = frame_at(instrument, received_ns, self.candles.get(instrument, ()))
        status = "READY"
        if not frame.candles:
            status = "WARMUP_UNAVAILABLE"
        elif not frame.continuous:
            status = "WARMUP_GAP"
        elif received_ns >= frame.candles[-1].end_ns + 3600 * 10**9:
            status = "NEXT_CLOSED_BAR_REQUIRED"
        latest_end = frame.candles[-1].end_ns if frame.candles else None
        metrics: tuple[Metric, ...] = ("settled_funding_rate", "open_interest", "long_short_ratio")
        values = {
            metric: positioning_at(self.readings, instrument, metric, received_ns)
            for metric in metrics
        }
        positioning = {
            metric: str(value) if value is not None else None for metric, value in values.items()
        }
        key = (instrument, status, latest_end, canonical(positioning))
        if self.emitted.get(instrument) == key:
            return None
        opportunity = grammar_opportunity(frame, self.grammar) if status == "READY" else None
        stances = (
            observe_all(frame, opportunity, readings=self.readings) if status == "READY" else ()
        )
        result = dict(
            instrument_id=instrument,
            decision_ns=received_ns,
            warmup_status=status,
            grammar=self.grammar,
            positioning_values=positioning,
            positioning_policy=self.positioning_policy.model_dump(mode="json"),
            regime=asdict(observe_regime(frame, readings=self.readings))
            if status == "READY"
            else None,
            latest_closed_bar_ns=latest_end,
            candle_source_hashes=sorted({c.source_hash for c in frame.candles}),
            capture_manifest_hashes=self.source_hashes,
            opportunity=asdict(opportunity) if opportunity else None,
            stances=[asdict(s) for s in stances],
            claim_status="NO_ECONOMIC_CLAIM",
            admission_status="UNVERIFIED_CALIBRATION",
            scope="STREAM_WARMUP_OBSERVATION_NOT_EXECUTION",
        )
        # Ensure serializability before marking emitted.
        canonical(result)
        self.emitted[instrument] = key
        return result
