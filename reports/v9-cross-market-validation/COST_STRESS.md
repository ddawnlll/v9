# Cost and slippage stress

Reference profile, unchanged from the BTC development experiment: maker 0.0002,
taker 0.0005, NautilusTrader 2.0.0rc4 `default` fill model, `SLIPPED=False`,
execution profile `baseline`. No queue model, no L2 book, no observed slippage.

The stress adds **+2 / +5 / +10 basis points of incremental cost per side**, applied
identically to entries and exits of arm D. In a bar-driven engine a per-side fee
increment and a per-side slippage increment are the same cash flow, so the increment
is charged through the commission model on top of the base fees. Nothing is
double-counted: the active fill model applies no slippage of its own, so the only
cost in the baseline is the explicit fee.

**Limit of this method.** It cannot represent maker-fill probability, queue position
or a spread that widens when volatility rises. Those require quote or order-book
data the monthly archives do not contain, so no such claim is made and no fill was
invented.

---

## 1. Reference results and sensitivity (arm D)

| Symbol | Net PnL @ reference | +2 bps/side | +5 bps/side | +10 bps/side | Cost per added bps | Break-even added cost |
|---|---:|---:|---:|---:|---:|---:|
| ETHUSDT | +30.40 | +12.01 | −15.57 | −61.54 | 9.19 | **+3.31 bps** |
| SOLUSDT | +195.61 | +173.05 | +139.19 | +82.78 | 11.28 | **+17.34 bps** |
| BNBUSDT | +97.79 | +75.61 | +42.35 | −13.08 | 11.09 | **+8.82 bps** |
| XRPUSDT | +78.67 | +57.91 | +26.78 | −25.12 | 10.38 | **+7.58 bps** |
| AVAXUSDT | +60.98 | +35.63 | −2.40 | −65.78 | 12.68 | **+4.81 bps** |
| LINKUSDT | +50.71 | +26.31 | −10.30 | −71.30 | 12.20 | **+4.16 bps** |
| DOGEUSDT | −56.39 | −78.38 | −111.37 | −166.35 | 11.00 | — already negative |
| ADAUSDT | −108.86 | −134.91 | −173.98 | −239.10 | 13.02 | — already negative |
| LTCUSDT | −98.07 | −122.11 | −158.16 | −218.24 | 12.02 | — already negative |
| DOTUSDT | −408.20 | −434.97 | −475.12 | −542.04 | 13.38 | — already negative |
| **Total** | **−157.37** | **−389.85** | **−738.58** | **−1,319.78** | **116.24** | — |

"Cost per added bps" is the measured increase in commission per basis point of added
per-side cost — i.e. twice the arm's notional turnover divided by 10,000. It is
stable across symbols (9.2–13.4 USDT per bps), which is the expected linear
behaviour: the fee increment does not change any decision, so the trade path is
identical at every stress level and total cost grows linearly.

Break-even is `reference net PnL ÷ cost per bps` and is reported only where the
reference result is positive. For the four already-losing symbols no amount of cost
*reduction* fixes them; the number would be negative and meaningless.

---

## 2. What the stress says

1. **The aggregate adaptive arm is negative before the stress starts.** At the
   reference cost it is −157.37 USDT; the stress cannot rescue a negative baseline
   and only deepens it. Adding 2 bps per side costs ~233 USDT and turns a −157 loss
   into −390.
2. **Of the six symbols that were positive, five die before +10 bps.**
   * ETHUSDT survives only **+3.3 bps** — half of one tick on a 2,500 instrument.
   * LINKUSDT +4.2, AVAXUSDT +4.8, XRPUSDT +7.6, BNBUSDT +8.8.
   * SOLUSDT alone survives +17.3 bps.
3. **The whole positive set collapses to one symbol at +10 bps.** SOLUSDT remains
   +82.78 while every other symbol is negative — the same concentration seen in the
   main results (two coins carried arm C).
4. **The measured cost load is already high.** Realised commission across the
   experiment is 4.5 bps per side on a 2 bps maker / 5 bps taker schedule
   (2,734.27 USDT on 6,028,616 USDT of turnover). A strategy earning ~10 bps per
   campaign before costs cannot absorb another 10 bps per side; +10 bps is a
   doubling of the cost base, not a tail scenario.

---

## 3. Cost survival by arm at the reference profile

| Arm | Gross price PnL | Commissions | Net | Gross kept |
|---|---:|---:|---:|---:|
| A (V8) | +1,371.56 | 1,170.07 | +127.07 | 9.3 % |
| B (V9 legacy H1) | +180.08 | 529.77 | −393.67 | — |
| C (V9 multi static) | +1,010.00 | 472.33 | +496.88 | 49.2 % |
| D (V9 multi adaptive) | +429.66 | 562.11 | −157.37 | — |

Arm A pays 1,170 USDT of commission to keep 127 — it is a high-turnover arm whose
gross edge is almost entirely transferred to the venue. Arm C earns less gross but
keeps half of it, because it trades 40 % less. D earns the least gross and pays more
than C, which is why it is negative.

This is the honest summary of the cost dimension: **no arm here has an edge large
enough to be robust to plausible execution friction.** The V8 arm survives only
because its gross signal is strong enough to pay a 9 % retention rate; a 10 bps
increase in per-side cost would eliminate it as well.

---

## 4. What a real cost model would need

* Quote/trade tick data to estimate maker-fill probability and realised spread at
  each entry — the monthly kline archives carry none of it.
* A queue-position model for limit entries; `prob_fill_on_limit` is a free parameter
  and would be a guess.
* Funding is already modelled from recorded settlements; the *attribution* of funding
  to campaigns is limited by the position-id reuse documented in
  `EXECUTION_SAFETY.md` §5.

Absent those inputs, the defensible statement is the one above: a per-side increment
of a few basis points erases the entire measured edge, and the strategy's survival
margin against friction is roughly 3–9 bps per side depending on the market.
