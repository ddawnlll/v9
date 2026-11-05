# Experimental expert horizon contracts

Profiles are uncalibrated hypotheses from `src/v8_next/experts/horizon_profiles.py`; confirmation horizons are provenance only and are not consumed by legacy observers. No profile is connected to native simulation; runtime evidence for every row is NOT_RUN. Warmup is inherited from the registry. Synthetic tests are mechanics, not economic evidence.

| Expert | Required | Primary | Optional confirmation | Warmup bars | Runtime |
| --- | --- | --- | --- | ---: | --- |
| bollinger_breakout | ohlcv, volatility, history | H1 | H4 | 20 | NOT_RUN |
| bollinger_reversion | ohlcv, trend, volatility, history | H1 | H4 | 20 | NOT_RUN |
| breakout_retest | ohlcv, location, volatility, history | M15 | H1 | 21 | NOT_RUN |
| candlestick_reversal | ohlcv, candle_shape, volatility, history | M15 | H1 | 2 | NOT_RUN |
| divergence_12_setups | ohlcv, oscillator, location, volatility, history | H1 | H4 | 21 | NOT_RUN |
| donchian_breakout | ohlcv, location, volatility, history, participation | H1 | H4 | 21 | NOT_RUN |
| failed_breakout | ohlcv, location, volatility, history | M15 | H1 | 2 | NOT_RUN |
| failed_breakout_2b | ohlcv, location, volatility, history, candle_shape | M15 | H1 | 21 | NOT_RUN |
| fib_projection_reversal | ohlcv, location, volatility, history | H1 | H4 | 21 | NOT_RUN |
| fib_retracement_continuation | ohlcv, location, volatility, history | H1 | H4 | 21 | NOT_RUN |
| fib_rsi_bb_confluence | ohlcv, oscillator, location, volatility, history | H1 | H4 | 21 | NOT_RUN |
| floor_trader_pivot | ohlcv, location, volatility, history, session | M15 | H1 | 25 | NOT_RUN |
| funding_crowding_reversal | ohlcv, positioning, volatility, history | H1 | H4 | 11 | NOT_RUN |
| gap_exhaustion | ohlcv, candle_shape, location, volatility, history | M15 | H1 | 21 | NOT_RUN |
| ichimoku_cloud | ohlcv, volatility, history | H1 | H4 | 27 | NOT_RUN |
| liquidity_sweep_reclaim | ohlcv, location, volatility, history | M15 | H1 | 2 | NOT_RUN |
| macd_stoch_trend | ohlcv, oscillator, volatility, history | H1 | H4 | 34 | NOT_RUN |
| market_profile_value_area | ohlcv, session, volatility, history | M15 | H1 | 25 | NOT_RUN |
| obv_adl_regime | ohlcv, participation, trend, volatility, history | H1 | H4 | 20 | NOT_RUN |
| open_interest_divergence | ohlcv, positioning, participation, volatility, history | H1 | H4 | 100 | NOT_RUN |
| pandf_breakout | ohlcv, volatility, history | H1 | H4 | 20 | NOT_RUN |
| pattern_measuring_objective | ohlcv, location, volatility, history | H1 | H4 | 21 | NOT_RUN |
| range_breakout_1to1 | ohlcv, location, volatility, history, participation | M15 | H1 | 100 | NOT_RUN |
| rsi_stoch_reversion | ohlcv, oscillator, volatility, history | H1 | H4 | 21 | NOT_RUN |
| trend_pullback | ohlcv, trend, volatility, history | H1 | H4 | 20 | NOT_RUN |
| trend_pullback_depth | ohlcv, trend, location, volatility, history | H1 | H4 | 21 | NOT_RUN |
| volume_climax_reversal | ohlcv, trend, volatility, participation, history | M15 | H1 | 100 | NOT_RUN |
| volume_confirmed_breakout | ohlcv, location, volatility, participation, history | M15 | H1 | 21 | NOT_RUN |
