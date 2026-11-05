# P1 — Simulation-only adaptive execution arm and costed runner

**Branch:** `feat/sim-adaptive-arm` (merged into `integration/wave1`) · **Owner authorization:**
offline simulation only, issued 2026-11-05. No venue adapter, credential, dry-run venue mode or
network path is reachable from the new modules (asserted by an AST/import scan test).

## What exists

- `adapters/adaptive_strategy.py` (1,416 lines): a NautilusTrader `Strategy` whose entries come
  from the V9 chain (PIT view → `belief.advance` → `experts.evidence.produce_evidence` →
  `OpportunityBook` snapshot → `TraderDecisionPolicy.evaluate` → entry intent), while execution
  stays native (market entry, stop-market + limit bracket, reduce-only exits). Every emitted
  order carries `decision_id` and `sim_arm=True`; `advisory_only=False` only inside this arm.
- `evaluation/sim_arm.py` (791 lines): `run_sim_arm` / `run_baseline_arm`, refuse-by-default
  (`SimArmConfig(enabled=True)` is required), tape authorization + integrity check, and a
  measured `SimArmMeasurement` with fees, funding, provenance (tape/config/engine hashes).

## Measured (this session, `research/tape/btcusdt-1h-2025Q1`, 2160 bars, Nautilus 2.0.0rc4)

| Arm | Trades | Net PnL (USDT) | Net R | Max DD | Hit rate | Fees | Funding |
|---|---|---|---|---|---|---|---|
| V8 baseline (same runner) | 44 | −8.57430674 | −0.021638104 | 1.804 % | 34.09 % | 36.76607636 | 0.33380580 |
| V9 adaptive sim arm | 24 | −33.92107028 | −0.049251559 | 1.847 % | 33.33 % | 20.57124326 | 0.33380580 |

Reproduce: `python -c "from pathlib import Path; from v8_next.evaluation.sim_arm import SimArmConfig, run_sim_arm; print(run_sim_arm(tape=Path('research/tape/btcusdt-1h-2025Q1'), config=SimArmConfig(enabled=True)))"`
(≈71 s wall for the V9 arm).

## Status: `MEASURED_NOT_QUALIFIED` — no economic claim

The V9 arm's protection audit reports `MISSING_ACK ×50`, `PROTECTION_QUANTITY_MISMATCH ×2`,
`POSITION_QUANTITY_CONFLICT ×2`, `PROTECTIVE_SIDE_INCONSISTENT ×2`,
`BRACKET_GEOMETRY_INCONSISTENT ×1`, and a final checker state of `UNKNOWN`. At the cut there is no
open position and no open order (state `PROTECTED`), and the misses read as telemetry-binding
artefacts — ACKs arrive in the same engine step as the bracket submission, entry legs carry no
position id, and protection is resized only after the fill is observed — but that is an
*interpretation*, not a proof. **Therefore the arm is not safety-qualified and must not be used
as an economic comparison.** The 24 trades are also below the preregistered `minimum_trades=30`,
so even a clean run would be `INCONCLUSIVE` under the registered decision rule.

## Baseline discrepancy (unresolved, do not paper over)

Two V8 runners disagree: `sim_arm.run_baseline_arm` = −8.57430674 USDT / fees 36.76607636 vs
`ablations.run_native_baseline` = −21.23495118 USDT / fees 37.91535683 — same 44 trades, same net
R (-0.021638104). The difference is in PnL/balance accounting, not the trade population. Until it
is reconciled, only the matched pair from one runner may be compared, and the M4-02 figure must
not be quoted against the V9 arm.

## Next fixes required before any V9 economic statement

1. Emit ACK from the submission step deterministically (or bind the position snapshot after the
   bracket's ACK) so `MISSING_ACK` distinguishes a real gap from an intra-step ordering artifact.
2. Bind entry bracket legs to the position id they create, and resize protection on every
   partial fill before evaluating the invariant.
3. Re-run both arms; only then publish a costed V9 result (`publish_costed_result`), which still
   requires ≥30 trades and a Holm-corrected interval to leave `INCONCLUSIVE`.
