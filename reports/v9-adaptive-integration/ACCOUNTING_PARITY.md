# Accounting parity — V8 runners

Two historical V8 numbers were reported for the same 90-day slice:

| Historical runner | Reported net PnL | Status at current HEAD |
|---|---:|---|
| `sim_arm.run_baseline_arm` | ≈ −8.57 USDT | **not reproducible**; predates the V8 parity repair |
| `ablations.run_native_baseline` | ≈ −21.23 USDT | reproducible |

## Current measurement (base `d0430a6`)

Both runners were executed on `research/tape/btcusdt-1h-2025Q1` (SHA-256
`84978f2d53a70afb769f760380b1587f073edfdc07d628c3a3be6773514673d7`), 10,000 USDT, maker 0.0002,
taker 0.0005.

| Field | `sim_arm.run_baseline_arm` | `ablations.run_native_baseline` | Difference |
|---|---:|---:|---:|
| Trades | 44 | 44 | 0 |
| Open campaigns at cutoff | 1 | 1 | 0 |
| Net PnL (USDT) | −21.23495118 | −21.23495118 | 2.8 × 10⁻¹⁴ |
| Net R | −0.021638104017588564 | −0.021638104017588564 | 0 |
| Fees (USDT) | 37.91535683 | 37.91535683 | 0 |
| Funding (USDT) | **0.36718638** | **0.70494565** | 0.33775927 |

The monetary PnL, trade count, net R and fee totals are identical. The remaining difference is not an
economic difference; it is **funding attribution basis**:

- `sim_arm` sums the engine's booked `FUNDING` position adjustments (`0.36718638`);
- `ablations.run_native_baseline` isolates funding with a funded/no-funding dual replay and reports
  the **balance delta** (`0.70494565`). The funded pass also feeds mark-price updates, so that delta
  is an upper bound on funding, not the funding cost. The runner now publishes
  `FUNDING_BALANCE_DELTA_UPPER_BOUND` in `unsupported` (see `evaluation/ablations.py`).

## Why the historical −8.57 is not comparable

The `reports/provenance/P1-SIM-ARM.md` measurement at base `15379fb` used a different runner state:
no funding/mark feed parity and a different fee total (36.766 vs 37.915). The V8 parity repair in
`d0430a6` aligned the two arms; the test
`tests/test_sim_adaptive_arm.py::test_baseline_pnl_parity_with_the_measured_portfolio_runner`
pins the current identity (`−21.23495118`, fees `37.91535683`).

## Rule

Cross-run comparisons are valid only when the **accounting basis** matches:

- same initial balance, sizing, fee model and funding attribution;
- same tape bytes (hash) and window;
- open positions at the cutoff marked consistently (or declared `NOT_IDENTIFIABLE`).

The `−8.57` figure must not be quoted against `−21.23` as a V8 improvement or regression. It is a
different measurement basis.

## Known remaining limitation

Neither runner marks the open campaign at the cutoff into the primary net PnL; both report
`open_campaigns=1`. A marked-equity comparison remains `MARKED_EQUITY_BLOCKED` for that campaign
until an explicit mark source is bound.
