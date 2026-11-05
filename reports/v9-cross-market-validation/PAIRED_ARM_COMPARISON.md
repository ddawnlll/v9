# Paired arm comparison — A / B / C / D across ten markets

Ten independent 10,000 USDT accounts per arm, 2025-01-01 → 2025-07-01
(end-exclusive, 181 days), maker 2 bps / taker 5 bps, `baseline` execution profile,
NautilusTrader 2.0.0rc4 default fill model. Net PnL is the native account balance
delta; net R is per-campaign net R summed over closed campaigns, with per-campaign
risk = entry notional × 2 %.

---

## 1. Totals

| Arm | Decision source | Context | Exit policy | Trades | Net PnL (USDT) | Mean net R / symbol | Symbols + / − | Sign-test p | Max DD (mean) |
|---|---|---|---:|---:|---:|---:|---|---:|---:|
| **A** | V8 ensemble | H1 | fixed 2 %/4 % | 1,311 | **+127.07** | +0.68 | 5 / 5 | 1.00 | 3.32 % |
| **B** | V9 policy | H1 only | fixed 2 %/4 % | 593 | **−393.67** | −1.97 | 6 / 4 | 0.75 | 2.19 % |
| **C** | V9 policy | M15+H1+H4 | fixed 2 %/4 % | 533 | **+496.88** | +2.50 | 5 / 5 | 1.00 | 1.93 % |
| **D** | V9 policy | M15+H1+H4 | thesis + structural stops + amendments | 583 | **−157.37** | −0.78 | 6 / 4 | 0.75 | 2.01 % |

Bootstrap 95 % interval of the mean net R per symbol (clustering unit = symbol,
5,000 resamples): A `[−7.00, +9.29]`, B `[−5.77, +1.55]`, C `[−1.53, +6.52]`,
D `[−6.15, +3.60]`. **Every interval contains zero.**

The single most important line in this table: the arm that produced +20.16 USDT
net on BTCUSDT 2025-Q1 (**D**) is **negative** across ten alt markets, while the
static multi-horizon arm (**C**) is the best performer. The BTC development result
was not replicated; it was reversed.

---

## 2. Paired differences per symbol (USDT)

| Symbol | B − A (decision source) | C − B (multi-horizon) | D − C (adaptive mgmt) | D − A (integrated) |
|---|---:|---:|---:|---:|
| ETHUSDT | −241.13 | +71.38 | **+155.79** | −13.96 |
| SOLUSDT | +173.28 | −96.87 | **+211.63** | +288.04 |
| BNBUSDT | −78.60 | +187.74 | **+108.37** | +217.50 |
| XRPUSDT | +205.96 | +85.74 | −17.24 | +274.45 |
| DOGEUSDT | −124.30 | +134.62 | **−246.25** | −235.93 |
| ADAUSDT | +63.05 | +6.32 | −14.83 | +54.54 |
| AVAXUSDT | −519.28 | +204.86 | **−215.88** | −530.31 |
| LINKUSDT | −6.34 | +184.98 | **−172.15** | +6.49 |
| DOTUSDT | −166.56 | −6.83 | **−464.37** | −637.76 |
| LTCUSDT | +173.19 | +118.62 | +0.69 | +292.50 |
| **Sum** | **−520.74** | **+890.55** | **−654.25** | **−284.44** |
| Symbols positive | 4 / 10 | **8 / 10** | 4 / 10 | 6 / 10 |
| Sign-test p | 0.75 | 0.11 | 0.75 | 0.75 |

### B − A: the V9 decision policy is not an improvement over V8
−520.74 USDT, positive in only 4 of 10 markets, p = 0.75. The V9 policy trades far
less (593 vs 1,311 campaigns) and saves 640 USDT of commission, yet still ends
646 USDT worse gross. On BTCUSDT Q1 the same swap cost −1.87 USDT; across ten
markets it costs −520.74. The decision source is not the source of any edge.

### C − B: multi-horizon context is the only consistent improvement
+890.55 USDT, positive in 8 of 10 symbols, and the only pair whose bootstrap
interval excludes zero (`[+1.45, +7.13]` net R). This is the one signal in the
experiment that survives cross-market generalisation: giving each expert its
declared horizon (and giving entries an M15/H4 view) helps, at unchanged bracket
and unchanged entry policy. It is still not significant at α = 0.05 on a sign test
(p = 0.11) and it is concentrated: AVAXUSDT + LINKUSDT + DOGEUSDT alone contribute
+724 of the +891.

### D − C: adaptive management **subtracts** value out of sample
−654.25 USDT, positive in only 4 of 10 markets. The distribution is bimodal rather
than uniformly bad: SOL +211.63, ETH +155.79, BNB +108.37 versus DOT −464.37,
DOGE −246.25, AVAX −215.88. That shape is the signature of a mechanism that helps
in some market states and is very expensive in others, not of a small consistent
cost.

