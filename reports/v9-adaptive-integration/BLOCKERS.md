# Blockers and open gaps

Nothing below is silently hidden in the positive numbers.

## 1. Decision cadence is still H1

The engine is fed only the H1 tape. M15 is **real**, verified intraday context consumed by the
eight M15-primary experts at the H1 decision cut; the strategy does not run `on_bar` on M15, so it
is not yet a 15-minute signal cadence. The profile table is correct, the cadence is not the
mission's "M15 opportunity evaluation" target.

Status: `PARTIAL`.

## 2. Target amendments and thesis-driven exits are not observed in the replay

Full slice: 17 native stop amendments, 0 target amendments, 0 thesis exits. The target policy and the
exit path are exercised by deterministic unit tests, not by market behaviour on this tape.

Status: `MECHANICS_TESTED`, `ECONOMIC_OBSERVATION_MISSING`.

## 3. Single instrument, single 90-day development window

All economic numbers in `ECONOMIC_COMPARISON.md` are one BTCUSDT slice, 23 closed campaigns. No
sealed OOS, no walk-forward, no regime split, no random-entry control on this slice.

Status: `DEVELOPMENT_ONLY`, `INCONCLUSIVE` for any edge claim.

## 4. 10-symbol / 180-day experiment not built yet

The verified public archives for 10 USD-M perpetuals (ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, DOT,
LTC) and 2025 H1 (1H, 15m, funding) were downloaded to a scratch directory (174/180 files). No slice
manifests have been committed, no tapes were built, and no multi-symbol run was executed.

Even when built, that experiment runs **independent 10,000 USDT books per symbol**. It is not a shared
portfolio and must not be reported as one. A true portfolio run needs a multi-instrument strategy and
a capital allocator, which this change set does not provide.

Status: `NOT_RUN`.

## 5. Marked equity at the cutoff

Both V8 runners report one open campaign at the cutoff and do not mark it into the primary PnL.
`MARKED_EQUITY_BLOCKED` remains for that campaign until an explicit mark source is bound.

## 6. Funding attribution

`sim_arm` reports booked FUNDING adjustments (`0.36718638`); `ablations.run_native_baseline` reports a
funded/no-funding balance delta (`0.70494565`), which is an upper bound because the funded pass also
feeds mark-price updates. The two are not interchangeable; see `ACCOUNTING_PARITY.md`.

## 7. Resource gate

Peak RSS 316 MiB in an uncapped process. No cgroup 4 GiB-limited run, no 24–48 h soak, no swap/disk
telemetry.

Status: `NOT_RUN`.

## 8. Unrelated working-tree files

`.pi/` and `src/v8_next/evaluation/grammar_census.py` / `tests/test_grammar_census.py` are untracked
from another session. They are deliberately **not** part of this change set.

## 9. Authority

No live trading, no credentials, no venue adapter, no automatic `main` merge, no promotion. The
adaptive orders exist only inside `BacktestEngine`. The D arm is a simulation measurement.
