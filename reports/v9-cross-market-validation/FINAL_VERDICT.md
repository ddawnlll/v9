# Final verdict

## `NO_EDGE`

Ten USD-M perpetual futures, six calendar months (2025-01-01 → 2025-07-01), four
control arms, 3,020 campaigns opened (3,011 closed; nine still open at cutoff),
one independent 10,000 USDT account per symbol per arm, maker 2 bps / taker 5 bps,
NautilusTrader 2.0.0rc4 default fill model.

The repo's own preregistration (`reports/evaluation/preregistration.json`) states:
*PASS is earned only by a V9 arm whose net-R exceeds the best matched control with a
bootstrap 95 % CI excluding zero after the registered Holm correction; otherwise
NO_EDGE / INCONCLUSIVE / STOP / INVALID.* No arm meets that bar.

| Arm | Net PnL | Mean net R / symbol | 95 % CI | Symbols + / − |
|---|---:|---:|---|---|
| A — V8 static H1 | +127.07 | +0.68 | [−7.00, +9.29] | 5 / 5 |
| B — V9 legacy H1 | −393.67 | −1.97 | [−5.76, +1.55] | 6 / 4 |
| C — V9 multi-horizon static | **+496.88** | +2.50 | [−1.53, +6.52] | 5 / 5 |
| D — V9 multi-horizon adaptive | −157.37 | −0.78 | [−6.15, +3.60] | 6 / 4 |

---

## The seven questions

### 1. Does V9 outperform V8 across markets after costs?

**No.**

* D − A = **−284.44 USDT** (6 of 10 symbols positive, sign-test p = 0.75).
* B − A = **−520.74 USDT** (4 of 10). Swapping the V8 decision source for the V9
  policy loses money even before the adaptive layer is switched on.
* D is beaten by its own static control C on 6 of 10 symbols.

On BTCUSDT 2025-Q1 the same comparison was D = +20.16 against A = −21.23. The
inversion across ten markets is the central result of this study.

### 2. Is the improvement consistent or concentrated?

**Concentrated, and there is only one improvement worth naming.**

C − B = **+890.55 USDT**, positive in **8 of 10** symbols — the only pair that behaves
like a systematic effect. But AVAXUSDT (+276.86) and LINKUSDT (+222.86) alone account
for all of C's total; remove both and C is −2.84 USDT. Arm C is +496.88 USDT on
100,000 USDT of aggregate equity, i.e. **+0.50 % over six months** (or +4.97 % per
10,000 USDT book), which is not a return anyone can trade.

Everything else is concentrated in one or two coins, in both directions: DOTUSDT
alone contributes −464.37 of the −654.25 D − C gap.

### 3. Does adaptive management add incremental value?

**No — it subtracts 654.25 USDT, and the mechanism is identified.**

| Configuration | Net PnL | Δ vs static |
|---|---:|---:|
| `C_STATIC` (fixed 2 %/4 %, no management) | +496.88 | — |
| `TARGET_ONLY` (structural target) | +638.51 | +141.63 |
| `STOP_ONLY` (structural stop) | −50.43 | −547.31 |
| `BE_ONLY` (break-even stop amendments) | −517.01 | −1,013.89 |
| `FULL` (= arm D) | −157.37 | −654.25 |

Entry-matched decomposition: on the 472 campaigns both C and D took, the difference
is only **−33.08 USDT**. The other **−621.17 USDT is portfolio-path effect** — an
earlier structural exit frees capital into a different next trade.

Two mechanisms are individually harmful on *identical* entries: the structural stop
(−486) and break-even amendments (−732). The break-even amendment is the clearest
failure: it lifts the win rate from 36.4 % to 52.8 % while losing 1,014 USDT, because
scratching winners at entry cancels the 2:1 bracket's entire payoff.

**Thesis exits: zero.** The native ledger records no `EXIT` order in any A/B/C/D arm.
Nothing in the adaptive arm's result can be attributed to a thesis exit.

### 4. How fragile is the result to slippage?

Very. At reference cost six symbols are positive for arm D; the break-even added cost
per side is:

| Symbol | Break-even added cost |
|---|---:|
| ETHUSDT | +3.31 bps |
| LINKUSDT | +4.16 bps |
| AVAXUSDT | +4.81 bps |
| XRPUSDT | +7.58 bps |
| BNBUSDT | +8.82 bps |
| SOLUSDT | +17.34 bps |

Three basis points per side — roughly half a tick on a 2,500 USDT instrument — kills
ETHUSDT. At +10 bps only SOLUSDT survives. The arm's aggregate is already −157.37 at
reference cost, so the stress has nothing to erode; it only deepens the loss.

Arm A is the mirror image: it earns 1,371.56 USDT gross and keeps 127.07 after 1,170
USDT of commission — a 9.3 % retention rate. **The best arm in the study is a
high-turnover strategy whose edge is almost entirely paid to the venue.**

### 5. Which experts actually contribute useful observations?

**None can be credited, and two never spoke.**

