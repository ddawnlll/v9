"""P0 grammar census: all five opportunity grammars on one PIT stream, no orders.

What this measures
------------------
Every bar of one verified tape is turned into exactly one :class:`CausalFrame`,
and *each* registered opportunity grammar (``economics.grammar.POLICIES``) is
evaluated on that same frame. The frame sequence, warmup rule, tape and clock are
therefore identical across grammars; only the detection rule differs.

The output answers the P0 question — *what does each grammar actually produce?* —

* candidate bars, directional / neutral / ambiguous split;
* deduplicated economic episodes per grammar, with repeat triggers inside an
  episode window counted as fragments instead of extra episodes;
* the ex-post outcome of every episode under the repository's shared bracket
  geometry (stop 2 %, target 4 %, stop evaluated first inside a bar) plus MFE,
  MAE and signed market move;
* same-bar overlap between grammars, including opposing directions, because a
  trend-continuation LONG and a mean-reversion SHORT are different economic
  hypotheses and are not votes against each other;
* whether the expert panel gate as configured can ever block (pure arithmetic).

What this is not
----------------
No strategy, no order, no venue adapter, no account and no sizing: the module
never imports ``nautilus_trader`` and never constructs an execution object. The
outcome columns are *measurement*, resolved after the episode window has closed
from bars that were not visible at decision time; they are never fed back into a
decision. Costs are not applied, so no expectancy here is an economic claim:
``claim_status`` is ``NO_ECONOMIC_CLAIM`` and ``authority`` is
``OFFLINE_PIT_CENSUS``.

No grammar is admitted, weighted or preferred by this module. Selecting among
them is a separate, explicitly authorized decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from v8_next.domain.market import Candle, CausalFrame, frame_at_incremental
from v8_next.economics.grammar import (
    POLICIES,
    POLICY_HORIZON_BARS,
    POLICY_REQUIRED_BARS,
    grammar_opportunity,
)
from v8_next.evaluation.multitape import load_multitape
from v8_next.evaluation.tape_identity import sha256_file

CENSUS_VERSION: Final[str] = "p0-grammar-census-v1"
CLAIM_STATUS: Final[str] = "NO_ECONOMIC_CLAIM"
AUTHORITY: Final[str] = "OFFLINE_PIT_CENSUS"

#: Shared bracket geometry (matches ``SimArmConfig`` and the V8 parity profile).
STOP_PCT: Final[Decimal] = Decimal("0.02")
TARGET_PCT: Final[Decimal] = Decimal("0.04")

#: Canonical evaluation order: registry order, not a ranking.
GRAMMAR_ORDER: Final[tuple[str, ...]] = (
    "range-breakout-48-v1",
    "volatility-extreme-v2",
    "trend-continuation-v2",
    "mean-reversion-v2",
    "compression-expansion-v2",
)

TARGET_HIT: Final[str] = "TARGET_HIT"
STOP_HIT: Final[str] = "STOP_HIT"
TIMEOUT: Final[str] = "TIMEOUT"
CENSORED: Final[str] = "CENSORED"


@dataclass(frozen=True)
class EpisodeOutcome:
    """Ex-post resolution of one episode window; never a decision input."""

    endpoint: str
    bars_held: int
    mfe_pct: float | None
    mae_pct: float | None
    market_move_pct: float | None


@dataclass(frozen=True)
class GrammarEpisode:
    policy: str
    direction: str
    anchor_ns: int
    horizon_bars: int
    entry_price: float
    identity_status: str
    #: Repeat triggers of the same direction inside this window (same hypothesis).
    fragments: int
    #: True when this episode opened while an opposite-direction episode was live.
    against_open_episode: bool
    outcome: EpisodeOutcome | None


@dataclass(frozen=True)
class GrammarDistribution:
    """One grammar's candidate and outcome census on the shared stream."""

    policy: str
    warmup_bars: int
    horizon_bars: int
    bars_evaluated: int
    directional_bars: int
    neutral_bars: int
    ambiguous_bars: int
    episodes: int
    fragments: int
    against_open_episodes: int
    target_hits: int
    stop_hits: int
    timeouts: int
    censored: int
    hit_rate: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_market_move_pct: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "warmup_bars": self.warmup_bars,
            "horizon_bars": self.horizon_bars,
            "bars_evaluated": self.bars_evaluated,
            "directional_bars": self.directional_bars,
            "neutral_bars": self.neutral_bars,
            "ambiguous_bars": self.ambiguous_bars,
            "episodes": self.episodes,
            "fragments": self.fragments,
            "against_open_episodes": self.against_open_episodes,
            "target_hits": self.target_hits,
            "stop_hits": self.stop_hits,
            "timeouts": self.timeouts,
            "censored": self.censored,
            "hit_rate": self.hit_rate,
            "mean_mfe_pct": self.mean_mfe_pct,
            "mean_mae_pct": self.mean_mae_pct,
            "mean_market_move_pct": self.mean_market_move_pct,
        }


