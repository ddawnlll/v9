# Execution safety and accounting reconciliation

Scope: what the engine actually did, what could not be audited, and which defects
were found and fixed before any number was published.

**No order left this machine.** The only execution object in the measurement path
is a NautilusTrader `BacktestEngine`; `sim_arm` imports no venue adapter, reads no
credential and has no dry-run or live switch. `SimArmConfig.enabled` defaults to
`False` and is set explicitly per run.

---

## 1. Execution configuration (identical for all matched arms)

| Item | Value |
|---|---|
| Engine | NautilusTrader 2.0.0rc4, `BacktestEngine` |
| OMS | `NETTING`, one position per instrument |
| Account | `MARGIN`, leverage 1, 10,000 USDT per symbol per arm |
| Venue | `BINANCE`, `liquidation_enabled=False` |
| Fill model | `default`, `SLIPPED=False` |
| Execution profile | `baseline`, digest `e7e76a80da652514…` |
| Maker / taker fee | 0.0002 / 0.0005 |
| Funding | engine `FundingRateUpdate` + mark-price proxy from a *closed* bar |
| Bar data | H1 `LAST-EXTERNAL`; M15 and H4 are context only |
| Decision cadence | H1 close |

Each symbol × arm is a separate 10,000 USDT account, not a shared portfolio. The
ten accounts are never netted against each other, so the aggregate USDT totals in
the results tables are sums of independent experiments, not a portfolio return.

---

## 2. Reconciliation: net PnL vs its components

Basis, computed independently per arm:

```
net_pnl (account balance delta)
  == independent gross price PnL        (recomputed from every campaign's
     ± signed funding adjustments        recorded entry/exit prices and quantity)
     − fill commissions                  (summed from the native fill commission column)
     ± residual
```

The residual across all 40 arm-runs lies between **−0.0061 % and +0.0023 % of
turnover**, i.e. below 6 parts per million. Its cause is identified, not absorbed:
campaign records publish `avg_px_open`/`avg_px_close` rounded to the instrument's
price precision, so a price-recomputed gross differs from the engine's internal
Decimal gross by up to half a tick per fill. Turnover across the experiment is
6,028,616 USDT; total commissions are 2,734.27 USDT.

`Position.realized_pnl` in this engine version is **already net of commissions and
funding adjustments**. Treating it as gross — the obvious reading — double-counts
cost. That mistake was made, detected, and corrected here; the corrected basis is
the one used everywhere in these reports.

---

## 3. Order / fill / position / campaign linkage

| Arm | Orders (ENTRY) | Orders (STOP) | Orders (TARGET) | REDUCE | EXIT | Fills | Campaigns opened (closed + open at cutoff) |
|---|---:|---:|---:|---:|---:|---:|---:|
| A (V8, roles are raw sides) | — | — | — | — | — | 2,619 | 1,311 |
| B | 593 | 593 | 593 | 0 | 0 | 1,184 | 593 |
| C | 533 | 534 | 534 | 1 | 0 | 1,065 | 533 |
| D | 583 | 603 | 593 | 10 | 0 | 1,174 | 583 |

Closed-only counts are A 1,308, B 591, C 531, D 581 — **3,011 closed**. The
table's campaign column counts entries, so the nine positions still open at the
cutoff are included (3,020 opened). Net PnL at cutoff is realised-only; those nine
open campaigns are not marked to market.

* Every entry produced a bracket: STOP count equals ENTRY count in B and C.
* **D issues 20 more stops and 10 more targets than entries** — those are the
  native stop/target re-issues (amendments). In other words the adaptive arm
  amended roughly 3 % of its stops and 2 % of its targets, and reduced a position
  10 times out of 583 campaigns.
* **`EXIT` role count is zero in every arm.** No thesis exit was ever emitted, so
  no result may attribute value to thesis exits. Any claim that "thesis exits
  contributed" is unsupported by the native event ledger.
* Zero `OrderRejected` events and zero skipped fills in the final run.

---

## 4. Protection invariants

The `BracketInvariantChecker` audits the V9 arms' protective-order lifecycle per
engine step. Across 1,709 V9 campaigns opened (B+C+D) there is **one violation**:

| Symbol | Arm | Code | Detail | When |
|---|---|---|---|---|
| LTCUSDT | D | `PROTECTION_QUANTITY_MISMATCH` | `stop=12.048 position=6.024` | 2025-06-27T17:00Z |

A partial reduce halved the position to 6.024 LTC while a reduce-only stop still
carried the original 12.048. That order was invalid for the remaining size — it
would have been rejected or clamped at a real venue. The affected campaign still
closed at **+16.55 USDT** (target), so no realised outcome changed; the defect is
one under-protected window in 581 campaigns, not a PnL driver.

**The V8 arm cannot be audited this way.** `ExpertEnsembleStrategy` does not emit
the protection ledger, so arm A reports `protection_state = NOT_APPLICABLE` and its
order roles are raw sides, not semantic roles. Consequently the A-vs-B/C/D
comparison is not symmetric on protection assurance: the V8 arm's bracket is
unverified by construction. This is a limitation of the frozen code, recorded here
rather than papered over.

---

## 5. Funding is measured, but only as a floor — read this before quoting it

