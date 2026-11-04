"""Small economic boundary: observations never carry order authority."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from typing import Any

import polars as pl

from v8_next.domain.market import CausalFrame


def numeric(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("expected finite numeric feature")
    return float(value)


class StanceKind(StrEnum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    exposure_id: str
    instrument_id: str
    direction: str
    anchor_ns: int
    expires_ns: int
    grammar_version: str = "range-breakout-48-v1"
    identity_status: str = "CANONICAL"


@dataclass(frozen=True)
class Stance:
    observer_id: str
    dependency_group: str
    kind: StanceKind
    reason: str
    opportunity_id: str | None
    decision_ns: int
    behavior_family: str = "compression-breakout"
    mechanism_family: str = "volatility-expansion-hypothesis"
    version: str = "squeeze-observer-v1"
    variant_id: str = "baseline"


#: Field names of the two hot per-bar records. Resolved once at import instead of
#: per conversion: ``dataclasses.fields()`` was measured as a top-five cost in a
#: portfolio run (302k calls) purely from re-deriving them inside the loop.
STANCE_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(Stance))
OPPORTUNITY_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(Opportunity))


def stance_record(stance: Stance) -> dict[str, Any]:
    """Flat stance record, identical to ``dataclasses.asdict`` for this type.

    Measured on the real quad tape (385 bars x 2 curves x 4 legs x 28 experts):
    ``asdict`` was the single largest cost of the run -- 3.3M recursive
    ``_asdict_inner`` calls, 302k ``fields()`` lookups and 325k ``deepcopy``
    calls -- because it recurses and deep-copies every field, including the
    enum-valued ones. Every ``Stance`` field is a scalar (str/int/enum/None), so
    a direct attribute copy produces a mapping that compares equal field by
    field, with no recursion and no copy. ``test_perf_hot_paths`` pins that
    equality for all 28 canonical experts.
    """
    return {name: getattr(stance, name) for name in STANCE_FIELDS}


def opportunity_record(opportunity: Opportunity) -> dict[str, Any]:
    """Flat opportunity record, identical to ``dataclasses.asdict`` for this type."""
    return {name: getattr(opportunity, name) for name in OPPORTUNITY_FIELDS}


def linear_exposure_id(instrument_id: str) -> str | None:
    """Explicit initial USD-M universe; native metadata must still qualify contracts."""
    return {
        "BTCUSDT-PERP.BINANCE": "BTC/USD:linear-perpetual",
        "ETHUSDT-PERP.BINANCE": "ETH/USD:linear-perpetual",
    }.get(instrument_id)


def opportunity_at(frame: CausalFrame) -> Opportunity | None:
    """Grammar is independent of observer identity and observation multiplicity.

    v1 treats each breakout candle as a discrete measurement episode. Its anchor
    is the closed market candle, never the clock on which an observer notices it.
    Only explicitly mapped single-leg linear USD-M exposure is supported.
    """
    exposure = linear_exposure_id(frame.instrument_id)
    if exposure is None:
        return None
    if not frame.continuous or len(frame.candles) < 49:
        return None
    history, current = frame.candles[-49:-1], frame.candles[-1]
    if current.close > max(c.high for c in history):
        direction = "LONG"
    elif current.close < min(c.low for c in history):
        direction = "SHORT"
    else:
        return None
    identity = json.dumps(
        ["range-breakout-48-v1", exposure, frame.instrument_id, direction, current.end_ns],
        separators=(",", ":"),
    )
    return Opportunity(
        hashlib.sha256(identity.encode()).hexdigest(),
        exposure,
        frame.instrument_id,
        direction,
        current.end_ns,
        current.end_ns + 336 * 3600 * 1_000_000_000,
    )


def observe_breakout_baseline(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    """The frozen comparison omits only the compression filter, not admission."""
    return Stance(
        "range-breakout-baseline",
        "range-breakout-data",
        StanceKind.SUPPORT if opportunity else StanceKind.ABSTAIN,
        "GRAMMAR_BREAKOUT" if opportunity else "NO_OPPORTUNITY",
        opportunity.opportunity_id if opportunity else None,
        frame.decision_ns,
        mechanism_family="range-breakout-hypothesis",
        version="range-breakout-without-compression-v1",
    )


def observe_squeeze(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    observer_id: str = "squeeze-swing",
    *,
    variant: str = "baseline",
) -> Stance:
    """Repo squeeze_swing economic hypothesis, with complete-window warmup.

    20-close population bandwidth, 50-bandwidth range, volume expansion,
    20-close efficiency. Full windows require 69 closes. Missing volume is not
    replaced with one; degenerate bandwidth/volume produces abstention.
    """
    parameters = {
        "baseline": (0.35, 48, 1.30),
        "m1": (0.25, 48, 1.40),
        "m2": (0.30, 72, 1.35),
        "m3": (0.25, 72, 1.40),
    }
    if variant not in parameters:
        raise ValueError("unknown squeeze variant")
    rank_limit, lookback, volume_limit = parameters[variant]
    kind = StanceKind.ABSTAIN
    reason = "NO_OPPORTUNITY"
    if not frame.continuous:
        reason = "SOURCE_GAP"
    elif len(frame.candles) < max(69, lookback + 1):
        reason = "WARMUP"
    elif opportunity is not None:
        values = frame.df.select(["close", "volume"])
        features = values.with_columns(
            (4 * pl.col("close").rolling_std(20, ddof=0) / pl.col("close").rolling_mean(20)).alias(
                "bandwidth"
            ),
            pl.col("volume").rolling_mean(20).alias("volume_mean"),
        )
        bandwidths = features["bandwidth"].tail(50)
        lo, hi, latest = (numeric(v) for v in (bandwidths.min(), bandwidths.max(), bandwidths[-1]))
        volume_mean = numeric(features["volume_mean"][-1])
        close = values["close"].tail(20)
        path = numeric(close.diff().abs().sum())
        chan_high = Decimal(str(numeric(frame.df["high"].slice(-lookback - 1, lookback).max())))
        chan_low = Decimal(str(numeric(frame.df["low"].slice(-lookback - 1, lookback).min())))
        if hi <= lo or volume_mean <= 0 or path <= 0:
            reason = "DEGENERATE_FEATURE"
        elif (latest - lo) / (hi - lo) > rank_limit:
            reason = "NO_COMPRESSION"
        elif numeric(values["volume"][-1]) / volume_mean < volume_limit:
            reason = "NO_VOLUME_EXPANSION"
        elif abs(numeric(close[-1]) - numeric(close[0])) / path < 0.18:
            reason = "LOW_EFFICIENCY"
        elif not (
            opportunity.instrument_id == frame.instrument_id
            and (
                (opportunity.direction == "LONG" and frame.candles[-1].close > chan_high)
                or (opportunity.direction == "SHORT" and frame.candles[-1].close < chan_low)
            )
        ):
            reason = "NO_VARIANT_BREAKOUT"
        else:
            kind, reason = StanceKind.SUPPORT, "COMPRESSION_BREAKOUT"
    return Stance(
        observer_id,
        "ohlcv-compression-v1",
        kind,
        reason,
        opportunity.opportunity_id if opportunity else None,
        frame.decision_ns,
        variant_id=variant,
    )


def reconcile(opportunity: Opportunity, stances: tuple[Stance, ...]) -> str:
    """No vote scores: correlated witnesses cannot manufacture confidence."""
    if any(s.opportunity_id != opportunity.opportunity_id for s in stances):
        raise ValueError("evidence belongs to another opportunity")
    groups: dict[str, set[StanceKind]] = {}
    for stance in stances:
        groups.setdefault(stance.dependency_group, set()).add(stance.kind)
    if any(StanceKind.CONTRADICT in kinds for kinds in groups.values()):
        return "CONTRADICTED"
    return (
        "SUPPORTED_OBSERVATION"
        if any(StanceKind.SUPPORT in kinds for kinds in groups.values())
        else "ABSTAIN"
    )


def build_reconciliation_receipt(
    opportunity: Opportunity,
    stances: tuple[Stance, ...],
    reconciliation_ns: int,
) -> Any:
    """Build an immutable reconciliation receipt (Mutabakat Fişi) from opportunity stances."""
    from v8_next.evaluation.store import DependencyGroupProof, ReconciliationReceipt, canonical

    if any(s.opportunity_id != opportunity.opportunity_id for s in stances):
        raise ValueError("evidence belongs to another opportunity")

    groups: dict[str, list[Stance]] = {}
    for stance in sorted(stances, key=lambda s: (s.dependency_group, s.observer_id)):
        groups.setdefault(stance.dependency_group, []).append(stance)

    proofs: list[DependencyGroupProof] = []
    group_stances: dict[str, str] = {}
    for group_name, group_list in sorted(groups.items()):
        kinds = tuple(s.kind.value for s in group_list)
        if any(s.kind == StanceKind.CONTRADICT for s in group_list):
            eff = "CONTRADICT"
        elif any(s.kind == StanceKind.SUPPORT for s in group_list):
            eff = "SUPPORT"
        else:
            eff = "ABSTAIN"
        group_stances[group_name] = eff
        proofs.append(
            DependencyGroupProof(
                dependency_group=group_name,
                raw_evidence_count=len(group_list),
                kinds=kinds,
                effective_stance=eff,
            )
        )

    if any(eff == "CONTRADICT" for eff in group_stances.values()):
        agg = "CONTRADICTED"
    elif any(eff == "SUPPORT" for eff in group_stances.values()):
        agg = "SUPPORTED_OBSERVATION"
    else:
        agg = "ABSTAIN"

    evidence_ids = tuple(sorted(s.observer_id for s in stances))
    receipt_id = hashlib.sha256(
        canonical([opportunity.opportunity_id, reconciliation_ns, evidence_ids, agg]).encode()
    ).hexdigest()

    return ReconciliationReceipt(
        receipt_id=receipt_id,
        opportunity_id=opportunity.opportunity_id,
        reconciler_algorithm_id="v8-reconciler-independent-groups",
        reconciler_algorithm_version="1.0.0-v8next",
        reconciliation_ns=reconciliation_ns,
        participating_evidence_ids=evidence_ids,
        dependency_group_proofs=tuple(proofs),
        aggregate_stance=agg,
    )


@dataclass(frozen=True)
class UtilityInputs:
    gross_edge: Decimal | None
    fees: Decimal | None
    spread: Decimal | None
    slippage: Decimal | None
    funding_cost: Decimal | None
    uncertainty: Decimal | None
    calibration_receipt: str | None

    def net(self) -> Decimal | None:
        items = (
            self.gross_edge,
            self.fees,
            self.spread,
            self.slippage,
            self.funding_cost,
            self.uncertainty,
        )
        if not self.calibration_receipt or any(v is None for v in items):
            return None
        values = tuple(v for v in items if v is not None)
        if any(not v.is_finite() for v in values):
            raise ValueError("non-finite utility input")
        if any(v < 0 for v in (values[1], values[2], values[3], values[5])):
            raise ValueError("negative friction or uncertainty")
        return values[0] - sum(values[1:], Decimal(0))


def utility_admission(inputs: UtilityInputs) -> str:
    value = inputs.net()
    if value is None:
        return "REJECTED_MISSING_CALIBRATION"
    return "UTILITY_ELIGIBLE" if value > 0 else "REJECTED_SUB_FRICTION"


def stance_with(
    stance: Stance, *, variant_id: str | None = None, version: str | None = None
) -> Stance:
    """Return ``stance`` with the given metadata replaced, without ``dataclasses.replace``.

    The expert observers stamp their own variant/version once per observation;
    ``replace`` re-derives the field list and deep-copies unknown values on each of
    those calls (measured: 197k calls / 0.80s cumulative per portfolio run, the
    largest remaining Python-level cost after the polars collect). A ``Stance`` is a
    flat frozen record, so rebuilding it from its own instance dict is equivalent --
    pinned field-by-field against ``replace`` by ``test_perf_hot_paths``.
    """
    metadata: dict[str, Any] = {}
    if variant_id is not None:
        metadata["variant_id"] = variant_id
    if version is not None:
        metadata["version"] = version
    return type(stance)(**{**stance.__dict__, **metadata})
