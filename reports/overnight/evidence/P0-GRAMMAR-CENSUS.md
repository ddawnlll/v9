# P0 — grammar census on one PIT stream

**Module:** `src/v8_next/evaluation/grammar_census.py` · **Version:** `p0-grammar-census-v1`
**Tape:** `research/tape/btcusdt-1h-2025Q1` (`sha256:84978f2d…4673d7`), BTCUSDT-PERP.BINANCE, 2 160 bars
**Authority:** `OFFLINE_PIT_CENSUS` · **Claim:** `NO_ECONOMIC_CLAIM` · **Costs applied:** none
**Evidence:** `reports/overnight/evidence/p0-grammar-census.json` (`evidence_digest sha256:6aa93f79…4a607f`)

## What it does

Every bar becomes one `CausalFrame`; all five registered grammars are evaluated on that same
frame. Same tape, same clock, same warmup rule, same bracket geometry (stop 2 %, target 4 %,
stop evaluated first inside a bar). No strategy, no order, no venue adapter, no sizing — the
module never imports `nautilus_trader`.

Episodes are deduplicated per grammar: a repeat trigger in the same direction inside the
grammar's own horizon is a **fragment** of one economic episode, not a second opportunity.

## Result — detection and episode units differ by an order of magnitude

| Grammar | Bars eval. | Directional bars | Episodes | Fragments | Target | Stop | Timeout | Censored | Hit rate | Mean MFE | Mean MAE |
|---|---|---|---|---|---|---|---|---|---|---|---|
| range-breakout-48-v1 | 2 112 | **123** | **10** | 113 | 4 | 6 | 0 | 0 | 40.0 % | 2.60 % | 1.68 % |
| volatility-extreme-v2 | 2 140 | 389 | 56 | 333 | 13 | 32 | 10 | 1 | 23.6 % | 1.99 % | 1.71 % |
| trend-continuation-v2 | 2 136 | 818 | 118 | 700 | 13 | 42 | 62 | 1 | 11.1 % | 1.76 % | 1.50 % |
| mean-reversion-v2 | 2 140 | 62 | 33 | 29 | 2 | 11 | 20 | 0 | 6.1 % | 1.57 % | 1.54 % |
| compression-expansion-v2 | 2 099 | 32 | 22 | 10 | 5 | 12 | 5 | 0 | 22.7 % | 2.20 % | 1.73 % |

`directional_bars` for `range-breakout-48-v1` is **123**, which is exactly the detection count
the architecture monograph reports for the same tape (§3.1: "123 fırsat kaydedildi"). The census
therefore reproduces the documented candidate count from an independent path.

The new information is the next column: those 123 detection bars collapse to **10 distinct
economic episodes**, because the grammar's own horizon is 336 bars (14 days) and a repeat
breakout in the same direction is the same hypothesis. 8 of the 10 episodes opened while an
opposite-direction episode was still live.

## Cross-grammar overlap (same bar)

| Pair | Bars |
|---|---|
| trend-continuation-v2 · volatility-extreme-v2 | 309 |
| range-breakout-48-v1 · trend-continuation-v2 | 122 |
| range-breakout-48-v1 · volatility-extreme-v2 | 111 |
| mean-reversion-v2 · volatility-extreme-v2 | 26 |
| compression-expansion-v2 · volatility-extreme-v2 | 17 |
| compression-expansion-v2 · trend-continuation-v2 | 15 |
| compression-expansion-v2 · range-breakout-48-v1 | 8 |

354 bars carry more than one grammar; **26 bars carry opposing directions**. With one grammar
active at a time those 26 bars are silently resolved by the default choice, not by policy.

## Panel gate audit

```
expert_count=28  min_support_quorum=1  max_contradiction_tolerance=28
contradiction_can_block = false
```

The contradiction condition `contradict <= 28` is vacuous for a 28-expert panel, so the gate
reduces to "at least one support vote". This is an audit of the configured values
(`SimArmConfig` defaults), not a new gate; the sim-arm values were chosen to match the V8
parity baseline and must not be reused for a V9 panel claim.

## Limits

- One tape, one window, one venue, one instrument; no OOS.
- Costs, funding and slippage are **not** applied. Hit rates are bracket geometry, not edge.
- Timeouts are scored at the horizon close; censored episodes are excluded from denominators.
- Outcomes are resolved after the window closes and are never fed back into a decision.
- No grammar is selected, weighted or promoted by this slice.

## Next gates (not done here)

1. Binding gate: pick a contradiction tolerance `< expert_count` and record the panel's
   `decision_changed` count against the census episodes.
2. Multi-hypothesis book: witness identity is expert-id-keyed and first-binding-wins
   (`opportunities/book.py:279-307`), so one expert cannot witness two live episodes. Any
   multi-grammar book needs `(expert_id, hypothesis_id)` witness scope first.
3. Entry reuse: while flat, only the current bar's detection can become an entry
   (`adaptive_strategy.py::_opportunity_view`), so book persistence does not yet reach the
   decision path.
