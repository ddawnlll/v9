# V9 experiment ledger (append-only)

Every experiment is appended here, including failures. Do not rewrite history:
a correction is a new entry that names the entry it supersedes.

Status vocabulary: `PREREGISTERED`, `RUNNING`, `DEV_CANDIDATE`, `NO_EDGE`,
`INCONCLUSIVE`, `STOP`, `INVALID`, `NATIVE_VALIDATED`, `NATIVE_REJECTED`.

---

## 2026-11-05 — E-001..E-003: raw directional signal screen

* Preregistration: `reports/research/PREREGISTRATION_raw_signals.json`
  (code `0157fc4`, dataset role `DEVELOPMENT`).
* Dataset: 10 USD-M perpetuals, H1, 2025-01-01 → 2025-05-01. Holdout
  2025-05-01 → 2025-07-01 sealed.
* Cost model: taker 5 bps, maker 2 bps, slippage 2 bps per market side,
  actual venue funding inside the holding window.
* Exit: static 2% stop / 4% target, stop-first intrabar, 48-bar time exit.
* Controls: eligible-time random entry, both directions, identical policy,
  200 count-matched draws per symbol, seed 20260101.
* Status: `PREREGISTERED` (results pending).

| ID | Candidate | Primary hypothesis |
|---|---|---|
| E-001 | `bollinger_reversion` | 2-sigma H1 band breach mean-reverts net of costs |
| E-002 | `volume_climax_reversal` | 4x-median volume at a range extreme marks exhaustion |
| E-003 | `liquidity_sweep_reclaim` | prior 24-bar extreme sweep + reclaim continues |

### E-001..E-003 — results (2026-11-05)

Run: `python tools/signal_screen.py --candidate all`, code `6929314`, prereg
amendment 1 (timestamp unit fix, applied before any result was used).
Every row is `MODEL_DERIVED`; no native replay was performed.

| ID | Candidate | Trades | Net exp. R | Matched random entry | Positive symbols | Control p | Holm p | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| E-001 | `bollinger_reversion` | 3,495 | **−0.1226** | −0.0444 | 0/10 | 0.727 | 1.00 | `NO_EDGE` |
| E-002 | `volume_climax_reversal` | 797 | **−0.2225** | −0.0439 | 0/10 | 0.791 | 0.79 | `NO_EDGE` |
| E-003 | `liquidity_sweep_reclaim` | 2,695 | **−0.1116** | −0.0446 | 1/10 | 0.672 | 1.00 | `NO_EDGE` |

Per-campaign cost drag is ~0.055 R (taker 5 bps + slippage 2 bps per market
side against a 2% stop). The matched random-entry control is −0.044 R per
campaign, i.e. almost exactly the cost drag, which is what a costed coin flip
must produce. **Every candidate is worse than that control**, so the failure is
not "the costs ate a small edge": the raw signal has no positive gross edge.

Declared sensitivity grid (all negative, none selected):

| Candidate | Variant | Trades | Net exp. R |
|---|---|---:|---:|
| bollinger | k=1.5 / k=2.5 | 8,167 / 1,202 | −0.105 / −0.057 |
| bollinger | window=10 / 40 | 2,687 / 3,813 | −0.062 / −0.118 |
| volume | multiple=3 / 6 | 1,508 / 300 | −0.159 / −0.317 |
| volume | window=20 / 100 | 614 / 818 | −0.208 / −0.192 |
| sweep | lookback=12 / 48 | 4,206 / 1,742 | −0.103 / −0.096 |

#### Diagnosis (why it failed)

1. **Loss is one-sided.** Bollinger shorts are −0.006 R net (gross +0.040) while
   bollinger longs are −0.221 R net (gross −0.175). The same asymmetry holds for
   the sweep candidate (shorts −0.051, longs −0.175). Fading a down-move in this
   window paid; buying the dip did not.
2. **Exit geometry is not the cause.** Outcome shares are stable across
   candidates (stop 65–71%, target 27–29%, time stop 2–6%), and the stop-first
   2%/4% bracket needs a 33.4% hit rate to break even while realised hit rates
   are 33.0%, 32.9% and 32.9%. Hit rate sits exactly at the break-even point, so
   the sign is decided by costs, not by bracket luck.
3. **Raw forward returns are noise.** Signed forward return in R over 1/4/24/48
   bars, by side:

   | Candidate | LONG 24h | LONG 48h | SHORT 24h | SHORT 48h |
   |---|---:|---:|---:|---:|
   | bollinger | −0.057 | −0.254 | +0.347 | +0.303 |
   | volume climax | +0.407 | +0.141 | −0.198 | +0.317 |
   | sweep reclaim | +0.076 | −0.130 | +0.143 | +0.167 |

   The sign is inconsistent across side and horizon, and no cell exceeds the
   ~0.055 R cost drag by a margin that survives the spread across cells. There is
   no stable gross direction to harvest at these horizons.
