# Performance evidence

On the H1 tape with SHA-256 `84978f2d53a70afb769f760380b1587f073edfdc07d628c3a3be6773514673d7`, a single sequential process ran the command `uv run --locked --extra dev python /tmp/v9_adaptive_measure.py` against the integration checkout at `fb93f26`/`5412cad` plus independent unused horizon profiles `4ef4b7f`. The reproducible script is `tools/measure_adaptive_integration.py`. Configuration: `SimArmConfig(enabled=True)`, 10,000 USDT starting balance, default baseline execution, 0.0002 maker/0.0005 taker, full 2,160 bars. Engine: locked NautilusTrader 2.0.0rc4.

| Arm | Wall s | CPU s | Process peak RSS bytes |
| --- | ---: | ---: | ---: |
| V8 | 15.754 | 2.224 | 245235712 |
| V9 | 13.828 | 14.159 | 261586944 |

RSS is cumulative peak of the shared process, not an isolated per-arm peak. The wall/CPU divergence of the V8 run means this single observation is unsuitable for a speed ratio; contention and startup were not controlled. A 4 GiB constrained run and before/after parity profile were not performed. The checked-in earlier report records 95.827 s before and 13.036 s after its optimization; that is historical, not a newly replicated before/after benchmark. No new performance improvement is claimed.

A later sequential full replay at `27fe512` measured V8 1.216 s wall / 1.215 s CPU / process peak RSS 229277696 bytes, and V9 12.362 s wall / 12.743 s CPU / cumulative peak RSS 250298368 bytes. Same command/configuration/tape as above. This is an additional observation, not an isolated before/after optimization or a 4 GiB qualification. V9 decision digest changed after correcting position opportunity identity; order digest stayed the same.

`uv run --locked --extra dev python -m cProfile -o /tmp/v9-adaptive-500.prof /tmp/v9_native_trace.py` profiled a 500-bar native replay after `d290589` (instrumentation enabled). `pstats` reported 3.090 s cumulative for `run_sim_arm`, 2.670 s native engine run, and 2.641 s for 500 `on_bar` callbacks. Within callbacks, `produce_evidence` was 1.209 s, `_context_rows` 0.677 s, and `build_view` 0.533 s cumulative (overlapping call trees; not additive). cProfile changes wall time, and this is only a hotspot diagnosis, not a parity or speedup claim. The first script creates the 500-bar measurement and raw trace; the full-slice reproducible measurement script remains `tools/measure_adaptive_integration.py`.
