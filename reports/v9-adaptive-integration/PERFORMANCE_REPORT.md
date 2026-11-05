# Performance evidence

On the H1 tape with SHA-256 `84978f2d53a70afb769f760380b1587f073edfdc07d628c3a3be6773514673d7`, a single sequential process ran the command `uv run --locked --extra dev python /tmp/v9_adaptive_measure.py` against the integration checkout at `fb93f26`/`5412cad` plus independent unused horizon profiles `4ef4b7f`. The reproducible script is `tools/measure_adaptive_integration.py`. Configuration: `SimArmConfig(enabled=True)`, 10,000 USDT starting balance, default baseline execution, 0.0002 maker/0.0005 taker, full 2,160 bars. Engine: locked NautilusTrader 2.0.0rc4.

| Arm | Wall s | CPU s | Process peak RSS bytes |
| --- | ---: | ---: | ---: |
| V8 | 15.754 | 2.224 | 245235712 |
| V9 | 13.828 | 14.159 | 261586944 |

RSS is cumulative peak of the shared process, not an isolated per-arm peak. The wall/CPU divergence of the V8 run means this single observation is unsuitable for a speed ratio; contention and startup were not controlled. A 4 GiB constrained run and before/after parity profile were not performed. The checked-in earlier report records 95.827 s before and 13.036 s after its optimization; that is historical, not a newly replicated before/after benchmark. No new performance improvement is claimed.
