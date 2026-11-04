"""Parquet dual-loader parity (#467).

MECHANICS ONLY section: none (no synthetic tape; parity is verified on real
windows only). Real-window section: JSONL-oracle vs parquet candle streams
element-identical on real windows when the parquet file exists (ceremony runs
in Wave 2); skips when absent. Default stays JSONL until the R3 gated cutover.
Schema mismatch / missing struct field fails loudly, never with defaults.
No Decimal-type change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.multitape import PARQUET_TAPE_NAME

QUAD_DIR = tape_path("quad-1h-12m")
QUAD_JSONL = QUAD_DIR / "tape.jsonl"


def _stream_hash(tape) -> dict[str, tuple[tuple[int, str, str, str, str], ...]]:
    out: dict[str, tuple[tuple[int, str, str, str, str], ...]] = {}
    for inst in tape.instruments:
        out[inst] = tuple(
            (c.end_ns, str(c.open), str(c.high), str(c.low), str(c.close))
            for c in tape.candles[inst]
        )
    return out


def test_parquet_missing_fails_loudly(tmp_path: Path, cached_multitape: Any) -> None:
    with pytest.raises(FileNotFoundError, match="parquet tape not found"):
        cached_multitape(tmp_path, limit=10, tape_format="parquet")


def test_parquet_parity_on_real_window(cached_multitape: Any) -> None:
    if not QUAD_JSONL.exists():
        pytest.skip(f"real quad tape absent at {QUAD_JSONL}")
    parquet = QUAD_DIR / PARQUET_TAPE_NAME
    if not parquet.exists():
        pytest.skip("parquet ceremony not yet executed (Wave 2); JSONL remains oracle")
    oracle = cached_multitape(QUAD_DIR, limit=100, tape_format="jsonl")
    columnar = cached_multitape(QUAD_DIR, limit=100, tape_format="parquet")
    assert _stream_hash(oracle) == _stream_hash(columnar)
    assert [r.funding_time_ms for r in oracle.funding] == [
        r.funding_time_ms for r in columnar.funding
    ]

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
