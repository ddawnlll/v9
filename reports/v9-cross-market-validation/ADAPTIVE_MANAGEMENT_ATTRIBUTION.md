# Adaptive management attribution

Which mechanism in arm D actually did anything? Five configurations of the *same*
strategy were measured on the same ten tapes, with only the frozen flags changed.
No strategy code was modified for this experiment.

| Key | `adaptive_management_enabled` | initial stop | initial target | stop amendments | target amendments |
|---|---|---|---|---|---|
| `C_STATIC` | off | fixed 2 % | fixed 4 % | no | no |
| `STOP_ONLY` | on | **structural** | fixed 4 % | no | no |
| `TARGET_ONLY` | on | fixed 2 % | **structural** | no | no |
| `BE_ONLY` | on | fixed 2 % | fixed 4 % | **yes** | no |
| `FULL` | on | structural | structural | yes | yes |

`FULL` is byte-identical to arm D of the main matrix (verified: every symbol's
`FULL` net PnL equals its matrix `D` net PnL, e.g. LINKUSDT +50.71 and
−408.20 for DOTUSDT).

---

## 1. Portfolio outcome

| Configuration | Trades | Net PnL (USDT) | Δ vs C | Median hold | Mean max DD | Mean win rate | Protection violations |
|---|---:|---:|---:|---:|---:|---:|---:|
| `C_STATIC` | 531 | **+496.88** | — | 8.0 h | 1.93 % | 36.40 % | 0 |
| `TARGET_ONLY` | 509 | **+638.51** | **+141.63** | 8.0 h | 1.92 % | 36.60 % | 0 |
| `STOP_ONLY` | 469 | −50.43 | −547.31 | 9.5 h | 2.56 % | 43.15 % | 0 |
| `BE_ONLY` | 622 | −517.01 | −1,013.89 | 3.0 h | 1.74 % | 52.75 % | 1 |
| `FULL` | 581 | −157.37 | **−654.25** | 4.0 h | 2.01 % | 54.05 % | 1 |

Per symbol (Net PnL, USDT):

| Symbol | `C_STATIC` | `STOP_ONLY` | `TARGET_ONLY` | `BE_ONLY` | `FULL` |
|---|---:|---:|---:|---:|---:|
| ETHUSDT | −125.39 | −223.41 | −229.32 | −109.75 | **+30.40** |
| SOLUSDT | −16.02 | +46.01 | +99.58 | +37.46 | **+195.61** |
| BNBUSDT | −10.58 | +7.14 | +42.36 | −30.18 | **+97.79** |
| XRPUSDT | +95.92 | +25.78 | +95.92 | +13.86 | +78.67 |
| DOGEUSDT | +189.87 | +173.73 | +193.83 | +68.70 | −56.39 |
| ADAUSDT | −94.03 | −88.85 | −103.79 | −246.84 | −108.86 |
| AVAXUSDT | **+276.86** | +329.24 | +286.56 | +74.03 | +60.98 |
| LINKUSDT | **+222.86** | −31.54 | +249.94 | −25.56 | +50.71 |
| DOTUSDT | **+56.16** | −243.78 | +56.16 | −224.87 | −408.20 |
| LTCUSDT | −98.76 | −44.74 | −52.74 | −73.85 | −98.07 |

---

## 2. Entry-matched diagnostic — native trade outcome, separated from path

`C` and `D` diverge as soon as an exit differs, so the aggregate difference mixes
two effects. Matching campaigns on the exact entry bar and side isolates what
happened to **the trades both arms actually took**.

| Comparison | Matched campaigns | C entry-side PnL | Variant PnL | **Matched delta** | Portfolio delta | Path residual |
|---|---:|---:|---:|---:|---:|---:|
| `FULL` vs `C_STATIC` | 472 | −4.48 | −37.56 | **−33.08** | −654.25 | **−621.17** |
| `STOP_ONLY` vs `C_STATIC` | 452 | +474.96 | −11.06 | **−486.02** | −547.31 | −61.29 |
| `BE_ONLY` vs `C_STATIC` | 513 | +456.48 | −276.01 | **−732.49** | −1,013.89 | −281.40 |
| `TARGET_ONLY` vs `C_STATIC` | 500 | +788.86 | +753.96 | **−34.90** | +141.63 | +176.53 |

This is the most important table in the report.

* **On the trades both arms took, the full adaptive stack costs −33 USDT.** Per-trade
  management is essentially neutral.
* **The −654 aggregate difference is −621 of portfolio path.** Because only one
  position may be open at a time, a structural stop that exits 4 hours earlier
  changes *which later opportunity is available*. D trades 581 campaigns against C's
  531, and the extra/different 109 campaigns are where the money is lost.
