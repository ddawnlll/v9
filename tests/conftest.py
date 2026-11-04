"""Shared pytest boundary for v8-next tests.

- Registers the ``slow`` marker: tests touching the real tape / engines that take
  seconds (not milliseconds). The fast loop is the DEFAULT: bare ``pytest`` skips
  ``slow`` via ``addopts``; explicit ``-m "not slow"`` is accepted but redundant.
- Registers the ``dangerous-full-loop`` marker: auto-applied to every collected
  test, so ``pytest -m dangerous-full-loop`` runs the FULL suite (fast + slow).
  Dangerous: minutes of wall time, GBs of RAM (measured worker death under
  ``-n 2`` xdist); never ``-n auto``, at most ``-n 2``, prefer serial. Milestones
  and releases only — never the dev loop.
- Session-scoped cached tape loaders: repeated ``load_multitape`` /
  ``load_tape_candles`` calls with identical arguments hit an in-worker cache instead
  of re-reading the JSONL tape. (With xdist each worker keeps its own cache.)
- Worker ceiling (R2): never ``-n auto`` (per-worker caches duplicate memory;
  measured OOM at 10GB). Hard ceiling is ``-n 2``. Fast loop::

      uv run --project v8-next --extra dev pytest -q v8-next/tests

  Full suite at milestones (dangerous)::

      uv run --project v8-next --extra dev pytest -q -m dangerous-full-loop v8-next/tests
- Testmon-style selection ONLY with the tape-mtime guard (R3): a tape change
  forces the full suite; frozen-OOS tests are never skipped silently. Take a
  snapshot with :func:`tape_snapshot` before a selective run and call
  :func:`assert_tape_unchanged` afterwards; any change raises loudly instead
  of reporting a selective pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from v8_next.evaluation.gate_resolution import load_tape_candles
from v8_next.evaluation.multitape import MultiTape, load_multitape

#: Hard xdist ceiling (R2): per-worker tape caches duplicate memory.
XDIST_CEILING = 2

_MULTITAPE_CACHE: dict[tuple[str, int | None, int], MultiTape] = {}
_CANDLES_CACHE: dict[tuple[str, int | None, str | None], tuple[Any, ...]] = {}


def tape_snapshot(paths: list[str | Path]) -> dict[str, int]:
    """Snapshot tape mtimes (ns) for the selection guard (R3)."""
    snap: dict[str, int] = {}
    for raw in paths:
        p = Path(raw)
        try:
            snap[str(p)] = p.stat().st_mtime_ns
        except OSError:
            snap[str(p)] = -1
    return snap


def assert_tape_unchanged(before: dict[str, int]) -> None:
    """Fail loudly when a tape changed under a selective run (R3).

    A changed tape forces the full suite; selective results under a changed
    tape are never reported as passes.
    """
    for raw, old in before.items():
        try:
            now = Path(raw).stat().st_mtime_ns
        except OSError:
            now = -1
        if now != old:
            raise AssertionError(
                f"TAPE_CHANGED_UNDER_SELECTIVE_RUN: {raw} changed; "
                "rerun the full suite instead of reporting selective results"
            )
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: real-tape or engine tests taking seconds (excluded from fast runs)"
    )
    config.addinivalue_line(
        "markers",
        "dangerous-full-loop: full-suite membership (auto-applied to every test; "
        "select it to run everything: -m dangerous-full-loop)",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-apply ``dangerous-full-loop`` so the full suite has one opt-in name."""
    for item in items:
        item.add_marker("dangerous-full-loop")


@pytest.fixture(scope="session")
def cached_multitape() -> Any:
    """Session-cached ``load_multitape`` keyed by (path, limit, offset)."""

    def _load(
        path: str | Path,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> MultiTape:
        if kwargs:
            # Flag-selected paths (e.g. parquet) bypass the plain cache;
            # identical objects still come from the same loader.
            return load_multitape(Path(path), limit=limit, offset=offset, **kwargs)
        key = (str(path), limit, offset)
        tape = _MULTITAPE_CACHE.get(key)
        if tape is None:
            tape = load_multitape(Path(path), limit=limit, offset=offset)
            _MULTITAPE_CACHE[key] = tape
        return tape

    return _load


@pytest.fixture(scope="session")
def cached_tape_candles() -> Any:
    """Session-cached ``load_tape_candles`` keyed by (path, limit, instrument)."""

    def _load(
        path: str | Path, limit: int | None = None, instrument: str | None = None
    ) -> tuple[Any, ...]:
        key = (str(path), limit, instrument)
        candles = _CANDLES_CACHE.get(key)
        if candles is None:
            candles = (
                tuple(load_tape_candles(Path(path), limit=limit, instrument=instrument))
                if instrument is not None
                else tuple(load_tape_candles(Path(path), limit=limit))
            )
            _CANDLES_CACHE[key] = candles
        return candles

    return _load
