# V9 experiment ledger (append-only)

Every experiment is appended here, including failures. Do not rewrite history:
a correction is a new entry that names the entry it supersedes.

Status vocabulary: `PREREGISTERED`, `RUNNING`, `DEV_CANDIDATE`, `NO_EDGE`,
`INCONCLUSIVE`, `STOP`, `INVALID`, `NATIVE_VALIDATED`, `NATIVE_REJECTED`.

---

## 2026-11-05 — E-001..E-003: raw directional signal screen

* Preregistration: `reports/research/PREREGISTRATION_raw_signals.json`
  (code `0157fc4`, dataset role `DEVELOPMENT`).
* Dataset: 10 USD-M perpetuals, H1, 2025-01-01 → 2025-05-01. Holdout
  2025-05-01 → 2025-07-01 sealed.
* Cost model: taker 5 bps, maker 2 bps, slippage 2 bps per market side,
  actual venue funding inside the holding window.
* Exit: static 2% stop / 4% target, stop-first intrabar, 48-bar time exit.
* Controls: eligible-time random entry, both directions, identical policy,
  200 count-matched draws per symbol, seed 20260101.
* Status: `PREREGISTERED` (results pending).

| ID | Candidate | Primary hypothesis |
|---|---|---|
| E-001 | `bollinger_reversion` | 2-sigma H1 band breach mean-reverts net of costs |
| E-002 | `volume_climax_reversal` | 4x-median volume at a range extreme marks exhaustion |
| E-003 | `liquidity_sweep_reclaim` | prior 24-bar extreme sweep + reclaim continues |
