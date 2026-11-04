"""O1 canonical episode extractor — port of v8-core/src/oracle/episode.rs (D-138).

Tiers (TARGET_ORACLE_SPEC §§1–11):
- O0 (raw horizon excursion) is a diagnostic only: ``o0_forward_excursion`` measures realized
  forward excursion per event for evaluators. Its output is labeled ``EVALUATOR_ONLY`` and
  must never be fed to decision features (R1).
- O1 (episode oracle): causal, non-overlapping directional swings — the canonical denominator
  for recall. Pure function over a polars frame (lib-first: polars owns the frame).
- O2 (feasible portfolio): capital/concurrency/margin/fees — not in this substrate.

DIVERGENCES from the Rust module: identity digests use hashlib (sha256) with the same
domain tags; reproducible inside this port, not bit-equal to the Rust blake3 Canon digests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

import polars as pl

__all__ = [
    "EPISODE_FRAME_COLUMNS",
    "O0_EVALUATOR_ONLY",
    "OracleDefinition",
    "OracleEpisode",
    "OracleEpisodeExtractor",
    "OracleTier",
    "o0_forward_excursion",
]


class OracleTier(StrEnum):
    O0_RAW_HORIZON = "O0_RAW_HORIZON"
    O1_EPISODE = "O1_EPISODE"
    O2_FEASIBLE_PORTFOLIO = "O2_FEASIBLE_PORTFOLIO"


#: Columns the extractor reads from the input frame (all f64).
EPISODE_FRAME_COLUMNS = ("high", "low", "close", "volume", "atr")

#: Marker carried by every O0 diagnostic row: evaluator-only, never a decision feature.
O0_EVALUATOR_ONLY = "EVALUATOR_ONLY"


def _digest(domain: str, payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(f"{domain}|{blob}".encode()).hexdigest()


@dataclass(frozen=True)
class OracleDefinition:
    """Canonical parameters defining the O1 measurement frame (D-138)."""

    symbol: str
    horizon_bars: int
    min_mfe_pct: float
    max_mae_pct: float
    min_rr_ratio: float
    roundtrip_friction_bps: float
    non_overlapping_policy: bool
    definition_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "definition_id",
            _digest(
                "OracleDefinition-v1",
                {
                    "symbol": self.symbol,
                    "horizon_bars": self.horizon_bars,
                    "min_mfe_pct": self.min_mfe_pct,
                    "max_mae_pct": self.max_mae_pct,
                    "min_rr_ratio": self.min_rr_ratio,
                    "roundtrip_friction_bps": self.roundtrip_friction_bps,
                    "non_overlapping_policy": self.non_overlapping_policy,
                },
            ),
        )


@dataclass(frozen=True)
class OracleEpisode:
    """One structured non-overlapping O1 opportunity episode."""

    episode_id: str
    definition_id: str
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_bar: int
    exit_bar: int
    duration_bars: int
    entry_price: float
    optimal_exit_price: float
    gross_mfe_pct: float
    gross_mae_pct: float
    gross_r: float
    habitat_type: str

    def compute_id(self) -> str:
        return _digest(
            "OracleEpisode-v1",
            {
                "definition_id": self.definition_id,
                "symbol": self.symbol,
                "direction": self.direction,
                "entry_bar": self.entry_bar,
                "exit_bar": self.exit_bar,
                "entry_price": self.entry_price,
                "optimal_exit_price": self.optimal_exit_price,
            },
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "definition_id": self.definition_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_bar": self.entry_bar,
            "exit_bar": self.exit_bar,
            "duration_bars": self.duration_bars,
            "entry_price": self.entry_price,
            "optimal_exit_price": self.optimal_exit_price,
            "gross_mfe_pct": self.gross_mfe_pct,
            "gross_mae_pct": self.gross_mae_pct,
            "gross_r": self.gross_r,
            "habitat_type": self.habitat_type,
        }


def _columns(frame: pl.DataFrame) -> dict[str, list[float]]:
    missing = [c for c in EPISODE_FRAME_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"episode frame is missing columns: {missing}")
    return {c: [float(v) for v in frame[c].to_list()] for c in EPISODE_FRAME_COLUMNS}


def o0_forward_excursion(
    frame: pl.DataFrame, horizon_bars: int
) -> list[dict[str, object]]:
    """O0 diagnostic: realized forward excursion per bar (evaluator-only).

    Returns ``EVALUATOR_ONLY`` rows; callers must never feed these to decision features.
    """
    cols = _columns(frame)
    closes, highs, lows = cols["close"], cols["high"], cols["low"]
    out: list[dict[str, object]] = []
    for i in range(len(closes)):
        fwd_end = min(i + horizon_bars + 1, len(closes))
        window_h = highs[i + 1 : fwd_end]
        window_l = lows[i + 1 : fwd_end]
        out.append(
            {
                "bar": i,
                "tier": OracleTier.O0_RAW_HORIZON.value,
                "usage": O0_EVALUATOR_ONLY,
                "forward_max_gain_pct": (max(window_h) - closes[i]) / closes[i] if window_h else None,
                "forward_max_loss_pct": (closes[i] - min(window_l)) / closes[i] if window_l else None,
            }
        )
    return out


class OracleEpisodeExtractor:
    """Canonical O1 episode extraction (pure function over a polars frame)."""

    @staticmethod
    def extract_episodes(definition: OracleDefinition, frame: pl.DataFrame) -> list[OracleEpisode]:
        cols = _columns(frame)
        highs, lows, closes = cols["high"], cols["low"], cols["close"]
        volumes, atrs = cols["volume"], cols["atr"]
        n_bars = len(closes)
        episodes: list[OracleEpisode] = []
        i = 0
        horizon = definition.horizon_bars
        while i + horizon < n_bars:
            entry_price = closes[i]
            atr = max(atrs[i] if i < len(atrs) else 1.0, 1e-6)
            fwd_end = min(i + horizon + 1, n_bars)
            fwd_highs = highs[i + 1 : fwd_end]
            fwd_lows = lows[i + 1 : fwd_end]
            if not fwd_highs or not fwd_lows:
                i += 1
                continue
            max_h = max(fwd_highs)
            max_h_bar = i + 1 + fwd_highs.index(max_h)
            min_l = min(fwd_lows)
            min_l_bar = i + 1 + fwd_lows.index(min_l)

            long_mae_price = min(lows[i + 1 : max_h_bar + 1])
            long_mfe_pct = (max_h - entry_price) / entry_price
            long_mae_pct = max(entry_price - long_mae_price, 0.0) / entry_price

            short_mae_price = max(highs[i + 1 : min_l_bar + 1])
            short_mfe_pct = (entry_price - min_l) / entry_price
            short_mae_pct = max(short_mae_price - entry_price, 0.0) / entry_price

            admitted: OracleEpisode | None = None
            volume_i = volumes[i] if i < len(volumes) else 1.0
            habitat = "TrendExpansionBreakout" if volume_i > 1.2 else "TrendPullbackContinuation"
            if (
                long_mfe_pct >= definition.min_mfe_pct
                and long_mae_pct <= definition.max_mae_pct
                and (long_mfe_pct / max(long_mae_pct, 0.001)) >= definition.min_rr_ratio
            ):
                duration = max(max_h_bar - i, 1)
                admitted = OracleEpisode(
                    episode_id="",
                    definition_id=definition.definition_id,
                    symbol=definition.symbol,
                    direction="LONG",
                    entry_bar=i,
                    exit_bar=max_h_bar,
                    duration_bars=duration,
                    entry_price=entry_price,
                    optimal_exit_price=max_h,
                    gross_mfe_pct=long_mfe_pct * 100.0,
                    gross_mae_pct=long_mae_pct * 100.0,
                    gross_r=(max_h - entry_price) / atr,
                    habitat_type=habitat,
                )
            elif (
                short_mfe_pct >= definition.min_mfe_pct
                and short_mae_pct <= definition.max_mae_pct
                and (short_mfe_pct / max(short_mae_pct, 0.001)) >= definition.min_rr_ratio
            ):
                duration = max(min_l_bar - i, 1)
                admitted = OracleEpisode(
                    episode_id="",
                    definition_id=definition.definition_id,
                    symbol=definition.symbol,
                    direction="SHORT",
                    entry_bar=i,
                    exit_bar=min_l_bar,
                    duration_bars=duration,
                    entry_price=entry_price,
                    optimal_exit_price=min_l,
                    gross_mfe_pct=short_mfe_pct * 100.0,
                    gross_mae_pct=short_mae_pct * 100.0,
                    gross_r=(entry_price - min_l) / atr,
                    habitat_type=habitat,
                )
            if admitted is not None:
                object.__setattr__(admitted, "episode_id", admitted.compute_id())
                episodes.append(admitted)
                i = admitted.exit_bar if definition.non_overlapping_policy else i + 1
            else:
                i += 1
        return episodes

    @staticmethod
    def extract_from_sequences(
        definition: OracleDefinition,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        volumes: Sequence[float],
        atrs: Sequence[float],
    ) -> list[OracleEpisode]:
        """Sequence entry point (tests/fixtures); the frame form above is canonical."""
        frame = pl.DataFrame(
            {
                "high": [float(v) for v in highs],
                "low": [float(v) for v in lows],
                "close": [float(v) for v in closes],
                "volume": [float(v) for v in volumes],
                "atr": [float(v) for v in atrs],
            }
        )
        return OracleEpisodeExtractor.extract_episodes(definition, frame)
