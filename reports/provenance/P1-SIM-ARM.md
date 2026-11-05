# P1 — Simulation-only V9 adaptive execution arm (offline measurement)

**Branch:** `feat/sim-adaptive-arm` · **Base:** `15379fb` (`integration/wave1`)
**Engine:** NautilusTrader 2.0.0rc4 `BacktestEngine`
**Tape:** `research/tape/btcusdt-1h-2025Q1` (2,160 bars, SHA-256 `84978f2d…4673d7`)

## Authorization boundary

- `AdaptiveSimStrategy` is a native `Strategy`; it can only run under a `BacktestEngine`.
- `SimArmConfig.enabled` is `False` by default; `run_sim_arm` refuses unless it is explicitly
  `True` (`SIM_ARM_DISABLED`).
- `sim_arm.py` imports no `nautilus_trader.adapters`, no credential source, no dry-run/live path.
- Orders exist only as engine events. There is no venue adapter and no CLI that can send one.
- The runner refuses an unauthorized tape (`TAPE_NOT_AUTHORIZED`) and verifies the manifest hash.

## Defects found by the independent audit and fixed here

| Defect | Fix |
|---|---|
| `Position.avg_px_open` is a native `float` in 2.0.0rc4; `.as_decimal()` raised on every open-position bar, silently aborting decisions | `_as_decimal` boundary helper used for avg price, exposure and re-protection |
| consensus gate defaulted to contradiction tolerance `0` while the measured V8 baseline uses `28` | default aligned to `audit.pipeline._config` (quorum 1, tolerance 28); both arms share it |
| protective ACK could enter the ledger before its buffered submission | submission + ACK buffered together and flushed when the position id is known |
| a position snapshot was published before same-batch protective ACKs | opened-position snapshot deferred to the next bar |
| a cache miss could allow an overlapping entry and under-sized protection | fail-closed `_seen_open_positions` guard blocks a second entry until close is observed |

## Measured comparison (one full slice, costed)

Both arms use the identical tape, engine, execution profile, initial balance, fees and
sizing/bracket parameters. `net_pnl` is the native account balance delta; fees are summed from
the fill commission column; funding from the engine's funding adjustments.

| Metric | V9 adaptive arm | V8 baseline | Δ (V9 − V8) |
|---|---|---|---|
| Bars | 2,160 | 2,160 | 0 |
| Closed campaigns | 24 | 44 | −20 |
| Open campaigns at cutoff | 1 | 1 | 0 |
| Net PnL (USDT) | **−33.92107028** | **−8.57430674** | **−25.34676354** |
| Mean net R | −0.049252 | −0.021638 | −0.027613 |
| Max drawdown | 1.847% | 1.804% | +0.043 pp |
| Hit rate | 33.33% | 34.09% | −0.76 pp |
| Fees (USDT) | 20.57124326 | 36.76607636 | −16.19483310 |
| Funding (USDT) | +0.3338058 | +0.3338058 | 0 |
| V9 actions | ABSTAIN 1437, HOLD 644, WAIT 54, ENTER 25 | n/a | n/a |
| Wall time | 95.8 s | 0.48 s | — |

**Result: V9 underperformed V8 on this single slice.** The V9 arm traded fewer, larger-loss
campaigns; the fee saving did not offset the worse per-campaign outcome. This is a descriptive
measurement, not an edge claim and not a generalisation.

Artifact: `reports/overnight/evidence/sim-arm-comparison.json` (status
`MEASURED_DESCRIPTIVE_ONLY`, claim `NONE`).

## Protection-invariant caveats (not suppressed)

The arm's ledger reports, on the full slice:

`MISSING_ACK=50`, `PROTECTION_QUANTITY_MISMATCH=2`, `POSITION_QUANTITY_CONFLICT=2`,
`PROTECTIVE_SIDE_INCONSISTENT=2`, `BRACKET_GEOMETRY_INCONSISTENT=1`.

`MISSING_ACK` is raised at the submission→ACK interval; the ACK events are present in the
ledger, and the submission/ACK ordering is now buffered. The quantity conflict occurs on the
netting position id shared by consecutive campaigns and is **not** claimed safe. The arm must
not be treated as production-ready until the protector feed is reconciled for repeated netting
positions.

## Verification

```
pytest -q tests/test_sim_adaptive_arm.py     -> 8 passed
ruff check src tests                         -> clean
mypy src                                     -> clean (252 files)
pytest -q                                    -> 2731 passed, 72 skipped, 512 deselected
```

## Not claimed

No live trading, no venue adapter, no profitability, no promotion. The negative result is
first-class evidence. Comparing these numbers with the audit runner (−8.6747) or the portfolio
baseline (−21.235) is only valid after equalising their accounting basis; the table above compares
the two arms produced by the same sim runner.
