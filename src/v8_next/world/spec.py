"""World spec + receipt — thin port of v8-core/src/world/spec.rs (D-147/D-149/D-150).

Only the substrate #453 runs on is ported: the 14-family enum, the
3-population isolation tag, the spec parameters, and the receipt identity.
Generator internals live in ``foundry.py``; execution lives in
``system_proving/run.py``. Digests use sha256 (reproducible here, not
bit-equal to the Rust Canon digests).

Isolation rule (I3): a synthetic population tag is never a research role.
``ResearchStore`` roles are observed-research / protected-holdout; worlds
carry ``SYNTHETIC:<POP>`` and any mix is refused by construction (worlds
never read research tape, so there is nothing to leak).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "SYNTHETIC_TAG_PREFIX",
    "SyntheticPopulation",
    "WorldBar",
    "WorldFamily",
    "WorldReceipt",
    "WorldSpec",
]

#: Population tag prefix — disjoint from every ResearchStore dataset role.
SYNTHETIC_TAG_PREFIX = "SYNTHETIC:"


class SyntheticPopulation(StrEnum):
    SYNTHETIC_DEV = "SYNTHETIC_DEV"
    SYNTHETIC_QUALIFICATION = "SYNTHETIC_QUALIFICATION"
    SYNTHETIC_NOVELTY = "SYNTHETIC_NOVELTY"


class WorldFamily(StrEnum):
    STRUCTURAL_REGIME = "STRUCTURAL_REGIME"
    BLOCK_RESAMPLED = "BLOCK_RESAMPLED"
    STATIONARY_BOOTSTRAP = "STATIONARY_BOOTSTRAP"
    STOCHASTIC_VOLATILITY = "STOCHASTIC_VOLATILITY"
    JUMP_CASCADE = "JUMP_CASCADE"
    CROSS_ASSET_CONTAGION = "CROSS_ASSET_CONTAGION"
    FUNDING_BASIS_WORLD = "FUNDING_BASIS_WORLD"
    LIQUIDITY_STRESS_WORLD = "LIQUIDITY_STRESS_WORLD"
    COUNTERFACTUAL_SURGERY = "COUNTERFACTUAL_SURGERY"
    PATH_TOPOLOGY_WORLD = "PATH_TOPOLOGY_WORLD"
    LEARNED_GENERATIVE_WORLD = "LEARNED_GENERATIVE_WORLD"
    AGENT_MARKET_WORLD = "AGENT_MARKET_WORLD"
    METAMORPHIC_WORLD = "METAMORPHIC_WORLD"
    REVERSE_STRESS_ADVERSARIAL = "REVERSE_STRESS_ADVERSARIAL"


def _digest(domain: str, payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(f"{domain}|{blob}".encode()).hexdigest()


@dataclass(frozen=True)
class WorldSpec:
    """Exact parameters generating one market world (cf. Rust ``WorldSpec``)."""

    family: WorldFamily
    population: SyntheticPopulation
    symbol: str
    n_bars: int
    base_price: float
    volatility_annualized: float
    jump_frequency: float
    jump_mean: float
    jump_std: float
    seed: int

    def __post_init__(self) -> None:
        if self.n_bars <= 0:
            raise ValueError("WORLD_SPEC_EMPTY: n_bars must be positive")
        if self.base_price <= 0.0:
            raise ValueError("WORLD_SPEC_EMPTY: base_price must be positive")

    @property
    def spec_id(self) -> str:
        return _digest("WorldSpec-v1", self.as_dict())

    @property
    def population_tag(self) -> str:
        return f"{SYNTHETIC_TAG_PREFIX}{self.population.value}"

    def as_dict(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "population": self.population.value,
            "symbol": self.symbol,
            "n_bars": self.n_bars,
            "base_price": self.base_price,
            "volatility_annualized": self.volatility_annualized,
            "jump_frequency": self.jump_frequency,
            "jump_mean": self.jump_mean,
            "jump_std": self.jump_std,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class WorldBar:
    index: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_ns: int

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "ts_ns": self.ts_ns,
        }


@dataclass(frozen=True)
class WorldReceipt:
    """One generated world: spec identity + bars + receipt digest (fail closed)."""

    world_id: str
    spec_id: str
    family: WorldFamily
    population: SyntheticPopulation
    symbol: str
    seed: int
    bars: tuple[WorldBar, ...]
    receipt_digest: str

    @classmethod
    def bind(cls, spec: WorldSpec, bars: tuple[WorldBar, ...]) -> WorldReceipt:
        if not bars:
            raise ValueError("WORLD_EMPTY: refusing receipt over zero bars")
        if len(bars) != spec.n_bars:
            raise ValueError(
                f"WORLD_LENGTH_MISMATCH: spec wants {spec.n_bars}, got {len(bars)}"
            )
        spec_id = spec.spec_id
        digest = _digest(
            "WorldReceipt-v1",
            {
                "spec_id": spec_id,
                "closes": [b.close for b in bars],
                "volumes": [b.volume for b in bars],
            },
        )
        return cls(
            world_id=f"world-{digest[:16]}",
            spec_id=spec_id,
            family=spec.family,
            population=spec.population,
            symbol=spec.symbol,
            seed=spec.seed,
            bars=bars,
            receipt_digest=digest,
        )

    @property
    def population_tag(self) -> str:
        return f"{SYNTHETIC_TAG_PREFIX}{self.population.value}"

    def as_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "spec_id": self.spec_id,
            "family": self.family.value,
            "population": self.population.value,
            "population_tag": self.population_tag,
            "symbol": self.symbol,
            "seed": self.seed,
            "n_bars": len(self.bars),
            "bars": [b.as_dict() for b in self.bars],
            "receipt_digest": self.receipt_digest,
            "claim": "NO_ECONOMIC_CLAIM",
        }
