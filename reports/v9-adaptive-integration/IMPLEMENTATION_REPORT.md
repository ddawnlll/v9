# Adaptive integration checkpoint

Base: `d0430a64ebdeb44545115826e06a2e00caa36bea` (fetched `origin/integration/wave1`, 2026-11-05). PR #67 remains draft. The primary worktree contained pre-existing changes to `adaptive_strategy.py`, `ablations.py`, and `.pi/`; this work uses an independent worktree.

## Dependency order and gates

1. Establish causal multi-horizon input contracts and 28 expert profiles; integrate only when availability and provenance are demonstrable (#10, #11, #14, #15).
2. Reconcile native event chronology and independent protection audit before authorizing adaptive management (#6, #23, #24). Submission is not ACK; a cancellation is not replacement protection.
3. Bind position thesis and advisory management to the simulation lifecycle; activate native actions only once the coverage and independent audit gates pass (#23, #24, #28).
4. Reconcile accounting contracts and only compare arms sharing data, sizing, execution, costs and marked-equity basis (#3, #7, #29, #30).
5. Profile with a single resource-aware native runner, test decision parity and publish wall/CPU/RSS and scientific qualification (#9, #30, #34).

## Baseline and known issues

The checked-in `reports/overnight/evidence/sim-arm-comparison.json` reports a prior 2,160-bar H1 development replay, 24 closed V9 campaigns and 44 V8 campaigns. This is historical evidence, not a newly executed result. It reports 13.036 s for V9 and 0.746 s for V8, and a funding attribution disagreement. The 24-trade sample fails the 30-trade floor. Existing tape inventory contains H1; a genuine M15/H4 replay is not established. The checker grouped submissions through later ACK events, so its recorded zero violations cannot alone establish uninterrupted protection. Native REDUCE/EXIT cancelled protection before execution and could leave exposure unprotected; these order side effects are now explicitly blocked pending a verified handoff.

`uv sync --locked --extra dev` completed in this worktree. Further command outcomes and artifacts will be recorded as work progresses. All simulation results remain distinct from live authorization and economic edge.