The engine's own funding total (`booked funding`) is computed from
`Position.adjustments()`, which is only readable on the position records the cache
still retains. Both strategy classes **reuse position ids across campaigns**, so
the cache holds a single record and `booked funding` captures a small fraction of
the settlements actually charged — between 0 and 0.53 USDT per arm over six months.

An independent estimate integrates the recorded 8-hour settlement rates over each
campaign's holding window at its entry notional (`funding_usdt_estimated` in
`PER_SYMBOL_RESULTS.csv`). It is 10–30× larger than the booked figure, e.g.
AVAXUSDT arm A pays an estimated 13.15 USDT of funding against 0.10 USDT booked.

**Consequence:** funding is *not* an omitted cost in the reported `net_pnl` — the
engine's balance delta already includes every settlement it applied. What is
unreliable is the *attribution* of funding as a separate line item. Use the
independent estimate for magnitude, the balance delta for the result, and do not
claim a precise funding breakdown.

---

## 6. What was fixed before measuring (and the parity proof)

Three infrastructure defects would have made every altcoin number meaningless.

### 6.1 Instrument filters were BTC-calibrated

`portfolio_backtest._instrument` hard-coded `price_precision=2`,
`price_increment=0.01`, `size_increment=0.001`, `max_quantity=1000`,
`min_notional=1`, `margin_maint=0.05`.

On DOGEUSDT (tick 0.00001) that quantises every fill by up to 4.5 % of price, and
`max_quantity=1000` rejects the ~5,000-unit order that a 1,000 USDT notional needs.
Observed before the fix: `Skipping fill … fill price 0.337221 is not compatible with
DOGEUSDT-PERP.BINANCE price_precision=6 price_increment=0.000010`, followed by
`OrderRejected … Reduce-only order … would have increased position` and a campaign
stranded `UNPROTECTED`.

Fix: verified per-symbol filters (`v8_next/evaluation/instrument_metadata.py`)
threaded through `_instrument`, `_bars` and the mark-price update as an **explicit
optional argument**. `None` reproduces the frozen BTC values byte for byte.

A second-order trap: NautilusTrader quantises a `Price` to `price_precision` and
then rejects anything that is not an exact multiple of `price_increment`. Binance's
DOGEUSDT declares `pricePrecision=6` with `tickSize=0.000010`; pairing those
verbatim made half of all computed stop/target prices unfillable. The table now
derives precision from the increment and a validator rejects any pair that
disagrees.

### 6.2 Only BTC and ETH had an exposure id

`economics.decisions.linear_exposure_id` mapped exactly two instrument ids, and
`opportunity_at` returns `None` when the lookup misses. Every DOGE/ADA/XRP/SOL…
bar therefore short-circuited to "no opportunity": **zero trades for six months in
both the V8 and V9 arms**, which correctly looked like a strategy result and was
not one. Fixed by deriving the mapping from the verified filter table; unmapped
symbols still refuse rather than defaulting to BTC.

### 6.3 Reuse of position ids

Not fixed (it changes neither fills nor PnL) but disclosed: it is what limits the
funding attribution in §5.

### 6.4 Parity proof

After every change, the four documented BTCUSDT 2025-Q1 arms were re-run and
compared on decision digest, trade digest and net PnL:

| Arm | Expected net PnL | Re-run | Decision digest | Trade digest | Match |
|---|---:|---:|---|---|---|
| A | −21.234951 | −21.234951 | `4f53cda18c2baa0c` | `a2e716f7577c5e7a` | yes |
| B | −23.102285 | −23.102285 | `a874dbbd164b0cd4` | `a420388cde77e9fe` | yes |
| C | −13.880205 | −13.880205 | `6c3c541980c02fc5` | `a25c8c077a69ac38` | yes |
| D | +20.162531 | +20.162531 | `4aa9cdfce051ce5f` | `a901ba717f922371` | yes |

The proof was run three times: after the filter refactor, after the exposure fix,
and again inside the isolated worktree where the reported measurements were taken.

Applying the verified filter table to BTCUSDT itself (tick 0.01 / step 0.001 /
max 1,000 units, all matching the frozen values) reproduces −21.234951 and
+20.162531 unchanged, so the cross-market table and the frozen BTC slice share one
rule rather than two: the whole experiment is measured on verified venue filters.

---

## 7. Live-trading boundary

`SimArmConfig.enabled` is `False` by default and has no environment, file or CLI
override. `sim_arm` refuses `SIM_ARM_DISABLED` unless a caller constructs
`SimArmConfig(enabled=True)` explicitly, refuses any tape that is not an
integrity-verified `research/tape/<symbol>-<1h|15m>-<window>` slice, and refuses a
tape whose bytes do not hash to its manifest. Nothing in this validation changed
that boundary, added a venue adapter, or enabled any adaptive side effect outside
simulation.

## 8. Isolation of the measurement environment

During the run the shared working tree was concurrently modified by another process
(branch switched at 15:09:39 local, tracked files edited). Every number reported
here was therefore **re-measured inside a dedicated `git worktree` pinned to
`09908af`**, with only the four reviewed files carrying changes and with tapes
verified by digest at load. The parity table in §6.4 was produced in that worktree
before the measurement runs, so the frozen baseline and the cross-market results
come from the same code bytes.
