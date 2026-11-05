# Performance report

Same hardware, same tape (`btcusdt-1h-2025Q1`, 2,160 bars), same prices/fees/capital. Wall time is
`time.perf_counter()` around the full runner call; RSS is process peak from `getrusage`.

## Before / after

| Arm | Historical report | Current HEAD | Change |
|---|---:|---:|---:|
| V8 static H1 | 0.48 s | 1.0 s | +0.5 s (measurement includes tape load/verify; the engine portion is ~0.7 s) |
| V9 adaptive | **95.83 s** | **14.8 s** (multi-horizon + management) | **6.5× faster** |
| V9 legacy H1 path | 95.83 s | 8.5 s | 11.3× faster |

The historical 95.83 s figure is from `reports/provenance/P1-SIM-ARM.md` at base `15379fb`. The
current V9 arm does strictly more work (three horizons, thesis lifecycle, native amendments), so the
comparison is conservative.

## Where the time went (1,000-bar profile, then fixes)

| Stage | Wall @ 1,000 bars |
|---|---:|
| First multi-horizon implementation | 25.75 s |
| Incremental per-horizon `ObservationView` | 14.26 s |
| Stable numeric frame cache + cached view digest + quality prefix | **6.13 s** |
| Full 2,160-bar run (final) | **14.8 s** |

Hot spots removed, in order of impact:

1. **Quadratic view rebuilds** (`domain/observation.py`): `build_view` re-hashed and re-bucketed the
   whole observation history every bar. `ObservationView.extended` appends only new observations and
   proves the fast path is equivalent (strictly increasing keys, no prior rejections, cut does not
   move backwards). It falls back to a full rebuild on any doubt.
2. **Quadratic feature digests** (`experts/cache.py` path): each horizon now keeps one stable numeric
   DataFrame, so `FeatureCache._sync` chains its digest over the new rows instead of re-reading the
   prefix.
3. **Full-history provenance digests** (`experts/evidence.py`): the frame/context digests now use the
   view's rolling `content_digest` (O(1)) instead of sorting every observation id per bar.
4. **`quality_at` scans**: precomputed cumulative-worst prefix with `bisect_right`.
5. **`assemble_context` scans**: suffix scan for the latest admissible bar instead of filtering the
   whole history.
6. **H4 allocation churn**: `H4` is rebuilt only when a 4-bar bucket completes.

No decision semantics were changed to obtain this speedup. Economics are bit-identical across
repeated runs (same `decisions_digest`), and the V8 baseline is unchanged (`−21.23495118`).

## Resource profile

| Arm | Peak RSS (process) |
|---|---:|
| V8 static H1 | 217 MiB |
| V9 legacy H1 | 237 MiB |
| V9 multi-horizon, static bracket | 298 MiB |
| V9 multi-horizon + management | 316 MiB |

This is well inside the 4 GiB development target, but it is not a cgroup-capped measurement. A
4 GiB-limited soak remains `NOT_RUN`.

## Reproduce

```
uv run --locked --extra dev python - <<'PY'
from pathlib import Path
from v8_next.adapters.adaptive_strategy import SimArmConfig
from v8_next.evaluation.sim_arm import run_sim_arm
m = run_sim_arm(
    tape=Path("research/tape/btcusdt-1h-2025Q1"),
    config=SimArmConfig(enabled=True),
    intraday_tape=Path("research/tape/btcusdt-15m-2025Q1"),
)
print(m.trades, m.net_pnl, m.fees if hasattr(m, "fees") else m.fee_total)
PY
```

Machine: Apple Silicon macOS, Python 3.12.12, NautilusTrader 2.0.0rc4, single process.