**D − C is not a pure per-trade management effect.** C and D diverge as soon as an
earlier exit frees capital at a different time, so part of the difference is a
portfolio-path effect. `ADAPTIVE_MANAGEMENT_ATTRIBUTION.md` separates the two with
entry-matched campaigns; on the three symbols measured so far the matched subset
tells the same story as the aggregate.

### D − A: the integrated product does not beat V8
−284.44 USDT, 6 of 10 positive, p = 0.75. Where D wins (LTC +292.50, SOL +288.04,
XRP +274.45, BNB +217.50) it wins by taking a third fewer trades; where it loses
(AVAX −530.31, DOT −637.76, DOGE −235.93) it is not the trade count but the exit
geometry.

---

## 3. Gross vs net: costs are the dominant term

| Arm | Independent gross price PnL | Commissions | Booked funding (floor) | Reconciliation residual | Net | Commissions / gross |
|---|---:|---:|---:|---:|---:|---:|
| A | +1,371.56 | 1,170.07 | −0.51 | −73.91 | **+127.07** | 85 % |
| B | +180.08 | 529.77 | −0.82 | −43.16 | **−393.67** | 294 % |
| C | +1,010.00 | 472.33 | −0.64 | −40.16 | **+496.88** | 47 % |
| D | +429.66 | 562.11 | −0.71 | −24.22 | **−157.37** | 131 % |

Gross price PnL is recomputed from every campaign's recorded entry/exit price and
quantity, independently of the engine's own realized figure. The residual column is
what does not reconcile (net − gross − funding + fees) and is at most
−0.0038 % of turnover — engineered by the 2-decimal price rounding in the position
records, as explained in `EXECUTION_SAFETY.md` §2. It is immaterial to every sign in
this report but it is not zero, so it is shown rather than hidden.

Booked funding is a floor, not a total — see `EXECUTION_SAFETY.md` §5. Total
commissions across the 40 arm-runs are 2,734.27 USDT on 6,028,616 USDT of turnover.

---

## 4. Realized vs marked at the cutoff

| Arm | Symbols with an open position at 2025-07-01 | Open campaigns |
|---|---:|---:|
| A | 3 | 3 |
| B | 2 | 2 |
| C | 2 | 2 |
| D | 2 | 2 |

Those positions are excluded from every closed-campaign statistic and are **not**
marked in `net_pnl`: the account balance delta is realized only. The largest single
open campaign notional is ~1,000 USDT, so the unmarked exposure is at most ~0.1 R
per affected arm — immaterial to the signs above, but it is a limitation, not a
rounding.

---

## 5. Monthly stability (pooled campaign net R)

| Month | A | B | C | D | D campaigns |
|---|---:|---:|---:|---:|---:|
| 2025-01 | **+40.07** | +2.78 | +14.25 | −6.91 | 95 |
| 2025-02 | +13.35 | −25.36 | +10.04 | −0.81 | 66 |
| 2025-03 | +14.83 | +16.85 | −16.37 | −0.61 | 92 |
| 2025-04 | −33.37 | −14.96 | −17.40 | −11.15 | 80 |
| 2025-05 | −28.15 | +15.81 | **+28.04** | **+19.88** | 137 |
| 2025-06 | +1.11 | −13.73 | +7.53 | −6.65 | 109 |
| 2025-07 | −1.05 | −1.05 | −1.05 | −1.55 | 2 |

April is the only month in which all four arms lose; May is C's and D's best month.
A's +40 R of January is not reproduced by any V9 arm in any month. The 2025-07 row
holds the two campaigns whose close landed exactly on the window boundary and is
not a month of its own.

---

## 6. Concentration: two coins carry the table

C's +496.88 USDT decomposes as AVAXUSDT +276.86, LINKUSDT +222.86, DOGEUSDT +189.87,
XRPUSDT +95.92, DOTUSDT +56.16, BNBUSDT −10.58, SOLUSDT −16.02, ADAUSDT −94.03,
LTCUSDT −98.76, ETHUSDT −125.39. The top two symbols alone (499.72) exceed the
total. Remove AVAXUSDT and C is +220.02; remove AVAXUSDT and LINKUSDT and C is
−2.84, i.e. flat.

Ten symbols trading the same six months are also not ten independent replications:
they share the same January rally, the same April drawdown and the same May
recovery. The sign tests in §2 are reported for completeness; they are not
confirmatory evidence of a cross-sectional edge.

---

## 7. Verdict on the paired arms

* **V9 does not beat V8 across markets after costs** (D − A = −284.44; B − A = −520.74).
* **Multi-horizon context does help** (C − B = +890.55, 8/10), and it is the only
  robust improvement measured — but it is concentrated and not statistically
  significant.
* **Adaptive management reduces net value out of sample** (D − C = −654.25, 4/10),
  contradicting the BTC development measurement (+34.04) that motivated this study.
