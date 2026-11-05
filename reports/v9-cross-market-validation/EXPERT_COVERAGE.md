# Expert coverage and missing-input diagnosis

28 canonical experts, all with an explicit horizon contract
(`v9.expert-horizon-profiles.1`). This report is observational: nothing here
changes a decision, and no expert is called profitable because it supported a
winning trade.

---

## 1. The panel as configured

| Setting | Value |
|---|---|
| Expert count | 28 (including the abstention behaviour) |
| `min_support_quorum` | 1 |
| `max_contradiction_tolerance` | 28 |
| Can a contradiction block? | **No** |

The contradiction gate is arithmetically vacuous: with 28 experts and a tolerance
of 28, at most 28 contradictions can exist, so the gate can never fire. Entry
therefore reduces to *"at least one support vote, an armed opportunity, and a
usable belief"*. This is a property of the frozen configuration, not of this
experiment, and it is the single largest structural risk in the decision stack:
one supporting witness out of 28 is enough to authorise a trade.

---

## 2. Which horizon each expert is dispatched on

| Primary | Experts | Count |
|---|---|---:|
| H1 | bollinger_breakout, breakout_retest, divergence_12_setups, fib_retracement_continuation, fib_rsi_bb_confluence, floor_trader_pivot, funding_crowding_reversal, macd_stoch_trend, obv_adl_regime, open_interest_divergence, range_breakout_1to1, trend_pullback, trend_pullback_depth, volume_confirmed_breakout | 14 |
| M15 | bollinger_reversion, candlestick_reversal, failed_breakout, failed_breakout_2b, gap_exhaustion, liquidity_sweep_reclaim, rsi_stoch_reversion, volume_climax_reversal | 8 |
| H4 | donchian_breakout, fib_projection_reversal, ichimoku_cloud, market_profile_value_area, pandf_breakout, pattern_measuring_objective | 6 |

Warmup ranges from 2 bars (candlestick_reversal) to 100 bars
(open_interest_divergence, range_breakout_1to1, volume_climax_reversal).
Coverage of the four horizons in every arm, per symbol: H1 4,344 bars, M15 17,376
bars, H4 1,086 completed bars — i.e. the full window once warmed up.

---

## 3. Stance counts on the adaptive arm (D)

Every expert is evaluated **once per H1 decision bar** — 28 × 4,344 = 121,632
evaluations per symbol, 1,216,320 across the ten markets. The decision cadence is
H1; an expert whose contract names M15 or H4 reads the most recent *completed* bar
of that horizon at the decision cut, so M15 context is never an M15 execution
cadence. Evaluations by declared primary horizon (per symbol): H1 60,816,
M15 34,752, H4 26,064.

| Symbol | Evaluations | SUPPORT | CONTRADICT | ABSTAIN | Missing input | Context incomplete | Input quality | Support rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ETHUSDT | 121,632 | 1,060 | 416 | 120,156 | 8,579 | 18 | 16 | 0.871 % |
| SOLUSDT | 121,632 | 1,312 | 500 | 119,820 | 8,579 | 18 | 16 | 1.079 % |
| BNBUSDT | 121,632 | 1,345 | 485 | 119,802 | 8,579 | 18 | 16 | 1.106 % |
| XRPUSDT | 121,632 | 1,048 | 397 | 120,187 | 8,579 | 18 | 16 | 0.862 % |
| DOGEUSDT | 121,632 | 1,280 | 505 | 119,847 | 8,579 | 18 | 16 | 1.052 % |
| ADAUSDT | 121,632 | 1,160 | 426 | 120,046 | 8,579 | 18 | 16 | 0.954 % |
| AVAXUSDT | 121,632 | 1,501 | 559 | 119,572 | 8,579 | 18 | 16 | 1.234 % |
| LINKUSDT | 121,632 | 1,369 | 512 | 119,751 | 8,579 | 18 | 16 | 1.126 % |
| DOTUSDT | 121,632 | 1,386 | 513 | 119,733 | 8,579 | 18 | 16 | 1.140 % |
| LTCUSDT | 121,632 | 1,238 | 455 | 119,939 | 8,579 | 18 | 16 | 1.018 % |
| **Total** | **1,216,320** | **12,699** | **4,768** | **1,198,853** | **85,790** | **180** | **160** | **1.044 %** |

* A support vote occurs on **1.04 %** of expert-bar evaluations; contradiction on
  0.39 %. The panel is overwhelmingly silent, which is expected for 28 selective
  patterns, but it means each entry rests on very few witnesses.
* Arms C and D produce **identical** stance counts (12,699 / 4,768 / 1,198,853).
  Adaptive management changes exits only — it does not change what any expert
  observed on any bar. Any C-vs-D difference is therefore execution, not evidence.
* The legacy-H1 arm B produces more support (13,634, 1.121 %) than C/D (12,699).
  In B every witness is evaluated against the H1 frame while stances are still
  reported under each expert's declared primary, so experts whose contract expects
  M15 or H4 structure are fed H1 structure instead. That support inflation is the
  multi-horizon effect seen from the evidence side, and it is why B is not a valid
  stand-in for the multi-horizon arms.

---

## 4. Two experts are structurally mute — measured, not assumed

`MISSING_INPUT` is 8,579 per symbol. Divide by 4,344 H1 bars: **1.97 experts**.
The registry explains exactly which two:

| Expert | Primary | Declared requirement not supplied |
|---|---|---|
| `funding_crowding_reversal` | H1 | `positioning` (and `funding_rate` as an optional input) |
| `open_interest_divergence` | H1 | `open_interest`, `positioning`, `positioning_readings` |

The monthly kline archives carry no open interest and no long/short positioning
readings, and `run_sim_arm` never populates the `readings` argument, so the
positioning channel is empty in every arm. The funding channel *is* present in the
tape (543 settlements per symbol) but is used only for the engine's funding
payments, not converted into `PositioningReading` evidence.

**Consequence.** 26 of 28 experts can vote; `funding_crowding_reversal` and
`open_interest_divergence` cast zero votes in this experiment, by construction.
Neither supported nor contradicted anything, so neither can be credited or blamed
for any outcome. Wiring funding into the evidence layer is a candidate
improvement, but changing it now would be a strategy change and is out of scope.

---

## 5. Context completeness

`CONTEXT_INCOMPLETE` totals 180 across ten symbols = 18 per symbol, all on H4, all
in the first bars of the window: the first H4 evaluation needs four completed H1
bars, and the 18 flagged evaluations are the pre-warmup ones. There is no evidence
of a stale or misaligned higher-timeframe read anywhere else — the final alignment
on every symbol reports `complete=true, quality=OK` on M15, H1 and H4.

`INPUT_QUALITY` (16 per symbol) marks evaluations where a feature existed but was
degraded; the count is small and constant across symbols.

---

## 6. What this does **not** establish

* Which experts, if any, are individually profitable. Support on a winning trade is
  not evidence of skill: with a 1 % support rate the panel is a filter, not a
  forecaster, and 581 campaigns across ten markets cannot rank 28 experts.
* That `funding_crowding_reversal` or `open_interest_divergence` would help if
  wired up. They were never measured.
* That the quorum of 1 is safe. It is the configuration that produced these
  results; whether a higher quorum would improve them is a separate, preregistered
  experiment that has not been run.