@dataclass(frozen=True)
class CrossGrammarOverlap:
    """Same-bar co-detection. Opposite directions are separate hypotheses."""

    bars_with_multiple_grammars: int
    bars_with_opposing_directions: int
    pairwise_co_detections: tuple[tuple[str, str, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bars_with_multiple_grammars": self.bars_with_multiple_grammars,
            "bars_with_opposing_directions": self.bars_with_opposing_directions,
            "pairwise_co_detections": [
                {"a": a, "b": b, "bars": n} for a, b, n in self.pairwise_co_detections
            ],
        }


@dataclass(frozen=True)
class PanelGateAudit:
    """Arithmetic of the configured expert gate; an audit, not a new gate."""

    expert_count: int
    min_support_quorum: int
    max_contradiction_tolerance: int
    contradiction_can_block: bool
    min_support_can_pass_with: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "expert_count": self.expert_count,
            "min_support_quorum": self.min_support_quorum,
            "max_contradiction_tolerance": self.max_contradiction_tolerance,
            "contradiction_can_block": self.contradiction_can_block,
            "min_support_can_pass_with": self.min_support_can_pass_with,
            "note": self.note,
        }


@dataclass(frozen=True)
class GrammarCensus:
    instrument_id: str
    bars: int
    tape_sha256: str
    version: str
    claim_status: str
    authority: str
    bracket_stop_pct: str
    bracket_target_pct: str
    distributions: tuple[GrammarDistribution, ...]
    overlap: CrossGrammarOverlap
    panel_gate: PanelGateAudit
    evidence_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "instrument_id": self.instrument_id,
            "bars": self.bars,
            "tape_sha256": self.tape_sha256,
            "authority": self.authority,
            "claim_status": self.claim_status,
            "bracket_stop_pct": self.bracket_stop_pct,
            "bracket_target_pct": self.bracket_target_pct,
            "costs_applied": "NONE",
            "distributions": [d.as_dict() for d in self.distributions],
            "overlap": self.overlap.as_dict(),
            "panel_gate": self.panel_gate.as_dict(),
            "evidence_digest": self.evidence_digest,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def panel_gate_audit(
    *,
    expert_count: int,
    min_support_quorum: int,
    max_contradiction_tolerance: int,
) -> PanelGateAudit:
    """Report whether the configured gate can ever refuse a detection.

    A contradiction can only block when the tolerance is strictly smaller than
    the number of experts that can contradict. A tolerance equal to the panel
    size makes the condition vacuous, so the panel would admit every detection
    that reaches one support vote.
    """
    if expert_count <= 0:
        raise ValueError("expert_count must be positive")
    if not 0 <= min_support_quorum <= expert_count:
        raise ValueError("min_support_quorum must be within [0, expert_count]")
    if max_contradiction_tolerance < 0:
        raise ValueError("max_contradiction_tolerance must be non-negative")
    can_block = max_contradiction_tolerance < expert_count
    quorum = (
        "a single support vote"
        if min_support_quorum <= 1
        else f"{min_support_quorum} support votes"
    )
    note = (
        "contradiction is binding"
        if can_block
        else (
            f"contradiction can never block: {expert_count} experts exist and "
            f"{max_contradiction_tolerance} contradictions are tolerated, so the "
            "gate reduces to the support quorum alone"
        )
    )
    return PanelGateAudit(
        expert_count=expert_count,
        min_support_quorum=min_support_quorum,
        max_contradiction_tolerance=max_contradiction_tolerance,
        contradiction_can_block=can_block,
        min_support_can_pass_with=quorum,
        note=note,
    )


@dataclass
class _OpenEpisode:
    policy: str
    direction: str
    anchor_ns: int
    anchor_index: int
    entry: Decimal
    horizon_bars: int
    identity_status: str
    fragments: int = 0
    against_open_episode: bool = False


