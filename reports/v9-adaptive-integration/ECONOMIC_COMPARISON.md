# Economic comparison — one BTCUSDT development slice

**Data:** `research/tape/btcusdt-1h-2025Q1` (2,160 bars, 2025-01-01 → 2025-04-01,
SHA-256 `84978f2d…4673d7`) + verified M15 `research/tape/btcusdt-15m-2025Q1`
(SHA-256 `8fe71367…e73117`).
**Account:** 10,000 USDT, one BTCUSDT perpetual, NETTING, maker 0.0002 / taker 0.0005.
**Engine:** NautilusTrader 2.0.0rc4 `BacktestEngine`, execution profile `baseline`.

## Arms

| Arm | Decision source | Context | Exit policy | Wall | Trades | Net PnL (USDT) | Net R | Fees | Funding | Open | Max DD | Hit |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A — V8 static H1 | V8 ensemble consensus | H1 | fixed 2%/4% bracket | 1.0 s | 44 | −21.234951 | −0.021638 | 37.915357 | 0.367186 | 1 | 1.998% | 34.09% |
| B — V9 legacy H1 | V9 policy | H1 | fixed 2%/4% bracket | 8.5 s | 24 | −23.102285 | −0.049252 | 20.918686 | 0.367186 | 1 | 1.798% | 33.33% |
| C — V9 multi-horizon | V9 policy | M15+H1+H4 | fixed 2%/4% bracket | 14.3 s | 18 | −13.880205 | −0.045609 | 15.617577 | 0.166755 | 1 | 0.779% | 33.33% |
| D — V9 multi-horizon + adaptive mgmt | V9 policy | M15+H1+H4 | thesis + stop amendment | 14.8 s | 23 | **+20.162531** | **+0.052744** | 20.585532 | 0.219075 | 0 | 0.570% | 47.83% |

Delta D − A: **+41.397482 USDT**, net R **+0.074382**, max drawdown **−1.428 pp**.
Delta D − C (adaptive management alone): **+34.042736 USDT**.

## What the arms isolate

- A → B changes the **decision source** at the same static bracket and same H1 context.
- B → C changes **context only** (multi-horizon expert profiles), bracket still static.
- C → D changes **position management only** (thesis, stop amendments, target policy), context and
  entry policy unchanged.
- D vs A is the integrated product comparison; it is **not** a single-intervention causal estimate.

## Costs

Fees are engine fill commissions. Funding is the sum of booked `FUNDING` adjustments. Slippage is
the execution profile's model assumption (`FILL_MODEL=default`, `SLIPPED=False`), not observed venue
slippage. There is no L2/queue evidence; no maker-fill claim is made.

## Honest verdict

`DESCRIPTIVE_ONLY`. On this single 90-day development window the integrated arm improved net marked
PnL, net R and drawdown, with a lower hit-rate variance, and every mechanical gate passed. Twenty-three
closed campaigns are far below any preregistered sample floor. The result:

- is **not** an edge claim;
- is **not** OOS;
- does **not** generalise to altcoins, other regimes or other fee tiers;
- must not be promoted from this evidence.

A 10-symbol, 180-day evaluation is prepared as a separate experiment. It uses independent
10,000 USDT books per symbol, not a shared portfolio, and must be labelled as such.
