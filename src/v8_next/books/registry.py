"""books/ → opportunity grammar → BenchmarkCase mapping (literature loop entry).

The repository library (repo-root `books/`, ~160 files) is the strategy source;
this registry is the only authorized bridge from a book to an executable
BenchmarkCase. A book the loop has never mapped is `unmapped` and invisible to
evaluation — no BenchmarkCase may cite an unmapped book.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.evaluation.runner import BenchmarkCase


class BookMapping(BaseModel):
    """One literature → policy → case binding."""

    model_config = ConfigDict(frozen=True)

    mapping_id: str
    filename: str
    experts: tuple[str, ...] = ()
    grammar_policy: str = "range-breakout-48-v1"
    regime_family: str = "all"
    case_id: str = ""
    dataset_name: str = "BTCUSDT-1H-REAL"
    notes: str = ""


MAPPINGS: tuple[BookMapping, ...] = (
    BookMapping(
        mapping_id="squeeze-swing",
        filename="Algorithmic Trading - Winning Strategies and Their Rationale 2013.pdf",
        experts=("squeeze_swing",),
        grammar_policy="range-breakout-48-v1",
        regime_family="volatility_compression",
        case_id="BC-BOOKS-SQUEEZE-01",
        notes="Volatility-compression breakout rationale → squeeze observer + range-breakout grammar.",
    ),
    BookMapping(
        mapping_id="candlestick-reversal",
        filename="Beyond Candlesticks - New Japanese Charting Techniques Revealed 1994.pdf",
        experts=("candlestick_reversal",),
        grammar_policy="range-breakout-48-v1",
        regime_family="reversal",
        case_id="BC-BOOKS-CANDLE-01",
        notes="Japanese reversal patterns → candlestick expert family.",
    ),
    BookMapping(
        mapping_id="turtle-donchian",
        filename="Way of the Turtle - The Secret Methods that Turned Ordinary People into Legendary Traders 2007.pdf",
        experts=("donchian_breakout",),
        grammar_policy="range-breakout-48-v1",
        regime_family="trend_breakout",
        case_id="BC-BOOKS-TURTLE-01",
        notes="Turtle channel-breakout system → Donchian breakout expert.",
    ),
    BookMapping(
        mapping_id="volume-price-analysis",
        filename="A Complete Guide To Volume Price Analysis 2013.pdf",
        experts=("volume_confirmed_breakout", "volume_climax_reversal", "obv_adl_regime"),
        grammar_policy="compression-expansion-v2",
        regime_family="volume_confirmation",
        case_id="BC-BOOKS-VPA-01",
        notes="Effort-vs-result / volume-confirmed breakout rationale → volume expert family + compression-expansion grammar.",
    ),
    BookMapping(
        mapping_id="evidence-based-selection",
        filename="Evidence-Based Technical Analysis - Applying the Scientific Method and Statistical Inference to Trading Signals 2007.pdf",
        experts=(),
        grammar_policy="range-breakout-48-v1",
        regime_family="all",
        case_id="BC-BOOKS-EVIDENCE-01",
        notes="Statistical-inference discipline → ensemble case held to G5 selection-control scrutiny.",
    ),
)


def get_mapping(mapping_id: str) -> BookMapping:
    for m in MAPPINGS:
        if m.mapping_id == mapping_id:
            return m
    raise KeyError(f"unmapped book id: {mapping_id}")


def build_case(
    mapping_id: str,
    *,
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
    min_support_quorum: int = 1,
    max_contradiction_tolerance: int = 28,
) -> BenchmarkCase:
    """Build the executable BenchmarkCase a mapped book authorizes."""
    m = get_mapping(mapping_id)
    return BenchmarkCase(
        case_id=m.case_id,
        policy_id=f"pol_books_{m.mapping_id}",
        dataset_name=m.dataset_name,
        instrument_id=instrument_id,
        strategy_config=ExpertStrategyConfig(
            min_support_quorum=min_support_quorum,
            max_contradiction_tolerance=max_contradiction_tolerance,
            grammar_policy=m.grammar_policy,
            bracket_stop_pct=Decimal("0.02"),
            bracket_target_pct=Decimal("0.04"),
        ),
        allowed_populations=("BurnedDiagnosticReal",),
    )


def coverage(books_dir: Path | str) -> dict[str, Any]:
    """Report mapped vs unmapped library coverage (unpinned: library grows)."""
    root = Path(books_dir)
    files = (
        sorted(p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".pdf", ".epub", ".mobi"})
        if root.is_dir()
        else []
    )
    mapped_files = {m.filename for m in MAPPINGS}
    unmapped = [f for f in files if f not in mapped_files]
    return {
        "total": len(files),
        "mapped": len([f for f in files if f in mapped_files]),
        "unmapped": len(unmapped),
        "unmapped_sample": unmapped[:10],
        "mapping_ids": [m.mapping_id for m in MAPPINGS],
    }
