# V9 adaptive integration — implementation report

**Base commit:** `d0430a64ebdeb44545115826e06a2e00caa36bea` (`integration/wave1`, upstream of PR #67)
**Normative source:** `docs/V9_ARCHITECTURE_MONOGRAPH.md` (RFC 1.4.1)
**Scope:** simulation-only. No venue adapter, no credentials, no live order path, no `main` merge.

## What this change set does

The integrated arm previously evaluated all 28 experts on a single H1 `CausalFrame` and protected
every position with a fixed 2% stop / 4% target. This change set restores the architecture's two
missing behaviours and repairs the protection telemetry that made the first measurement unsafe to
read:

| Area | Before | After |
|---|---|---|
| Expert evaluation | one H1 frame for all 28 witnesses | versioned per-expert primary horizon (M15/H1/H4) with shared PIT context |
| Higher timeframe | not supplied | H4 aggregated from **complete** H1 bars, availability = latest input |
| Intraday data | none | verified real M15 tape (`research/tape/btcusdt-15m-2025Q1`), never interpolated |
| Position protection | static bracket, no reassessment | immutable `PositionThesis`, recurrent evaluation, native stop/target amendments |
| Protective ACK telemetry | buffered ACK could be dropped when a fill arrived first | ACK is published before its resolution; no orphaned ACK |
| Protection audit | unbounded look-ahead batching could hide a cross-bar gap | batching bounded to one engine step (`ts_ns`) |
| Bracket identity | child orders never bound to the native position id | bound on entry fill and position callback |
| Replay cost | historical 95.83 s (pre-optimisation) | 14.8 s on the same 2,160-bar slice |

## New / changed modules

| Path | Role |
|---|---|
| `src/v8_next/experts/horizon_profiles.py` | 28/28 versioned evaluation contracts (primary, confirmations, warmup, staleness, habitats, missing behaviour) |
| `src/v8_next/domain/horizon_data.py` | causal H1→H4 aggregation; refuses to split a coarser tape into M15 |
| `src/v8_next/experts/evidence.py` | `produce_evidence_multi`; profile gate ordered before warmup/quality |
| `src/v8_next/domain/observation.py` | `ObservationView.extended` (provably equivalent incremental view), cached content digest and quality prefix |
| `src/v8_next/domain/horizons.py` | suffix scan for the latest admissible bar |
| `src/v8_next/economics/position_lifecycle.py` | versioned `StopPolicyV1` / `TargetPolicyV1`, thesis construction, reassessment |
| `src/v8_next/adapters/adaptive_strategy.py` | multi-horizon wiring, thesis lifecycle, native `modify_order` amendments, bracket binding, ACK pairing |
| `src/v8_next/adapters/protection_invariants.py` | `AMENDED` event kind (same order identity, updated price/quantity) |
| `src/v8_next/evaluation/sim_arm.py` | declared slice allowlist, verified M15 loading, bounded protection audit, horizon evidence |

## Verification performed

```
uv run --locked --extra dev ruff check <changed files>          -> clean
uv run --locked --extra dev mypy <changed src files>            -> Success: no issues (7 files)
uv run --locked --extra dev pytest -q \
  tests/test_sim_adaptive_arm.py tests/test_sim_protection_audit.py \
  tests/test_protection_invariants.py tests/test_multi_horizon.py \
  tests/test_pit_observation.py tests/test_expert_horizon_profiles.py \
  tests/test_horizon_data.py tests/test_position_lifecycle.py \
  tests/test_sim_multi_horizon.py                               -> pass
```

Measured replay (2,160 H1 bars, 240 real M15 bars per 60 H1 bars, one BTCUSDT perp, 10,000 USDT,
maker 0.0002 / taker 0.0005):

| Arm | Wall | Trades | Net PnL | Net R | Max DD | Hit | Protection violations |
|---|---:|---:|---:|---:|---:|---:|---:|
| V8 static H1 | 1.0 s | 44 | −21.234951 | −0.021638 | 1.998% | 34.09% | 0 |
| V9 legacy H1 + static bracket | 8.5 s | 24 | −23.102285 | −0.049252 | 1.798% | 33.33% | 0 |
| V9 multi-horizon + static bracket | 14.3 s | 18 | −13.880205 | −0.045609 | 0.779% | 33.33% | 0 |
| V9 multi-horizon + adaptive management | 14.8 s | 23 | **+20.162531** | **+0.052744** | **0.570%** | **47.83%** | **0** |

These are descriptive measurements on one development slice. They are **not** an edge claim, not a
generalisation, and not an OOS result.

## What is demonstrated, and what is not

Demonstrated mechanically on a real native replay:

- all three horizons produced stances in the same run (M15 327, H1 498, H4 51 asserted stances);
- 23 theses created; **17 native stop amendments** requested and acknowledged; 0 rejected;
- 0 protection violations across the full slice; every management reassessment saw `PROTECTED`;
- accounting identity preserved: baseline net PnL and fees are unchanged by these edits.

Not demonstrated on this tape:

- no target amendment and no thesis-driven exit occurred naturally; their mechanics are covered by
  deterministic unit tests, not by an economic observation;
- M15 is real vendor data but the decision cadence is still H1 (see `BLOCKERS.md`);
- no statistical significance (23 trades), no sealed OOS, no promotion.
