"""Reusable economic-research capabilities for V8-next.

Scope
-----
This package holds *research-only* policy comparison, evidence export and
metrics aggregation. It never changes a production default: every policy is
opt-in, and the historical baseline path is unchanged when no research
configuration is supplied.

Two distinct kinds of evidence are produced here and are never conflated:

``OBSERVED_NATIVE``
    Rows that come from a NautilusTrader replay (fills, positions, accounting).

``MODEL_DERIVED``
    Rows produced by :mod:`v8_next.research.screen`, a causal bar-path
    evaluator with declared intrabar assumptions. These are counterfactuals,
    not executions.
"""

from __future__ import annotations

__all__ = [
    "arms",
    "contract",
    "metrics",
    "screen",
]

__version__ = "0.1.0"
