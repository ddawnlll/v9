# P1 — Simulation-only adaptive execution arm and costed runner

**Branch:** `feat/sim-adaptive-arm` (merged into `integration/wave1`) · **Owner authorization:**
offline simulation only, issued 2026-11-05. No venue adapter, credential, dry-run venue mode or
network path is reachable from the new modules (asserted by an AST/import scan test).

## What exists

- `adapters/adaptive_strategy.py`: a NautilusTrader `Strategy` whose entries come from the V9
  chain (PIT view → `belief.advance` → `experts.evidence.produce_evidence` → `OpportunityBook`
  snapshot → `TraderDecisionPolicy.evaluate` → entry intent), while execution stays native
  (market entry, stop-market + limit bracket, reduce-only exits). Every emitted order carries
  `decision_id` and `sim_arm=True`; `advisory_only=False` only inside this arm.
- `evaluation/sim_arm.py`: `run_sim_arm` / `run_baseline_arm`, refuse-by-default
  (`SimArmConfig(enabled=True)` required), tape authorization + integrity check, and a measured
  `SimArmMeasurement` with fees, funding and provenance (tape/config/engine hashes).
- `tests/test_sim_adaptive_arm.py`, `tests/test_observation_cache.py`,
  `tests/test_protection_invariants.py` (batch/fill semantics).

## Audit repairs completed in this round

| Finding | Repair |
|---|---|
| 50 `MISSING_ACK` on the V9 arm | Submission and its native resolution are published as one observation batch; the checker records only final-state violations (`apply_batch`). A genuine unresolved submission still reports `MISSING_ACK`. |
| Fill before/without an observable ACK | A venue fill is now accepted as implicit acknowledgement (stronger evidence than ACK). |
| `PROTECTION_QUANTITY_MISMATCH` / `POSITION_QUANTITY_CONFLICT` | Entry bracket legs bind to the created position; repeated netting snapshots clear the *current* conflict flag when they reconcile; a fail-closed overlap guard blocks a second entry until close is observed. |
| 5 non-ACK protection warnings | ACK/submission pairing, position binding, conflict lifecycle and batch evaluation; final state at cutoff with an open position is **PROTECTED**, violations **0**. |
| V8 runner disagreement (−8.57 vs −21.23) | Cause: fixed `0.01 BTC` sizing vs `per_leg_notional=1000`, not accounting. `SimArmConfig.target_notional=1000` now matches the measured portfolio runner exactly. |
| V9 95.8 s replay | `observation_id`/candle caches + `build_view(copy_inputs=False, assume_ordered=True)` fast path. |

## Baseline parity (now exact)

| Metric | sim runner | m402 portfolio runner |
|---|---|---|
| Trades | 44 | 44 |
| Net PnL | **−21.23495118** | **−21.23495118** |
| Net R | −0.021638104017588564 | −0.021638104017588564 |
| Fees | 37.91535683 | 37.91535683 |
| Funding attribution | 0.36718638 (sum of position adjustments) | 0.70494565 (funded vs unfunded balance difference) |

Net PnL, trade count and fees are **identical**. The funding *isolation method* still differs and
is reported as an attribution basis, not as a PnL discrepancy.

## Measured (full slice, `research/tape/btcusdt-1h-2025Q1`, 2,160 bars, Nautilus 2.0.0rc4)

Both arms use the same tape, engine, execution profile, fees, `target_notional=1000`, quorum 1 and
contradiction tolerance 28.

| Arm | Trades | Net PnL (USDT) | Net R | Max DD | Hit rate | Fees | Funding | Wall |
|---|---|---|---|---|---|---|---|---|
| V8 baseline (same runner) | 44 | **−21.23495118** | −0.021638104 | 1.998 % | 34.09 % | 37.91535683 | 0.36718638 | 0.75 s |
| V9 adaptive sim arm | 24 | **−23.10228485** | −0.049251559 | 1.798 % | 33.33 % | 20.91868621 | 0.36718638 | 13.04 s |

Δ (V9 − V8): **net PnL −1.86733367 USDT**, net R −0.027613, max DD −0.199 pp, fees −16.9967.

**V9 still underperforms V8 on this slice**, but the gap is now −1.87 USDT rather than −25.35 after
the sizing/accounting correction. Protection state at the cutoff (one open position): **PROTECTED,
0 violations**.

## Performance profile

| Stage | Before | After |
|---|---|---|
| V9 full slice | 95.8 s | **13.0 s** (7.4× faster) |
| V9 / V8 ratio | ~200× | **17.5×** |
| 500-bar profiled total | 11.0 s | 2.8 s |

Dominant hotspot was `domain/observation.py::build_view`: every bar re-deep-copied the full
observation journal and re-derived every `observation_id` (500 bars: 260k id calls, 4.8M deepcopy
calls, 7.4 s of 11 s). Caches plus an opt-in immutable fast path remove that quadratic work.
Remaining hotspots: `_candle_from_observation` (now cached), expert evidence production
(`produce_evidence`, 0.8 s / 500 bars) and the registry dispatch. Further optimization should
target evidence/frame digests, not the decision chain.

## Status: `MEASURED_INCONCLUSIVE_SAMPLE` — protection PASS, sample too small

- **Safety:** PASS for the invariant contract (state `PROTECTED`, 0 violations on the full slice).
- **Sample:** 24 closed campaigns < preregistered `minimum_trades=30`, so the economic result is
  `INCONCLUSIVE`, not a promotion.
- **Claim:** `NONE`; no profitability or generalisation claim.
- **Control arms:** matched random-entry controls and PIT regime splits are implemented, but their
  end-to-end comparison against a costed V9 arm is `NOT_RUN` in this report.

Artifact: `reports/overnight/evidence/sim-arm-comparison.json` (status
`MEASURED_INCONCLUSIVE_SAMPLE`, claim `NONE`).

## Next steps

1. Connect the matched random-entry control pool to the measured V9 arms and report the null
   comparison per PIT regime; add additional historical periods before any economic statement.
2. Reduce the remaining 17.5× replay gap (evidence/frame digests) without changing decisions.
3. Re-run with ≥30 closed campaigns and a Holm-corrected interval; only then consider a promotion
   verdict. Live approval remains owner-only and `false`.