def _validate_stream(candles: Sequence[Candle], instrument_id: str) -> None:
    if not candles:
        raise ValueError("census needs at least one candle")
    previous_end: int | None = None
    for candle in candles:
        if candle.instrument_id != instrument_id:
            raise ValueError("census stream must carry one instrument")
        if candle.available_ns is None:
            raise ValueError("census candle has no availability time")
        if candle.available_ns > candle.end_ns:
            raise ValueError("census candle became available after its own decision time")
        if previous_end is not None and candle.start_ns < previous_end:
            raise ValueError("census stream is not strictly ordered")
        previous_end = candle.end_ns


def _build_frames(candles: Sequence[Candle], instrument_id: str) -> list[CausalFrame]:
    """One causal frame per bar, sharing the same validated parent chain.

    ``continuous`` is carried as a hint computed from the same stream instead of
    being rescanned on every policy call; the scan would return exactly this
    value because every frame is a prefix of one ordered stream.
    """
    frames: list[CausalFrame] = []
    parent: CausalFrame | None = None
    continuous = True
    for index, candle in enumerate(candles):
        if index:
            continuous = continuous and candles[index - 1].end_ns == candle.start_ns
        frame = frame_at_incremental(
            parent,
            instrument_id,
            candle.end_ns,
            tuple(candles[: index + 1]),
        )
        if parent is not None and frame.candles[-1] != candle:
            raise ValueError("incremental frame dropped the current bar")
        frame = replace(frame, _continuous_hint=continuous)
        frames.append(frame)
        parent = frame
    return frames


def _resolve_outcome(
    candles: Sequence[Candle],
    episode: _OpenEpisode,
    *,
    stop_pct: Decimal,
    target_pct: Decimal,
) -> EpisodeOutcome:
    """Walk the episode window; stop is evaluated before target inside a bar."""
    entry = episode.entry
    long_side = episode.direction == "LONG"
    if long_side:
        stop_price = entry * (1 - stop_pct)
        target_price = entry * (1 + target_pct)
    else:
        stop_price = entry * (1 + stop_pct)
        target_price = entry * (1 - target_pct)
    mfe = 0.0
    mae = 0.0
    last_close: Decimal | None = None
    bars_held = 0
    for offset in range(1, episode.horizon_bars + 1):
        index = episode.anchor_index + offset
        if index >= len(candles):
            return EpisodeOutcome(
                endpoint=CENSORED,
                bars_held=bars_held,
                mfe_pct=mfe * 100 if bars_held else None,
                mae_pct=mae * 100 if bars_held else None,
                market_move_pct=(
                    None
                    if last_close is None
                    else float((last_close - entry) / entry) * 100 * (1 if long_side else -1)
                ),
            )
        bar = candles[index]
        bars_held = offset
        last_close = bar.close
        if long_side:
            favourable = float((bar.high - entry) / entry)
            adverse = float((entry - bar.low) / entry)
            stop_touched = bar.low <= stop_price
            target_touched = bar.high >= target_price
        else:
            favourable = float((entry - bar.low) / entry)
            adverse = float((bar.high - entry) / entry)
            stop_touched = bar.high >= stop_price
            target_touched = bar.low <= target_price
        mfe = max(mfe, favourable)
        mae = max(mae, adverse)
        if stop_touched or target_touched:
            endpoint = STOP_HIT if stop_touched else TARGET_HIT
            return EpisodeOutcome(
                endpoint=endpoint,
                bars_held=offset,
                mfe_pct=mfe * 100,
                mae_pct=mae * 100,
                market_move_pct=float((bar.close - entry) / entry) * 100 * (1 if long_side else -1),
            )
    return EpisodeOutcome(
        endpoint=TIMEOUT,
        bars_held=bars_held,
        mfe_pct=mfe * 100,
        mae_pct=mae * 100,
        market_move_pct=(
            None
            if last_close is None
            else float((last_close - entry) / entry) * 100 * (1 if long_side else -1)
        ),
    )


def _mean(values: Iterable[float]) -> float | None:
    collected = list(values)
    return None if not collected else sum(collected) / len(collected)


