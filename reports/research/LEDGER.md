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
