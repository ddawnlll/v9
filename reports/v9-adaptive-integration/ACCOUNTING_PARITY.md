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
