# V9 autonomous research — working state

Append-only history lives in `reports/research/LEDGER.md`. This file is the
compact resume point for the next session.

## Baseline (pinned)

| Item | Value |
|---|---|
| Repository | `ddawnlll/v9` |
| Baseline commit | `a4cdf94ec861c48d1c4f21d547369a666a123674` (`validation/cross-market`) |
| Working branch | `research/edge-discovery` |
| Worktree | `/Users/hootie/src/v9-worktrees/edge` |
| Engine | NautilusTrader 2.0.0rc4, Python 3.12 |

Why this baseline: it is the newest integrated line (`d0430a6` + multi-horizon
expert profiles and protection repair `09908af` + the 10-symbol cross-market
study `06b981e`). `main` is at `0b69fc4` and contains none of the integration
work; PR #67 (`integration/wave1` = `09908af`) and PR #70
(`codex/adaptive-integration` = `aab394a`) are both draft and diverge from
`d0430a6` in different directions (25 vs 4 commits, overlapping edits to
`sim_arm.py`). Integrating PR #70 into this baseline is not required for the
research program and is not attempted without a written merge plan.

## Completed work

### 2026-11-05 — Phase 1 baseline verification

* Fast suite: `pytest -q` → **2845 passed, 67 skipped, 512 deselected** (16.1 s).
* `mypy src` → **no issues in 258 source files**.
* Native parity: `run_sim_arm` / `run_baseline_arm` on the frozen BTCUSDT Q1
  slice reproduce the documented A/B/C/D decision digests, trade digests and
  net PnL to 6 decimals (`-21.234951`, `-23.102285`, `-13.880205`, `+20.162531`).
* Cross-market verdict on the incumbent arms is `NO_EDGE` (see
  `reports/v9-cross-market-validation/FINAL_VERDICT.md`); that is prior evidence,
  not restated as a new claim.

## Known defects carried forward

| # | Item | State |
|---|---|---|
| 1 | `ruff check` reports `I001` import order in `tools/performance/expert_trace.py` | Pre-existing, mechanical, not yet fixed |
| 2 | Cross-market runs are **ten independently funded accounts**, never one shared account | Blocks shared-portfolio economics; Phase 4 work |
| 3 | Funding is a booked floor; open positions are not marked at cutoff | Reported in `EXECUTION_SAFETY.md` §5, `PAIRED_ARM_COMPARISON.md` §4 |
| 4 | `min_support_quorum=1` with contradiction unable to block | Configuration property of the frozen arm; not a defect that Phase 3 relies on |

## Active research hypothesis

Raw directional signals, screened independently of the V9 breakout gate:
`bollinger_reversion`, `volume_climax_reversal`, `liquidity_sweep_reclaim`
(H1, dev window 2025-01-01 → 2025-05-01, holdout 2025-05-01 → 2025-07-01 sealed).

## Next action

Write the preregistration, then the signal screen and its tests, then run the
three candidates on the development window.