* The panel emits a SUPPORT vote on **1.04 %** of 1,216,320 expert-bar evaluations and
  a CONTRADICT on 0.39 %.
* `min_support_quorum = 1` with `max_contradiction_tolerance = 28` means **one
  support vote out of 28 authorises a trade** and contradiction can never block. That
  is a configuration property of the frozen code, and it is the largest structural
  risk in the decision stack.
* `funding_crowding_reversal` and `open_interest_divergence` abstained with
  `MISSING_INPUT` on ≥98 % of their evaluations in every arm: the archives carry no
  open interest, and the harness never populates positioning readings. They cast zero
  votes and therefore explain nothing.
* Arms C and D produce **identical** stance counts. Management changes exits, not
  evidence — so no C-vs-D difference can be blamed on or credited to an expert.

### 6. What remains necessary before a sealed OOS test?

1. **A pre-registered PASS criterion for this exact matrix**, including the
   hypothesis budget. This study ran 9 comparisons against a registered budget of 5
   (`max_hypotheses: 5`, Holm correction). Exceeding it is a deviation that must be
   declared and corrected for before any PASS could be claimed.
2. **A decision on the adaptive layer.** The mechanism as frozen loses money out of
   sample; running a sealed test on it as-is would spend OOS budget on a known loser.
   The only defensible candidate for OOS is **arm C** (multi-horizon context, static
   bracket), which is the one change that generalised.
3. **Cost realism.** Quote/trade data for maker-fill probability and realised
   slippage, without which a 3 bps survival margin cannot be verified.
4. **Independent market samples**, not ten correlated coins over one six-month regime.
5. **Position-id uniqueness** in the simulator, so funding can be attributed per
   campaign instead of estimated.

### 7. Is the strategy economically feasible on a 20 USDT account?

**No. This is a separate feasibility finding, not an extrapolation.**

At 1× leverage, a 20 USDT account can hold at most 20 USDT of notional. The venue's
minimums (measured against the window's median prices) are:

| Symbol | Median price | Min notional | Min tradable notional | Feasible at 20 USDT |
|---|---:|---:|---:|---|
| XRPUSDT | 2.31 | 5 | 5.00 | yes |
| DOGEUSDT | 0.19 | 5 | 5.00 | yes |
| ADAUSDT | 0.72 | 5 | 5.00 | yes |
| DOTUSDT | 4.45 | 5 | 5.00 | yes |
| SOLUSDT | 152.12 | 5 | 5.00 | yes |
| BNBUSDT | 641.92 | 5 | 6.42 | yes |
| ETHUSDT | 2,494.03 | 20 | 20.00 | no — 100 % of equity |
| LINKUSDT | 15.01 | 20 | 20.00 | no — 100 % of equity |
| LTCUSDT | 93.76 | 20 | 20.00 | no — 100 % of equity |
| AVAXUSDT | 21.86 | 5 | 21.86 | no — integer contract exceeds equity |

Beyond tradability:

* A 5 USDT minimum position on 20 USDT equity is **25 % of capital per trade**, versus
  10 % in these simulations — a different risk process, not a scaled copy.
* Round-trip fees of ~9 bps on a 5 USDT position are 0.0045 USDT against a 0.10 USDT
  stop risk: **4.5 % of every unit of risk is consumed by fees**, before slippage.
* Arm C's measured edge is +0.047 R per campaign. On this account that is 0.0047 USDT
  per trade, or roughly **0.25 USDT per six months per symbol** (≈1.3 %), with no
  capacity to diversify across the five tradable symbols.
* Arm D — the arm this study was commissioned to validate — is negative before any of
  these constraints apply.

The measured system cannot be reproduced at 20 USDT, and the fraction of its edge that
survives the account minimums is too small to matter.

---

## What would change the verdict

* A **preregistered** evaluation of arm C alone on an untouched window, with a cost
  model that includes observed fill probability.
* A **redesigned management layer** in which break-even amendments do not truncate
  winners — the current mechanism is measurably the largest single destroyer of value
  (−1,014 USDT standalone).
* A **quorum that can say no**: with contradiction unable to block and one support
  vote sufficient, the panel is not filtering anything.

## What must not be claimed

* Production readiness, live trading suitability, or any positive expectancy.
* That the BTC Q1 development result was wrong — it was a real measurement of one
  window; it simply does not generalise.
* That any of these results is out-of-sample evidence of edge: they are
  cross-market generalisation results on a window that also contains the design
  period's market regime.

## Status of the artifacts

`PROMISING_UNVALIDATED` was not awarded. `INCONCLUSIVE` was considered and rejected:
the sample is large enough to conclude that the adaptive layer is worse than its own
static control across these markets, which is a negative result, not an absence of
evidence. `INVALID` was considered and rejected: the frozen baseline reproduces
bit-for-bit, the four documented arms match, reconciliation closes to ≤ 0.0062 % of
turnover, and one protection-invariant violation in 1,709 V9 campaigns is reported
with its materiality — none of it invalidates the measurements.