4. **Sign-flipping is not a justified hypothesis.** Flipping the trades would
   require the bollinger LONG cell to be positive (it is −0.254 over 48 h) *and*
   the SHORT cell to be negative (it is +0.347 over 24 h). They disagree, so an
   inverse candidate would be fitting two contradictory cells, not testing a
   mechanism.
5. `volume_climax_reversal` is also underpowered in the strict sense (797 trades
   across 10 symbols, 300 at a 6x threshold), but its expectancy is so negative
   that power is not the binding constraint.

#### What this consumes from the budget

Three primary hypotheses tested of the registered family `v9-raw-signal-screen-2025H1`
(3 of 13 declared trials); the ten sensitivity variants were declared before the
run and are not selectable. The holdout remains sealed and untouched.

#### Next hypothesis

A different mechanism, not a sign flip: `funding_extreme_reversion` — the venue
funding rate is the price of leveraged crowding, so an extreme settlement rate
should predict a reversal against the crowded side. Funding data is present in
every verified tape and was unused by all three failed candidates.


---

## 2026-11-05 — E-004: funding-crowding reversal

* Preregistration: `reports/research/PREREGISTRATION_funding.json` (code `c6ce480`).
* Same dev window, costs, exits and control design as E-001..E-003.
* Hypothesis: an extreme settlement funding rate marks leveraged crowding and
  the crowded side reverses; strict quartile comparisons mean the modal constant
  rate never triggers.
* Status: `PREREGISTERED` (results pending).

### E-004 — results (2026-11-05)

| Trades | Net exp. R | Matched random entry | Positive symbols | Control p | Holm p | Status |
|---:|---:|---:|---:|---:|---:|---|
| 563 | **−0.1381** | −0.0433 | 3/10 | 0.645 | 0.645 | `NO_EDGE` |

Sensitivity (all negative): quantile 0.05 / 0.20 → −0.116 / −0.190;
window 15 / 60 → −0.163 / −0.163.

#### Diagnosis

* The long side (fading crowded shorts) loses on every horizon: −0.18 / −0.13 /
  −0.25 / −0.25 R at 1 / 8 / 24 / 48 bars. The short side (fading crowded longs)
  is +0.26 / +0.33 R at 24 / 48 bars. The side asymmetry is the same shape as
  E-001..E-003.
* Out of 563 campaigns, 425 were longs and 138 shorts. The signal is therefore
  direction-imbalanced in a window where the long side carried a negative drift
  for alt perpetuals.
* This matters for the null design: the control draws random directions 50/50,
  so a direction-imbalanced treatment is compared against a direction-balanced
  control. That conflates *timing* with *directional beta*. The repo already has
  a direction-matched sampler (`research.screen.matched_random_sample`), which
  the next registration uses.

#### Cross-experiment finding

Four independent mechanisms (band reversion, volume climax, sweep reclaim,
funding crowding) all show the same shape: short-side cells positive, long-side
cells negative. The common factor is the market's drift in 2025-01..04, not the
signal. Two consequences for the program:

1. A direction-balanced (cross-sectional, dollar-neutral) construction is
   required to separate signal from beta.
2. The matched random-entry control must be direction-matched whenever the
   treatment is direction-imbalanced.

#### Next hypothesis

`cross_sectional_relative_strength`: rank the ten perpetuals by trailing 24-hour
return at each aligned hourly bar, go long the weakest two and short the
strongest two, and compare with a random two-and-two selection at the same bars.
The construction is dollar- and direction-balanced by design, so the window's
beta cannot explain a positive result.


---

## 2026-11-05 — E-005: cross-sectional relative strength

* Preregistration: `reports/research/PREREGISTRATION_cross_sectional.json` (code `cf8ca77`).
* Long the two weakest trailing-24h symbols, short the two strongest, on rank
  state transitions; matched null is a random two-and-two at the same bars, so
  long/short counts, timing, bracket and costs are identical.
* Status: `PREREGISTERED` (results pending).

### E-005 — results (2026-11-05)

| Metric | Value |
|---|---:|
| Events (rank transitions) | 1,653 |
| Legs priced | 2,114 |
| Net expectancy per leg | **−0.1250 R** |
| Gross expectancy per leg | −0.0795 R |
| Matched random two-and-two (mean over 200 draws) | −0.0490 R |
| Control p (one-sided) | 1.000 |
| Positive symbols | 0 / 10 |
| Net profit factor | 0.821 |
| Leg mean 95% CI | [−0.182, −0.069] |
| Symbol mean 95% CI | [−0.165, −0.086] |