def run_grammar_census(
    candles: Sequence[Candle],
    *,
    instrument_id: str | None = None,
    policies: Sequence[str] = GRAMMAR_ORDER,
    stop_pct: Decimal = STOP_PCT,
    target_pct: Decimal = TARGET_PCT,
    expert_count: int = 28,
    min_support_quorum: int = 1,
    max_contradiction_tolerance: int = 28,
    tape_sha256: str = "",
) -> GrammarCensus:
    """Evaluate every grammar on every bar of one PIT stream; no orders."""
    if not policies:
        raise ValueError("census needs at least one grammar")
    unknown = [policy for policy in policies if policy not in POLICIES]
    if unknown:
        raise ValueError(f"unknown opportunity grammar: {sorted(unknown)}")
    if len(set(policies)) != len(policies):
        raise ValueError("duplicate grammar in census request")
    stream = tuple(candles)
    if not stream:
        raise ValueError("census needs at least one candle")
    resolved_instrument = instrument_id or stream[0].instrument_id
    _validate_stream(stream, resolved_instrument)
    frames = _build_frames(stream, resolved_instrument)

    episodes: dict[str, list[GrammarEpisode]] = {policy: [] for policy in policies}
    open_episodes: dict[str, dict[str, _OpenEpisode]] = {policy: {} for policy in policies}
    counters: dict[str, dict[str, int]] = {
        policy: {
            "bars_evaluated": 0,
            "directional_bars": 0,
            "neutral_bars": 0,
            "ambiguous_bars": 0,
        }
        for policy in policies
    }
    detections_by_bar: list[dict[str, str]] = [dict() for _ in stream]

    for index, frame in enumerate(frames):
        bar_ns = stream[index].end_ns
        duration_ns = stream[index].end_ns - stream[index].start_ns
        for policy in policies:
            if len(frame.candles) < POLICY_REQUIRED_BARS[policy]:
                continue
            counters[policy]["bars_evaluated"] += 1
            opportunity = grammar_opportunity(frame, policy)
            if opportunity is None:
                continue
            live = open_episodes[policy]
            expired = [
                direction
                for direction, episode in live.items()
                if bar_ns >= episode.anchor_ns + episode.horizon_bars * duration_ns
            ]
            for direction in expired:
                episodes[policy].append(_close_episode(live.pop(direction), stream, stop_pct, target_pct))
            if opportunity.direction == "NEUTRAL":
                counters[policy]["neutral_bars"] += 1
                continue
            if opportunity.direction not in {"LONG", "SHORT"}:
                continue
            counters[policy]["directional_bars"] += 1
            if opportunity.identity_status != "CANONICAL":
                counters[policy]["ambiguous_bars"] += 1
            detections_by_bar[index][policy] = opportunity.direction
            existing = live.get(opportunity.direction)
            if existing is not None:
                existing.fragments += 1
                continue
            live[opportunity.direction] = _OpenEpisode(
                policy=policy,
                direction=opportunity.direction,
                anchor_ns=opportunity.anchor_ns,
                anchor_index=index,
                entry=stream[index].close,
                horizon_bars=POLICY_HORIZON_BARS[policy],
                identity_status=opportunity.identity_status,
                against_open_episode=bool(live),
            )

    for policy in policies:
        for episode in open_episodes[policy].values():
            episodes[policy].append(_close_episode(episode, stream, stop_pct, target_pct))

    distributions = tuple(
        _distribution(policy, counters[policy], episodes[policy]) for policy in policies
    )
    overlap = _overlap(policies, detections_by_bar)
    gate = panel_gate_audit(
        expert_count=expert_count,
        min_support_quorum=min_support_quorum,
        max_contradiction_tolerance=max_contradiction_tolerance,
    )
    census = GrammarCensus(
        instrument_id=resolved_instrument,
        bars=len(stream),
        tape_sha256=tape_sha256,
        version=CENSUS_VERSION,
        claim_status=CLAIM_STATUS,
        authority=AUTHORITY,
        bracket_stop_pct=str(stop_pct),
        bracket_target_pct=str(target_pct),
        distributions=distributions,
        overlap=overlap,
        panel_gate=gate,
    )
    return replace(census, evidence_digest=_digest(census))


def _close_episode(
    episode: _OpenEpisode,
    candles: Sequence[Candle],
    stop_pct: Decimal,
    target_pct: Decimal,
) -> GrammarEpisode:
    return GrammarEpisode(
        policy=episode.policy,
        direction=episode.direction,
        anchor_ns=episode.anchor_ns,
        horizon_bars=episode.horizon_bars,
        entry_price=float(episode.entry),
        identity_status=episode.identity_status,
        fragments=episode.fragments,
        against_open_episode=episode.against_open_episode,
        outcome=_resolve_outcome(
            candles, episode, stop_pct=stop_pct, target_pct=target_pct
        ),
    )


