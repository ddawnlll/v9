# Opportunity census — five grammars, observational only

Research-only diagnostic. It runs every bar of every verified H1 tape through all
five registered opportunity grammars, resolves each episode's bracket outcome after
its horizon has closed, and touches no strategy, order, engine or account. The
extra grammar opportunities were **never** inserted into the A/B/C/D execution arms,
which use `range-breakout-48-v1` only.

Authority: `OFFLINE_PIT_CENSUS`. Claim: `NO_ECONOMIC_CLAIM`, costs not applied.
Evidence: `data/census/<SYMBOL>.json`.

---

## 1. The measurement

* 4,344 bars per symbol, 43,440 total, one `CausalFrame` per bar.
* Bracket geometry for scoring: stop 2 %, target 4 %, **stop evaluated first**
  inside a bar — the same convention as the executed arms.
* Episodes are deduplicated inside each grammar's own horizon: a repeat trigger in
  the same direction is a *fragment* of the existing episode, not a new one. This
  is the difference between "detection bars" and "distinct economic hypotheses".
* An episode resolved by neither stop nor target inside its horizon is a `timeout`;
  one still open at the tape end is `censored`.

With a 2 % stop and a 4 % target, a win pays +2 R and a loss costs −1 R, so the
**structural break-even hit rate is 33.3 %**. Costs only raise it.

---

## 2. Result: detection volume and hypothesis count differ by an order of magnitude

| Grammar | Episodes (10 symbols) | Fragments | Target | Stop | Timeout | Censored | Hit rate | Break-even gap |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| trend-continuation-v2 | 2,300 | 14,667 | 587 | 1,189 | 520 | 4 | 25.5 % | −7.8 pp |
| compression-expansion-v2 | 1,251 | 1,790 | 316 | 730 | 205 | 0 | 25.3 % | −8.0 pp |
| volatility-extreme-v2 | 1,167 | 6,512 | 352 | 739 | 75 | 1 | 30.2 % | −3.1 pp |
| mean-reversion-v2 | 676 | 659 | 95 | 322 | 256 | 3 | 14.1 % | −19.2 pp |
| **range-breakout-48-v1** | **220** | 2,310 | 84 | 135 | 0 | 1 | **38.2 %** | **+4.9 pp** |

Only the grammar the frozen strategy actually uses clears the 33.3 % structural
barrier before any cost. The other four sit below it — in this window they are
structurally losing propositions, and no amount of position management fixes a
negative-expectancy entry grammar.

This is measurement of a 2 %/4 % bracket, not a claim that those grammars are
useless with different exits.

| Symbol | range-breakout episodes | hit rate | trend-continuation | volatility-extreme | mean-reversion | compression-expansion |
|---|---:|---:|---:|---:|---:|---:|
| ETHUSDT | 21 | 38.1 % | 238 | 118 | 73 | 99 |
| SOLUSDT | 22 | 36.4 % | 232 | 115 | 72 | 134 |
| BNBUSDT | 22 | 36.4 % | 235 | 115 | 69 | 43 |
| XRPUSDT | 22 | **18.2 %** | 228 | 113 | 58 | 118 |
| DOGEUSDT | 22 | 45.5 % | 225 | 114 | 63 | 141 |
| ADAUSDT | 22 | 36.4 % | 232 | 120 | 62 | 154 |
| AVAXUSDT | 23 | **52.2 %** | 232 | 118 | 82 | 154 |
| LINKUSDT | 22 | 45.5 % | 225 | 117 | 75 | 145 |
| DOTUSDT | 21 | 47.6 % | 227 | 114 | 67 | 134 |
| LTCUSDT | 23 | 26.1 % | 226 | 123 | 55 | 129 |

The range-breakout hit rate ranges from 18.2 % (XRPUSDT) to 52.2 % (AVAXUSDT) on
~22 episodes per symbol. At that sample size each symbol's rate carries a ±20 pp
95 % interval, so the cross-symbol ordering is not established by this table — it
is a description of what happened, not a ranking of markets.

---

## 3. Overlap and opposing directions

| Symbol | Bars with >1 grammar | Bars with opposing directions |
|---|---:|---:|
| ETHUSDT | 703 | 64 |
| SOLUSDT | 809 | 66 |
| BNBUSDT | 716 | 54 |
| XRPUSDT | 738 | 52 |
| DOGEUSDT | 785 | 53 |
| ADAUSDT | 768 | 52 |
| AVAXUSDT | 848 | 54 |
| LINKUSDT | 808 | 60 |
| DOTUSDT | 799 | 41 |
| LTCUSDT | 757 | 45 |
| **Total** | **7,731** | **541** |

541 bar-observations carry two grammars asserting *opposite* directions (for
example a trend-continuation LONG and a mean-reversion SHORT). With one grammar
active at a time, those conflicts are resolved by the configuration, not by policy
— the same observation the single-tape P0 census made on BTCUSDT.

---

## 4. What this does and does not license

**Established.** The frozen grammar is the sparsest of the five (220 episodes vs
2,300 for trend-continuation) and the only one whose gross 2 %/4 % hit rate clears
the bracket's break-even. Episode counts differ from detection counts by 10×
because repeats inside a horizon are the same hypothesis.

**Not established.** That the other four grammars are worthless (different exits,
costs and holding rules were not tested); that 220 episodes is a sufficient sample;
that any individual symbol's hit rate will persist. Nothing here feeds a decision,
and the census results were not used to choose which grammar to trade — that choice
was frozen before this validation began.