Sensitivity (all negative): L4_k2 −0.067 (4,962 legs), L72_k2 −0.104 (1,280),
L24_k1 −0.027 (1,244), L24_k3 −0.137 (2,617). Status: `NO_EDGE`.

#### Diagnosis

The direction-balanced construction did what it was built to do: the matched
random two-and-two selection is −0.049 R, essentially the cost drag, so there is
no beta tailwind left in the null. The treatment is **worse than every one of the
200 control draws** (p = 1.0) and negative in all ten symbols. Because the null
is now beta-free, this is a genuine timing result, not a drift artefact:

* buying the two weakest and selling the two strongest trailing-24h symbols had
  a *negative gross* expectancy of −0.0795 R per leg before fees and funding;
* i.e. relative strength **persisted** over the 48-bar horizon rather than
  reverting, and the reversal rule paid the spread on both legs;
* the sensitivity grid is monotone in the strength of the bet: k=1 (weakest
  signal) is −0.027, k=3 is −0.137, and a longer lookback (72) is −0.104.

#### Consequence for the program

The sign of the cross-sectional effect is opposite to H-XS-001. The mirror
hypothesis — long the strong, short the weak — is not a free find: it is the same
information tested twice on the same window. It is nevertheless the only
economically motivated hypothesis this data has produced with a measured *gross*
effect larger than the cost drag (+0.0795 vs ~0.055 R), so it is worth one
preregistered test with its parameters frozen before the holdout is touched.


---

## 2026-11-05 — E-006: cross-sectional relative-strength persistence (momentum)

* Preregistration: `reports/research/PREREGISTRATION_momentum.json` (code `ea3ad0d`).
* Long the two strongest / short the two weakest trailing-24h symbols, on rank
  transitions, matched random two-and-two null.
* Disclosed double test: this is the mirror of E-005 on the same window. It is a
  new hypothesis motivated by E-005's gross measurement, not independent
  evidence; the sealed holdout is its first confirmatory test.
* Status: `PREREGISTERED` (results pending).

### E-006 — results (2026-11-05)

| Metric | Value |
|---|---:|
| Events | 1,653 |
| Legs priced | 2,114 |
| Net expectancy per leg | **−0.0353 R** |
| Gross expectancy per leg | +0.0107 R |
| Matched random two-and-two (200 draws) | −0.0490 R |
| Control p (one-sided) | 0.0796 |
| Positive symbols | 5 / 10 |
| Net profit factor | 0.947 |
| Leg mean 95% CI | [−0.097, +0.020] |
| Symbol mean 95% CI | [−0.097, +0.010] |

Sensitivity: L24_k3 **+0.0513** (6/10 positive, 2,617 legs), L72_k2 +0.0172 (4/10),
L4_k2 −0.0312 (4/10), L24_k1 −0.0870 (2/10). Status: `NO_EDGE`.

#### Diagnosis

1. The mirror is **not** the arithmetic negation of E-005. E-005's reversal lost
   0.0795 R gross; E-006's momentum earns only +0.0107 R gross. Reversing the
   direction moves the 2% stop and 4% target to the other side of the entry, and
   with stop-first intrabar resolution the two paths are not symmetric. Momentum
   also enters on a different set of state transitions. The "free mirror" does
   not exist.
2. Momentum is better than the matched null (−0.035 vs −0.049) but the
   improvement (0.014 R) is less than the residual cost drag and the p-value
   (0.080) does not clear the registered alpha. Five of ten symbols are positive;
   ADAUSDT alone is −0.242 R.
3. The positive cell (k=3) is a **declared sensitivity variant, not selectable**.
   Choosing it now would be the post-hoc selection the protocol exists to
   prevent, and its neighbours (k=1, L=72, L=4) do not corroborate it.

#### Next hypothesis

`cross_sectional_flow_imbalance`: rank the ten perpetuals at each aligned bar by
**aggressive taker-buy share** of quote volume, which is an information source
none of E-001..E-006 used (price rank and volume spikes only). Long the symbols
with the strongest buy-side aggression and short the weakest, with the same
dollar-neutral construction, costs and random two-and-two null.


---

## 2026-11-05 — E-007: cross-sectional aggressive-flow imbalance

* Preregistration: `reports/research/PREREGISTRATION_flow.json` (code `b3a312c`).
* Rank by taker-buy share of quote volume; long the two highest, short the two
  lowest, on rank transitions. Matched random two-and-two null, 50 draws.
* Status: `PREREGISTERED` (results pending).
