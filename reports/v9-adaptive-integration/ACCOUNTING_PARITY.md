# Accounting reconciliation — BLOCKED for marked-equity comparisons

Inspected base `d0430a64ebdeb44545115826e06a2e00caa36bea`. This is a code and
archived-receipt reconciliation, not a fresh replay. The receipt's source tape
is `research/tape/btcusdt-1h-2025Q1/tape.jsonl`, SHA-256
`84978f2d53a70afb769f760380b1587f073edfdc07d628c3a3be6773514673d7` per
`tape.manifest.json`. No claim is made that the receipt alone proves all native
cashflows or the entire account path.

| Contract or quantity | Earlier sim baseline | Portfolio baseline / current sim |
|---|---|---|
| Historical sizing | 0.01 BTC fixed, `target_notional=None` at `2aa0b7c`, `SimArmConfig.baseline_config` | portfolio `per_leg_notional=1000`; current sim `target_notional=1000` |
| Initial account | 10,000 USDT default for both runners | same default |
| Historical native balance delta | −8.57430674 USDT, 44 closed | −21.23495118 USDT, 44 closed |
| Same-sizing archived result | not applicable | both report −21.23495118 USDT, −0.021638104017588564 mean net R, fees 37.91535683 USDT |
| Funding attribution | position adjustments | sim adjustments +0.36718638; portfolio funded/unfunded balance difference +0.70494565 USDT |
| Open at cutoff | one (archived receipt) | one (archived receipt) |

Sources: `reports/provenance/P1-SIM-ARM.md`,
`reports/overnight/evidence/P1-SIM-ADAPTIVE-ARM.md`,
`reports/overnight/evidence/sim-arm-comparison.json`, the historical and current
`src/v8_next/adapters/adaptive_strategy.py`, and
`src/v8_next/evaluation/{sim_arm,ablations}.py`. Historical source is verified
by `git show 2aa0b7c:src/v8_next/adapters/adaptive_strategy.py`. The archived
same-sizing parity supports sizing as the identified contract difference behind
the prior balance-delta discrepancy; no separate counterfactual attribution of
each cashflow was executed here. The earlier report's apparent cross-run
comparison did not hold position sizing constant.

Both `run_baseline_arm` and `run_native_baseline` report `balance_total -
initial_balance` as `net_pnl` and sum native fill commissions. Neither
calculation establishes marked-equity return: `economic_state()` does not
export cutoff unrealized PnL or a cutoff valuation mark. Their reported max
drawdown is reconstructed from closed campaign realized PnL, not a
calendar-marked equity curve. Reported per-trade net R uses opening notional
and static bracket risk; it is not a capital-occupancy measure. Funding marks
use bounded as-of OHLC close, a historical model, not venue mark prices.
Execution/slippage is an engine profile assumption; captured order-book depth
is unavailable. Both reports include funding in native balance, but their
funding *attribution* procedures differ. The 0.33775927 USDT discrepancy
between the two attributed funding figures is **unexplained** by available
aggregate receipts. Equality of balance deltas does not reconcile this residual;
position-by-position settlement events, open-position treatment and signed
adjustments must be independently reconstructed to identify it. Funding is not
zero merely because an adjustment is absent.

`compare_economic_contracts` in `src/v8_next/evaluation/accounting_contract.py`
is an explicit, fail-closed building block for binding tape, window, capital,
sizing, fees, funding, mark, exposure and cutoff assumptions. Unknowns and
mismatches block it, including the old fixed-quantity and marked-equity versus
balance bases. `compute_delta` now requires matching serialized contracts, in addition to the
existing provenance checks, and cross-checks tape, profile and fees against the
receipt. Legacy receipts remain readable but cannot produce deltas when the
contract is absent. **The runners do not yet populate this contract**, so their
cross-run economic deltas remain blocked until their actual assumptions can be
bound and independently audited.
Even matching declarations alone cannot prove accounting correctness.

Commands executed in the isolated worktree:

```
git show 2aa0b7c:src/v8_next/adapters/adaptive_strategy.py
uv run --locked --extra dev pytest -q tests/test_accounting_contract.py
uv run --locked --extra dev ruff check src/v8_next/evaluation/accounting_contract.py tests/test_accounting_contract.py
uv run --locked --extra dev mypy src/v8_next/evaluation/accounting_contract.py
```

The fixture checks contract matching, fixed-quantity vs notional sizing,
marked-equity vs balance delta, and missing funding data; it is deterministic
and carries no simulated or real-market PnL. A native settlement-level audit,
mark-to-market equity series, comparable random controls and statistical
inference with dependence-preserving blocks remain **NOT_RUN**. No new native
replay or economic superiority conclusion was produced.

## Funding snapshot investigation

A separate small deterministic native fixture, based on
`test_two_closed_campaigns_survive_same_native_netting_id_and_reconcile` in
`tests/test_native_engine.py`, observed two closed campaigns sharing the same
NETTING position ID. The first campaign closure recorded −1.00000000 USDT in a
funding adjustment; the second recorded none. At replay end,
`economic_state()` contained one final position snapshot, corresponding to the
second campaign, with no adjustments, although account balance included both
campaigns. Therefore `sim_arm._funding_total`, which previously summed only
`engine.cache.positions()` at cutoff, can omit funding from earlier
incarnations. This supplies a demonstrated mechanism for undercounting, but
**does not prove that the archived +0.33775927 USDT residual is exactly that
omission**; matching the historical residual requires the archived funded and
unfunded native fill, settlement and position-incarnation timeline with
independently bound quantities, prices and cashflows. The archived totals alone
cannot establish that identity. The dual-run `trade_signature` verifies opens,
not every exit/fill or account cashflow, so its `VALID` label is also not an
independent full funding audit.

`_funding_total` now refuses the final-cache aggregate for multiple campaign
openings or missing opening records with cached positions; unparsable
adjustments and missing in-window settlements also yield `None` and an
unsupported reason rather than a partial or zero figure. This is deliberately
conservative: it does not reconstruct funding or change account balance.
`tests/test_accounting_contract.py::test_funding_snapshot_refuses_multiple_netting_campaigns`
is a deterministic regression; the native two-campaign fixture has additional
assertions pinning the observed history loss. The standalone fixture was run
before those assertions were added and returned the two campaign closures and
final position snapshot described above; the modified native test is **NOT_RUN**
pending the shared native runner. No profitability inference follows.

Follow-up checks: `uv run --locked --extra dev pytest -q
tests/test_accounting_contract.py` (4 passed); targeted Ruff on
`sim_arm.py`, `test_accounting_contract.py`, `test_native_engine.py` and mypy
on `sim_arm.py` passed.