* **The structural stop alone destroys value on identical entries** (−486), and so do
  break-even amendments (−732). Those two mechanisms are individually harmful, not
  merely path-dependent.
* **The structural target is neutral per trade** (−35) and only appears positive in
  the aggregate (+142) because it changes which campaigns occur.

---

## 3. Why break-even amendments lose money: the win-rate trap

`BE_ONLY` raises the win rate from 36.4 % to 52.8 % and cuts the median hold from
8 hours to 3 — and loses 1,014 USDT doing it.

With a 2 % stop and a 4 % target, a win is +2 R and a loss −1 R. Break-even
expectancy therefore needs a 33.3 % win rate. Moving to breakeven once a trade is
in profit converts a distribution of would-be winners into a mass of ~0 R scratches:
the win rate rises by 16 points, the expectancy falls by more than 1 R per trade, and
the arm pays the same commission to collect neither the target nor a meaningful loss.

`FULL` shows the same signature (54.05 % win rate, −157.37 USDT) with the structural
stop partially offsetting the worst of it.

---

## 4. Which requested mechanism explains the full arm

The mission asks whether the full arm's benefit is explained by better stop geometry,
target geometry, amendments, avoided losses, holding duration, or freed capital.
Measured answer: **there is no benefit to explain.**

| Mechanism | Native event count (all 10 symbols) | Effect on identical entries | Verdict |
|---|---:|---|---|
| Structural initial stop | 583 entries | −486 USDT (`STOP_ONLY`) | **destroys value** |
| Structural target | 583 entries | −35 USDT (`TARGET_ONLY`) | neutral per trade |
| Stop amendment (break-even) | 20 extra stop orders in `FULL` (20 in `BE_ONLY`) | −732 USDT (`BE_ONLY`) | **destroys value** |
| Target amendment | 10 extra target orders in `FULL` | not separately isolated | not measurable alone |
| Thesis exit | **0 `EXIT` orders in every matrix arm (A/B/C/D)**; 7 in `STOP_ONLY`, 6 in `TARGET_ONLY` | — | **never fired where it was supposed to** |
| Avoided losses | — | not observed: drawdown rises for `STOP_ONLY` (2.56 % vs 1.93 %) | no evidence |
| Changed holding duration | median 8 h → 4 h | correlated with the loss, not with a gain | harmful here |
| Freed capital | 472 matched / 109 D-only campaigns | **−621 USDT of path effect** | harmful here |

Order-ledger detail per configuration (all ten symbols): `C_STATIC` 533 entries /
534 stops / 534 targets / 1 reduce / 0 exits; `STOP_ONLY` 472 / 557 / 557 / 85 / 7;
`TARGET_ONLY` 511 / 585 / 585 / 74 / 6; `BE_ONLY` 623 / 643 / 633 / 10 / 0;
`FULL` 583 / 603 / 593 / 10 / 0. The 85 and 74 "extra" stops in `STOP_ONLY` and
`TARGET_ONLY` are re-protections after a partial reduce, not break-even amendments —
the fixed-target configurations reduce far more often.

Two consequences the record enforces:

1. **No thesis exit occurred in the main arms.** `EXIT` role count is zero in every
   A/B/C/D arm; the role appears only in two ablation configurations (7 and 6 orders
   respectively). Any narrative in which thesis exits contribute to the reported
   A/B/C/D performance is contradicted by the native order ledger.
2. **The target-amendment mechanism was never isolated**, because only the `FULL`
   configuration enables it. Its 30 events cannot be separated from the rest of the
   stack with the frozen flags, and no claim is made about it.

---

## 5. Why BTCUSDT Q1 showed the opposite sign

On the development slice D − C was **+34.04 USDT**; across ten alt markets it is
**−654.25**. Three differences are measurable and sufficient:

1. **Sample.** 23 campaigns on BTC versus 581 across the ten markets.
2. **Cost load.** BTCUSDТ Q1 fees were 20.59 USDT against 581 campaigns here
   absorbing 562.11 USDT — a 27× larger cost base on which the same 9 bps round trip
   must be recovered.
3. **Path dependence.** BTC Q1 was a trending window in which an early structural
   exit and re-entry worked; the alt set contains April's drawdown, where each early
   exit re-entered into a falling market (AVAX −216, DOGE −246, DOT −464 relative to
   C).

The BTC result was not noise — it was a real outcome of one window. It is simply not
a property of the mechanism, and it does not survive eight of ten other markets.
