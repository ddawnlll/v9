# M0-02 — repair: arithmetic reconciliation is independent and reachable

- Audit defect: **P3 — tautological arithmetic** (`audit/oracle_two_campaign.py:679-690`)
- Worktree: `/Users/hootie/src/v9-worktrees/fixaudit`, branch `fix/audit-closure`
- Base: `integration/wave1` @ `7b288cc`
- Date: 2026-11-05

## Defect

`recompute()` computed `final_equity` per campaign as
`initial + realized + funding - fees + unrealized`, then "verified" it with an `identity`
that re-evaluated the very same expression from the same campaign fields and compared it
to the same total. The check could not fail: `ARITHMETIC_MISMATCH` at report level was
unreachable, and `reconcile_campaign_arithmetic` did not exist.

## What changed (`src/v8_next/audit/oracle_two_campaign.py`)

- Removed the tautological report-level `identity != total_final` block.
- New public `reconcile_campaign_arithmetic(ledger, *, funding_rows=())`:
  - rejects a funding row belonging to another campaign (`CAMPAIGN_MISMATCH`);
  - re-derives `realized_pnl`, `fees` and `funding` from the per-order `OrderLedger`
    rows (`sum(order.realized_pnl)`, `sum(order.fees)`) and from the funding records
    themselves, and raises `ARITHMETIC_MISMATCH` on any disagreement with the campaign
    total;
  - re-derives `final_equity` from those independent sums via
    `_independent_final_equity(...)` and raises `ARITHMETIC_MISMATCH` if it differs.
- `recompute()` now calls it for **every** campaign before returning the report. The
  check is non-tautological because campaign totals are accumulated fill-by-fill in
  `_recompute_campaign` while the order ledgers are accumulated order-by-order, and the
  final equity is no longer re-evaluated from the campaign's own totals.
- `reconcile_campaign_arithmetic` added to `__all__`.

## Evidence

`tests/test_accounting_oracle_two_campaign.py`: **18 passed, 0 failed, 0 skipped**
(was 14; 14 pre-existing tests kept unchanged, 4 added).

| Acceptance requirement | Test | Observed |
|---|---|---|
| re-derived totals agree for the hand-computed fixture | `test_arithmetic_reconciliation_closes_for_every_campaign` | no exception for `campaign-alpha` / `campaign-beta` |
| mismatch branch is reachable | `test_mutated_campaign_total_raises_arithmetic_mismatch[final_equity]` | `OracleViolation` code `ARITHMETIC_MISMATCH`, record id `campaign-alpha` |
| mismatch branch is reachable | `test_mutated_campaign_total_raises_arithmetic_mismatch[realized_pnl]` | same code/record |
| mismatch branch is reachable | `test_mutated_campaign_total_raises_arithmetic_mismatch[fees]` | same code/record |
| mismatch branch is reachable | `test_mutated_campaign_total_raises_arithmetic_mismatch[funding]` | same code/record |
| foreign funding row cannot be summed in | `test_funding_row_from_another_campaign_is_rejected` | `CAMPAIGN_MISMATCH`, record id `FUND-B1` |

The mutation test corrupts one campaign total with `dataclasses.replace` and asserts the
typed branch fires, i.e. it proves the branch that was previously dead code is now live.

## Commands run (worktree `/Users/hootie/src/v9-worktrees/fixaudit`)

```
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_accounting_oracle_two_campaign.py
# 18 passed in 0.17s
/Users/hootie/src/v9/.venv/bin/python -m ruff check src/v8_next/audit/oracle_two_campaign.py \
    tests/test_accounting_oracle_two_campaign.py
# All checks passed!
/Users/hootie/src/v9/.venv/bin/python -m mypy src/v8_next/audit/oracle_two_campaign.py
# Success: no issues found in 1 source file
```

## Residual risks

- `reconcile_campaign_arithmetic` checks money that is representable in the ledger rows.
  `unrealized_pnl` has no per-order representation (it depends on the FIFO lot book and the
  final mark), so it enters `final_equity` as given; a corruption confined to
  `unrealized_pnl` is caught only through `final_equity`.
- The funding check compares the campaign total to the funding rows the caller passes; a
  caller that passes the wrong rows is caught by the campaign-id guard, but a caller that
  passes no rows while the ledger carries funding fails (`funding != 0`) rather than
  silently passing.
- The oracle still imports no engine path (AST test unchanged); reconciliation consumes
  only the ledger it just built and the caller-supplied rows.
