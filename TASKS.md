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
| Draft PR | https://github.com/ddawnlll/v9/pull/71 (base `integration/wave1`, draft, not merged) |

`main` is `0b69fc4` and holds none of the integration work. PR #67
(`integration/wave1` = `09908af`) and PR #70 (`codex/adaptive-integration` =
`aab394a`) diverge from `d0430a6` in different directions with overlapping edits
to `sim_arm.py`; neither was merged or required here.

## Final state — research program closed with NO_EDGE

Seven preregistered mechanisms, all `NO_EDGE` on the development window
(2025-01-01 → 2025-05-01). Full table and diagnosis: `reports/research/LEDGER.md`
("Program verdict").

The two beta-free experiments (E-005..E-007) bound every measured gross effect
within ±8 bps of notional per campaign; the registered cost floor is ~11 bps.
No mechanism cleared costs. The **holdout 2025-05-01 → 2025-07-01 is sealed and
unread** (`VALIDATION_DATA_UNSPENT`): no candidate qualified to spend it.

## Verified engineering state

* Fast suite `pytest -q`: **2862 passed, 72 skipped, 512 deselected**.
* `mypy src`: clean. `ruff check src tests tools`: clean (the last pre-existing
  `I001` in `tools/performance/expert_trace.py` is fixed).
* Native parity on the frozen BTCUSDT Q1 slice: A/B/C/D decision digests, trade
  digests and net PnL reproduce to six decimals.
* Research screen defects found and fixed:
  * `screen.load_bars` exposed tape **milliseconds** as `end_ns`; a native
    nanosecond timestamp compared against it skipped every bar. Converted, with a
    regression test (`6929314`).
  * `screen.evaluate_path` had no slippage or funding; both are now first-class,
    reported separately, with the accounting identity under test (`0157fc4`).
  * `screen.load_flow` added for aggressive taker-buy share (`b3a312c`).
* Shared-account multi-instrument execution already exists:
  `run_portfolio_backtest` runs one engine, one MARGIN/NETTING account, several
  instruments, native funding settlement — verified with two legs (one
  `balance_total`, 50 settlements fed).

## Reproduce

```bash
cd /Users/hootie/src/v9-worktrees/edge
uv run --locked --extra dev pytest -q
uv run --locked --extra dev python tools/signal_screen.py --candidate all
uv run --locked --extra dev python tools/cross_sectional_screen.py --mode momentum --sensitivity
uv run --locked --extra dev python tools/cross_sectional_screen.py --mode flow --control-draws 50
```

Tapes are gitignored; symlink or rebuild the ten `research/tape/<symbol>-1h-2025H1`
directories from the verified archives before running the screens.

## Remaining blockers / limitations

| # | Item | State |
|---|---|---|
| 1 | Holdout unread | Sealed by design; unseal only for a frozen `DEV_CANDIDATE` |
| 2 | No raw-signal strategy in the native shared-account runner | Needed only for a finalist; none exists |
| 3 | Screen charges funding on entry notional | Declared; the native path settles exactly |
| 4 | Cross-market `sim_arm` still opens one account per symbol | Its own limitation; `run_portfolio_backtest` is the shared-account path |

## Next action

None inside the current protocol. A future session should only reopen the
research program with an information source whose gross effect is measured above
the ~11 bps cost floor, or with the native shared-account runner once a candidate
exists.
