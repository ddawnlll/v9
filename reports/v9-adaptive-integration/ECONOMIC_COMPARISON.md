# Economic comparison

Historical development-only H1 replay was reproduced with `uv run --locked --extra dev python /tmp/v9_adaptive_measure.py`; source tape SHA-256 `84978f2d53a70afb769f760380b1587f073edfdc07d628c3a3be6773514673d7` and configuration are in PERFORMANCE_REPORT.md. These figures are native account **balance changes**, not verified marked-equity returns. Each arm had one open position at cutoff.

| Arm | Closed trades | Balance delta USDT | Mean net R | Fees USDT | Reported position funding adjustments USDT |
| --- | ---: | ---: | ---: | ---: | ---: |
| V8 static H1 | 44 | -21.23495118 | -0.021638104017588564 | 37.91535683 | 0.36718638 |
| V9 H1 static brackets | 24 | -23.10228485 | -0.04925155935670389 | 20.91868621 | 0.36718638 |

The V9 arm is **NOT_QUALIFIED** due to protection evidence; its 24 trades are below the 30-trade floor. No edge or valid primary marked-equity comparison is claimed. Multi-horizon and adaptive-management arms are NOT_RUN; a matched random-entry experiment, dependence-aware uncertainty, independent regime attribution, and slippage/mark reconciliation are NOT_RUN. Existing H1 tape cannot establish M15 behavior. No synthetic tape is treated as market profitability.
