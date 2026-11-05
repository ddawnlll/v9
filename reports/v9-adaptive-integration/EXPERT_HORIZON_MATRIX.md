# Expert horizon matrix — `v9.expert-horizon-profiles.1`

Every canonical expert has one explicit profile in
`src/v8_next/experts/horizon_profiles.py`. `primary` is the only frame the witness is dispatched on.
`confirmations` are advisory context horizons. `warmup` and `required features` are validated
against `experts/registry.py` at import time, so the table and the executable observer cannot drift.

## Declared profiles (28/28)

| # | expert | primary | confirmations | warmup (primary bars) | max staleness | required features | optional data |
|---:|---|---|---|---:|---|---|---|
| 1 | `bollinger_breakout` | H1 | H4 | 20 | 2 H1 | volatility, history | — |
| 2 | `bollinger_reversion` | M15 | H1 | 20 | 2 M15 | trend, volatility, history | — |
| 3 | `breakout_retest` | H1 | H4 | 21 | 2 H1 | location, volatility, history | — |
| 4 | `candlestick_reversal` | M15 | H1 | 2 | 2 M15 | candle_shape, volatility, history | — |
| 5 | `divergence_12_setups` | H1 | H4 | 21 | 2 H1 | oscillator, location, volatility, history | — |
| 6 | `donchian_breakout` | H4 | H1 | 21 | 2 H4 | location, volatility, history, participation | — |
| 7 | `failed_breakout` | M15 | H1 | 2 | 2 M15 | location, volatility, history | — |
| 8 | `failed_breakout_2b` | M15 | H1 | 21 | 2 M15 | location, volatility, history, candle_shape | — |
| 9 | `fib_projection_reversal` | H4 | H1 | 21 | 2 H4 | location, volatility, history | — |
| 10 | `fib_retracement_continuation` | H1 | H4 | 21 | 2 H1 | location, volatility, history | — |
| 11 | `fib_rsi_bb_confluence` | H1 | H4 | 21 | 2 H1 | oscillator, location, volatility, history | — |
| 12 | `floor_trader_pivot` | H1 | — | 25 | 2 H1 | location, volatility, history, session | — |
| 13 | `funding_crowding_reversal` | H1 | H4 | 11 | 3 H1 | positioning, volatility, history | funding_rate |
| 14 | `gap_exhaustion` | M15 | H1 | 21 | 2 M15 | candle_shape, location, volatility, history | — |
| 15 | `ichimoku_cloud` | H4 | H1 | 27 | 2 H4 | volatility, history | — |
| 16 | `liquidity_sweep_reclaim` | M15 | H1 | 2 | 2 M15 | location, volatility, history | — |
| 17 | `macd_stoch_trend` | H1 | H4 | 34 | 2 H1 | oscillator, volatility, history | — |
| 18 | `market_profile_value_area` | H4 | H1 | 25 | 2 H4 | session, volatility, history | — |
| 19 | `obv_adl_regime` | H1 | H4 | 20 | 3 H1 | participation, trend, volatility, history | bar_volume |
| 20 | `open_interest_divergence` | H1 | H4 | 100 | 3 H1 | positioning, participation, volatility, history | open_interest, positioning_readings |
| 21 | `pandf_breakout` | H4 | H1 | 20 | 2 H4 | volatility, history | — |
| 22 | `pattern_measuring_objective` | H4 | H1 | 21 | 2 H4 | location, volatility, history | — |
| 23 | `range_breakout_1to1` | H1 | H4 | 100 | 2 H1 | location, volatility, history, participation | — |
| 24 | `rsi_stoch_reversion` | M15 | H1 | 21 | 2 M15 | oscillator, volatility, history | — |
| 25 | `trend_pullback` | H1 | H4 | 20 | 2 H1 | trend, volatility, history | — |
| 26 | `trend_pullback_depth` | H1 | H4 | 21 | 2 H1 | trend, location, volatility, history | — |
| 27 | `volume_climax_reversal` | M15 | H1, H4 | 100 | 2 M15 | trend, volatility, participation, history | bar_volume |
| 28 | `volume_confirmed_breakout` | H1 | H4 | 21 | 2 H1 | location, volatility, participation, history | bar_volume |

Distribution: **M15 = 8 · H1 = 14 · H4 = 6**. The assignment is grouped by mechanism family
(short-structure liquidity, intermediate swing context, higher-timeframe regime), recorded before
the run, not tuned per bar. A change requires a new `PROFILE_TABLE_VERSION`.

## Runtime evidence (full 2,160-bar replay, `btcusdt-1h-2025Q1` + `btcusdt-15m-2025Q1`)

Counts are stance rows across 2,160 decision cuts.

| primary | SUPPORT | CONTRADICT | ABSTAIN | bar count at cut |
|---|---:|---:|---:|---:|
| M15 | 150 | 177 | 16,953 | 8,640 |
| H1 | 442 | 56 | 29,742 | 2,160 |
| H4 | 43 | 8 | 12,909 | 540 |

Last-cut alignments: M15/H1/H4 all `complete=true`, `quality=OK`, `as_of_ns` equal to the H1 bar
close. Without the M15 tape, M15 alignment is `MISSING` and all eight M15 experts abstain with
`CONTEXT_INCOMPLETE` (see `tests/test_sim_multi_horizon.py`).

## Why these assignments

| Group | Rationale |
|---|---|
| M15 primary | intrabar traps, sweeps, candle absorption and exhaustion; they need the finer structure but consult H1 (and sometimes H4) context |
| H1 primary | swing breakout/retest, oscillator confluence, participation regimes; the execution tape cadence |
| H4 primary | channel/regime claims (Donchian, Ichimoku), measured-move objectives, value-area structure |

These are **hypotheses**, not measured optima. The repository's scientific gate requires a paired
ablation before any of them can be called better than the H1 baseline.
