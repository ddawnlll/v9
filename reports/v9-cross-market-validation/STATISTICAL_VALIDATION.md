# Statistical validation

This is **cross-market generalisation research**, not a sealed out-of-sample test.
The 2025-H1 window post-dates the BTC 2025-Q1 development slice and was not used to
fit any parameter, but the strategy family was designed against earlier BTC data, so
untouched-data claims cannot be made. The protected OOS window remains reserved for
a separately authorised evaluation.

**Eligible symbols: 10 of 10 in every arm.** No symbol was dropped, no result was
`NOT_RUN` or `INVALID`, and no comparison was silently narrowed.

---

## 1. What each arm achieved per symbol

| Arm | Symbols positive (net R) | Symbols negative | Sign-test p (two-sided) | Mean net R / symbol | 95 % CI (symbol bootstrap) | Median |
|---|---:|---:|---:|---:|---|---:|
| A | 5 | 5 | 1.00 | +0.68 | [−7.00, +9.29] | −1.19 |
| B | 6 | 4 | 0.75 | −1.97 | [−5.76, +1.55] | +1.22 |
| C | 5 | 5 | 1.00 | +2.50 | [−1.53, +6.52] | +1.16 |
| D | 6 | 4 | 0.75 | −0.78 | [−6.15, +3.60] | +2.02 |

Every interval contains zero. **No arm demonstrates a cross-market edge.**

The intervals are percentile bootstraps of the mean, resampling the ten symbols —
i.e. treating the symbol as the clustering unit, which is the strongest
independence assumption the data can support. Even under that assumption nothing
separates from zero.

## 2. Paired differences

| Pair | Δ Net PnL (USDT) | Symbols positive | Sign-test p | 95 % CI of mean Δ net R |
|---|---:|---:|---:|---|
| B − A | −520.74 | 4 / 10 | 0.75 | [−9.63, +3.70] |
| C − B | **+890.55** | **8 / 10** | 0.11 | **[+1.45, +7.13]** |
| D − C | −654.25 | 4 / 10 | 0.75 | [−9.58, +2.65] |
| D − A | −284.44 | 6 / 10 | 0.75 | [−11.99, +7.99] |

`C − B` is the only pair whose interval excludes zero. With four pairs tested, one
interval excluding zero at 95 % is close to what multiple testing alone would
produce (expected 0.2 such intervals under the null across four comparisons); it is
suggestive, not established.

## 3. Why ten coins are not ten replications

All ten instruments trade the same six calendar months and share a common market
factor. The direct evidence is in the monthly table of `PAIRED_ARM_COMPARISON.md`:
**April 2025 is negative for all four arms simultaneously**, and May 2025 is
positive for three of four. That is a common regime, not ten independent draws.

An attempt to quantify this by correlating symbol-level monthly net R across the 45
symbol pairs gives mean correlations of 0.14–0.20 (A/B/C), but with only 6–7 monthly
observations per symbol the sampling error on each correlation is about ±0.4, so the
estimate is too imprecise to convert into an "effective number of independent
trials". It is reported as a failure of measurement, not as a correction:

* sign tests are reported **descriptively**, and are not used as evidence of a
  cross-sectional effect;
* the symbol-bootstrap intervals in §1–2 are optimistic for the same reason — they
  assume ten exchangeable units;
* the honest summary is that the experiment has **one** six-month market sample
  observed through ten correlated instruments.

## 4. Random-entry benchmark (matched exposure, direction and horizon)

A null was simulated per symbol and arm on the verified H1 bars: the same number of
campaigns, the arm's own long/short mix, the arm's own median holding time, the same
2 %/4 % bracket, stop evaluated before target inside a bar, horizon exit at the last
visible close. 2,000 draws, seed 20251105. This is a bar-level replay, not native
execution; it asks only whether directional **selection** beat arbitrary entries
with the same exits.

| Symbol | A: treatment net R | A: percentile | D: treatment net R | D: percentile | Null mean (D) |
|---|---:|---:|---:|---:|---:|
| ETHUSDT | +2.23 | 58.8 | +1.51 | 62.0 | −0.05 |
| SOLUSDT | −4.61 | 33.3 | +9.81 | 93.6 | +0.13 |
| BNBUSDT | −6.00 | 18.3 | +4.93 | 84.2 | +0.10 |
| XRPUSDT | −9.79 | 16.0 | +3.93 | 77.7 | −0.37 |
| DOGEUSDT | +9.01 | 81.0 | −2.82 | 31.8 | −0.09 |
| ADAUSDT | −8.18 | 23.5 | −5.45 | 21.6 | −0.45 |
| AVAXUSDT | +29.94 | **99.4** | +3.08 | 70.4 | −0.50 |
| LINKUSDT | +2.25 | 59.0 | +2.54 | 65.7 | −0.19 |
| DOTUSDT | +11.48 | 83.4 | −20.42 | **0.05** | −0.65 |
| LTCUSDT | −19.53 | 2.5 | −4.90 | 24.4 | −0.45 |

* One of twenty arm-symbol cells (A on AVAXUSDT, 99.4th percentile) clears the 95th
  percentile; under the null, one expected. **No arm beats matched random entries
  across markets.**
* DOTUSDT arm D sits at the **0.05th percentile** — worse than 1,999 of 2,000 random
  draws with the same direction mix and horizon. That single symbol contributes
  −464 of the −654 D − C difference.
* The null's own means are near zero to slightly negative, as expected once a 2/4
  bracket is applied to arbitrary entries with no edge.

## 5. Multiple-testing inventory

Hypotheses actually evaluated, and nothing else:

| Family | Count | Result |
|---|---:|---|
| Arm-level (A/B/C/D) net R per symbol | 40 cells | §1 |
| Paired differences (B−A, C−B, D−C, D−A) | 4 × 10 | §2 |
| Management ablations (C_STATIC, STOP_ONLY, TARGET_ONLY, BE_ONLY, FULL) | 5 × 10 | `ADAPTIVE_MANAGEMENT_ATTRIBUTION.md` |
| Cost stress (+2, +5, +10 bps/side) + break-even | 4 × 10 | `COST_STRESS.md` |
| Random-entry nulls | 2 arms × 10 symbols | §4 |
| Grammar census | 5 grammars × 10 symbols | `OPPORTUNITY_CENSUS.md` |

**No parameter was tuned, no symbol was dropped, no run was repeated to obtain a
better draw, and no result in this report was selected after inspection.** The four
arm configurations and the five ablation configurations were fixed before execution;
the grammar census was observational and never fed an execution arm.

Anything reported as "positive" in the presence of this many comparisons should be
read against a family-wise expectation: at α = 0.05 across ~100 cells, roughly five
nominally significant results are expected by chance alone. The experiment produced
one borderline result (AVAXUSDT arm A at the 99.4th percentile of the null) and one
strikingly negative one (DOTUSDT arm D at the 0.05th), which is exactly the pattern
expected when there is no edge but a real risk of large losses.

## 6. What would be needed for a legitimate test

1. A **sealed window** never inspected during design — the reserved OOS period — with
   the verdict criterion registered before the run.
2. **Independent market samples**, not ten correlated coins over six months: either
   genuinely different asset classes or disjoint time blocks treated as the
   clustering unit.
3. A **preregistered effect size and stopping rule**, so that a single interval
   excluding zero cannot be reinterpreted after the fact.
4. Cost realism beyond a fee increment: observed maker-fill probability and realised
   slippage from quote/trade data, which the current tapes cannot supply.

Until then the defensible statement is: on this sample, with these costs, the
measured differences are indistinguishable from zero and the adaptive arm is
measurably worse than its own static control.
