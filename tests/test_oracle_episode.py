"""O1 episode extractor contracts (MECHANICS ONLY).

The bars below are hand-written structure to exercise the extractor arithmetic (the same
8-bar vectors as the Rust unit test); no assertion carries evaluative weight. The one
real-data claim this issue owes — coverage on a real window — lives in
`docs/evidence/v87-port-chain/452/`, not in a unit test on synthetic candles.
"""

from __future__ import annotations

import polars as pl
import pytest

from v8_next.oracle import (
    O0_EVALUATOR_ONLY,
    OracleDefinition,
    OracleEpisodeExtractor,
    OracleTier,
    o0_forward_excursion,
)

# The Rust unit-test vectors (episode.rs), hand-copied so the golden set is derivable.
HIGHS = [100.0, 101.0, 103.0, 101.0, 100.0, 105.0, 102.0, 101.0]
LOWS = [99.0, 99.5, 99.5, 99.0, 98.0, 100.0, 100.0, 99.0]
CLOSES = [100.0, 100.5, 102.5, 100.0, 99.0, 104.0, 101.0, 100.0]
VOLUMES = [1.0] * 8
ATRS = [1.0] * 8

DEF = OracleDefinition(
    symbol="BTCUSDT",
    horizon_bars=4,
    min_mfe_pct=0.02,
    max_mae_pct=0.01,
    min_rr_ratio=2.0,
    roundtrip_friction_bps=10.0,
    non_overlapping_policy=True,
)


def test_definition_identity_is_deterministic() -> None:
    again = OracleDefinition(
        symbol="BTCUSDT",
        horizon_bars=4,
        min_mfe_pct=0.02,
        max_mae_pct=0.01,
        min_rr_ratio=2.0,
        roundtrip_friction_bps=10.0,
        non_overlapping_policy=True,
    )
    assert DEF.definition_id == again.definition_id
    other = OracleDefinition(
        symbol="ETHUSDT",
        horizon_bars=4,
        min_mfe_pct=0.02,
        max_mae_pct=0.01,
        min_rr_ratio=2.0,
        roundtrip_friction_bps=10.0,
        non_overlapping_policy=True,
    )
    assert DEF.definition_id != other.definition_id


def test_o1_golden_episode_set() -> None:
    """Hand-derived from the vectors above: LONG(0→2), SHORT(2→4).

    The scan stops when ``i + horizon >= n_bars`` (bar 4 admits no window), so the
    LONG(4→5) swing the raw excursion suggests is correctly never an episode.
    """
    episodes = OracleEpisodeExtractor.extract_from_sequences(
        DEF, HIGHS, LOWS, CLOSES, VOLUMES, ATRS
    )
    assert [(e.direction, e.entry_bar, e.exit_bar) for e in episodes] == [
        ("LONG", 0, 2),
        ("SHORT", 2, 4),
    ]
    first, second = episodes
    assert first.gross_mfe_pct == pytest.approx(3.0)
    assert first.gross_mae_pct == pytest.approx(0.5)
    assert first.gross_r == pytest.approx(3.0)
    assert first.duration_bars == 2
    assert first.habitat_type == "TrendPullbackContinuation"
    assert second.gross_mfe_pct == pytest.approx((102.5 - 98.0) / 102.5 * 100.0)
    assert second.gross_mae_pct == pytest.approx(0.0)
    assert second.gross_r == pytest.approx((102.5 - 98.0) / 1.0)
    assert second.duration_bars == 2
    for episode in episodes:
        assert episode.episode_id == episode.compute_id()
        assert episode.definition_id == DEF.definition_id


def test_o1_episodes_do_not_overlap() -> None:
    episodes = OracleEpisodeExtractor.extract_from_sequences(
        DEF, HIGHS, LOWS, CLOSES, VOLUMES, ATRS
    )
    for k in range(len(episodes) - 1):
        assert episodes[k].exit_bar <= episodes[k + 1].entry_bar


def test_o1_extraction_is_deterministic() -> None:
    once = OracleEpisodeExtractor.extract_from_sequences(DEF, HIGHS, LOWS, CLOSES, VOLUMES, ATRS)
    twice = OracleEpisodeExtractor.extract_from_sequences(DEF, HIGHS, LOWS, CLOSES, VOLUMES, ATRS)
    assert [e.episode_id for e in once] == [e.episode_id for e in twice]


def test_o1_reads_a_polars_frame() -> None:
    frame = pl.DataFrame(
        {"high": HIGHS, "low": LOWS, "close": CLOSES, "volume": VOLUMES, "atr": ATRS}
    )
    episodes = OracleEpisodeExtractor.extract_episodes(DEF, frame)
    assert len(episodes) == 2
    with pytest.raises(ValueError):
        OracleEpisodeExtractor.extract_episodes(DEF, frame.drop("atr"))


def test_o0_is_labeled_evaluator_only() -> None:
    frame = pl.DataFrame(
        {"high": HIGHS, "low": LOWS, "close": CLOSES, "volume": VOLUMES, "atr": ATRS}
    )
    rows = o0_forward_excursion(frame, horizon_bars=4)
    assert len(rows) == 8
    assert all(row["usage"] == O0_EVALUATOR_ONLY for row in rows)
    assert all(row["tier"] == OracleTier.O0_RAW_HORIZON.value for row in rows)
    assert rows[0]["forward_max_gain_pct"] == pytest.approx(0.03)
