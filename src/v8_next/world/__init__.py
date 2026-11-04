"""Market World Foundry — synthetic correctness worlds (chain issue #453).

Thin lib-first envelope: ``arch`` owns stationary-bootstrap resampling,
``numpy``/``scipy`` own stochastic-vol/Hawkes/copula/surgery arithmetic.
Every world is seed-pinned, population-tagged (never research/holdout),
and carries a sha256 receipt. Proving output is correctness evidence
(AF-T12), explicitly NO_ECONOMIC_CLAIM.
"""

from v8_next.world.foundry import build_world
from v8_next.world.spec import (
    SyntheticPopulation,
    WorldBar,
    WorldFamily,
    WorldReceipt,
    WorldSpec,
)

__all__ = [
    "SyntheticPopulation",
    "WorldBar",
    "WorldFamily",
    "WorldReceipt",
    "WorldSpec",
    "build_world",
]