def _distribution(
    policy: str, counters: dict[str, int], episodes: Sequence[GrammarEpisode]
) -> GrammarDistribution:
    endpoints = [episode.outcome.endpoint for episode in episodes if episode.outcome is not None]
    resolved = [episode for episode in episodes if episode.outcome is not None]
    decisive = [
        episode
        for episode in resolved
        if episode.outcome is not None
        and episode.outcome.endpoint in (TARGET_HIT, STOP_HIT, TIMEOUT)
    ]
    targets = endpoints.count(TARGET_HIT)
    return GrammarDistribution(
        policy=policy,
        warmup_bars=POLICY_REQUIRED_BARS[policy],
        horizon_bars=POLICY_HORIZON_BARS[policy],
        bars_evaluated=counters["bars_evaluated"],
        directional_bars=counters["directional_bars"],
        neutral_bars=counters["neutral_bars"],
        ambiguous_bars=counters["ambiguous_bars"],
        episodes=len(episodes),
        fragments=sum(episode.fragments for episode in episodes),
        against_open_episodes=sum(1 for episode in episodes if episode.against_open_episode),
        target_hits=targets,
        stop_hits=endpoints.count(STOP_HIT),
        timeouts=endpoints.count(TIMEOUT),
        censored=endpoints.count(CENSORED),
        hit_rate=(targets / len(decisive)) if decisive else None,
        mean_mfe_pct=_mean(
            float(episode.outcome.mfe_pct)
            for episode in decisive
            if episode.outcome is not None and episode.outcome.mfe_pct is not None
        ),
        mean_mae_pct=_mean(
            float(episode.outcome.mae_pct)
            for episode in decisive
            if episode.outcome is not None and episode.outcome.mae_pct is not None
        ),
        mean_market_move_pct=_mean(
            float(episode.outcome.market_move_pct)
            for episode in decisive
            if episode.outcome is not None and episode.outcome.market_move_pct is not None
        ),
    )


def _overlap(
    policies: Sequence[str], detections_by_bar: Sequence[dict[str, str]]
) -> CrossGrammarOverlap:
    multiple = 0
    opposing = 0
    pairwise: dict[tuple[str, str], int] = {}
    for detections in detections_by_bar:
        if len(detections) >= 2:
            multiple += 1
            if len(set(detections.values())) >= 2:
                opposing += 1
        ordered = sorted(detections)
        for first in range(len(ordered)):
            for second in range(first + 1, len(ordered)):
                key = (ordered[first], ordered[second])
                pairwise[key] = pairwise.get(key, 0) + 1
    pairs = tuple(
        (first, second, count)
        for (first, second), count in sorted(
            pairwise.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    )
    return CrossGrammarOverlap(
        bars_with_multiple_grammars=multiple,
        bars_with_opposing_directions=opposing,
        pairwise_co_detections=pairs,
    )


def _digest(census: GrammarCensus) -> str:
    payload = census.as_dict()
    payload.pop("evidence_digest", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load_verified_candles(tape: Path, symbol: str) -> tuple[tuple[Candle, ...], str]:
    """Load a repository slice, refusing any tape whose bytes fail its manifest."""
    directory = tape if tape.is_dir() else tape.parent
    jsonl = directory / "tape.jsonl"
    manifest_path = directory / "tape.manifest.json"
    if not jsonl.is_file() or not manifest_path.is_file():
        raise ValueError(f"{directory} is not a verified tape slice")
    declared = json.loads(manifest_path.read_text()).get("tape_sha256")
    actual = sha256_file(jsonl)
    if not isinstance(declared, str) or declared != actual:
        raise ValueError(
            f"tape.jsonl hashes to {actual} but the manifest declares {declared!r}"
        )
    loaded = load_multitape(jsonl)
    candles = loaded.candles.get(symbol)
    if not candles:
        raise ValueError(f"{symbol!r} is absent from the tape; available: {sorted(loaded.candles)}")
    return tuple(candles), actual


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tape", required=True, type=Path, help="research/tape/<slice> directory")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--instrument-id", default=None)
    parser.add_argument(
        "--policies",
        default=",".join(GRAMMAR_ORDER),
        help="comma-separated grammar subset (default: all five)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the census JSON here")
    parser.add_argument("--expert-count", type=int, default=28)
    parser.add_argument("--min-support-quorum", type=int, default=1)
    parser.add_argument("--max-contradiction-tolerance", type=int, default=28)
    args = parser.parse_args(argv)

    candles, digest = _load_verified_candles(args.tape, args.symbol)
    instrument_id = args.instrument_id or candles[0].instrument_id
    census = run_grammar_census(
        candles,
        instrument_id=instrument_id,
        policies=tuple(p.strip() for p in str(args.policies).split(",") if p.strip()),
        expert_count=args.expert_count,
        min_support_quorum=args.min_support_quorum,
        max_contradiction_tolerance=args.max_contradiction_tolerance,
        tape_sha256=digest,
    )
    payload = census.to_json()
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
