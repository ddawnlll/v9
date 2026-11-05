# V9 autonomous research — working state

Append-only history: `reports/research/LEDGER.md`. This file is the compact
resume point for the next session.

## Baseline (pinned)

| Item | Value |
|---|---|
| Repository | `ddawnlll/v9` |
| Baseline commit | `a4cdf94ec861c48d1c4f21d547369a666a123674` (`validation/cross-market`) |
| Working branch | `research/edge-discovery` |
| Worktree | `/Users/hootie/src/v9-worktrees/edge` |
| Engine | NautilusTrader 2.0.0rc4, Python 3.12 |

`main` is `0b69fc4` and holds none of the integration work. PR #67
(`integration/wave1` = `09908af`) and PR #70 (`codex/adaptive-integration` =
`aab394a`) diverge from `d0430a6` in different directions with overlapping edits
to `sim_arm.py`; neither is required for this program and neither is merged.

## Verified engineering state

* Fast suite `pytest -q`: **2851 passed, 72 skipped, 512 deselected**.
* `mypy src`: clean (259 files). `ruff check src tests tools`: clean except the
  pre-existing `I001` in `tools/performance/expert_trace.py`.
* Native parity on the frozen BTCUSDT Q1 slice: A/B/C/D decision digests, trade
  digests and net PnL reproduce to six decimals.
* Research screen defects found and fixed:
  * `screen.load_bars` exposed tape **milliseconds** as `end_ns`; a native
    nanosecond timestamp compared against it skipped every bar. Now converted,
    with a regression test (`6929314`).
  * `screen.evaluate_path` had no slippage or funding. Both are now first-class,
    reported separately, with the accounting identity checked by tests
    (`0157fc4`).
* Shared-account multi-instrument execution is **already implemented**:
  `v8_next.adapters.portfolio_backtest.run_portfolio_backtest` runs one
  `BacktestEngine`, one MARGIN/NETTING account, multiple instruments, native
  funding settlement (verified: two legs, one `balance_total`, 50 settlements
  fed). The cross-market report's "ten independent accounts" is a limitation of
  `sim_arm`, not of the repo.

## Research results (development window, 2025-01-01 → 2025-05-01)

| ID | Candidate | Net exp. R | Control | Status |
|---|---|---:|---:|---|
| E-001 | bollinger_reversion | −0.123 | −0.044 | NO_EDGE |
| E-002 | volume_climax_reversal | −0.222 | −0.044 | NO_EDGE |
| E-003 | liquidity_sweep_reclaim | −0.112 | −0.045 | NO_EDGE |
| E-004 | funding_extreme_reversion | −0.138 | −0.043 | NO_EDGE |
| E-005 | cross_sectional_relative_strength | pending | — | PREREGISTERED |

Cross-experiment finding: every single-symbol mechanism produced negative long
cells and positive short cells in a window with downward alt drift. The 50/50
random-entry null cannot separate timing from beta, so E-005 is dollar-neutral by
construction and uses a random two-and-two null at the same bars.

## Remaining blockers / known limitations

| # | Item | State |
|---|---|---|
| 1 | Holdout 2025-05-01 → 2025-07-01 | Sealed; runner refuses without `V9_HOLDOUT_AUTHORIZED=1` after a frozen candidate |
| 2 | No native strategy consumes raw signals in the shared-account runner | Needed only if a candidate passes dev; `ExpertEnsembleStrategy` is the only multi-instrument strategy today |
| 3 | Funding charged at entry notional in the screen | Declared limitation; native path settles exactly |
| 4 | Pre-existing ruff `I001` in `tools/performance/expert_trace.py` | Mechanical, unrelated |

## Active hypothesis

`cross_sectional_relative_strength` (E-005): long the two weakest / short the two
strongest trailing-24h perpetuals, matched random two-and-two null.

## Next action

Read the E-005 result; record it in the ledger with diagnosis. If it is
`NO_EDGE`, either test one further mechanism with a different information source
or stop the research program and report the exhaustion honestly. If it is
`DEV_CANDIDATE`, freeze it and run the sealed holdout before any native work.
