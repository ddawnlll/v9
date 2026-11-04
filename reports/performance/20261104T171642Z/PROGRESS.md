# Preliminary mechanics investigation — INCOMPLETE

This is not a completed performance report. No historical tape was found at the expected repository path, and this run is on macOS Apple M3 Pro, not the target 2-vCPU Linux VPS. B0–B5, cgroup acceptance, real execution, full wall accounting and Goose parity remain unmeasured. RFC 1.4.1 was not found in the checkout; the normative monograph identifies itself as RFC 1.0 plus Appendix A. No economic conclusions follow from this fixture.

The production `run_expert_strategy_backtest` path was run with flat synthetic hourly candles, default strategy configuration, zero fills and no funding. All 28 registered observers are called for each bar; this quiet fixture does not exercise representative feature, order or fill work. Three fresh processes for each size reported the following pre-output times and process peak RSS (median, min–max). Samples were run on a shared host; this is not a CPU-isolated VPS measurement. These runs preceded the later addition of optional tape arguments to the harness; the default strategy and synthetic data were the same.

| Bars | Pre-output seconds median (range) | Peak RSS median MiB (range) |
|---:|---:|---:|
| 720 | 0.449 (0.429–0.453) | 145.3 (145.3–145.4) |
| 1440 | 0.813 (0.750–0.833) | 154.9 (149.7–155.1) |
| 2880 | 1.486 (1.423–1.510) | 173.1 (172.9–173.3) |

These are internal timestamps, excluding interpreter startup before the script's first timer, final JSON write, and teardown; they do not meet the requested end-to-end accounting. ru_maxrss is a process high-water mark, not a stage allocation measurement. Raw small JSON summaries accompany this note.

One separately profiled 2880-bar run measured 1.948 s in the backtest versus 1.305 s median uninstrumented (about 49% overhead, approximate because separate runs and shared host). In that profile `BacktestEngine.run` was 1.742 s inclusive and `ExpertEnsembleStrategy.on_bar` was 1.727 s inclusive. Native `run` includes callbacks; subtracting them does not reliably measure native CPU or native internals. `CausalFrame.from_ordered_prefix` was 0.576 s inclusive and `continuous` 0.430 s inclusive; `observe_all_28` was 0.541 s inclusive. These nested entries overlap other entries and are not additive. Full pstats summary is in profile-top.txt; native stacks are not resolved by cProfile.

The code constructs `tuple(self.candles)` on each bar, validates the full prefix in `domain/market.py:147`, and recomputes continuity over the full prefix in `domain/market.py:101`. This gives a concrete quadratic-work hypothesis; the three workload sizes are insufficient to isolate it from startup, data shape and other costs. Incremental validation and continuity state are promising candidates only if causal availability, gaps, reordering, duplicates and fallback behavior remain identical. Their expected speedup and memory effect are not yet measured.

Call graph (single-instrument path): tape loader or synthetic Candle construction → `run_expert_strategy_backtest` → native instrument and Bar conversion → engine and venue initialization → `add_data` → strategy initialization and prefix DataFrame → `engine.run` → `on_bar` → OpportunityBook transition → CausalFrame → opportunity detection → `observe_all_28` → consensus/risk/order handling → callbacks → economic state, native reports and execution telemetry → `dispose` → harness summary. `run_portfolio_backtest` adds per-leg bars, sleeves and optional funding events; it was not measured here. The proposed MarketBelief and continuous Trader Decision Policy in the monograph were not established as implemented or measured by this run.

Next: instrument non-overlapping process phases and per-expert calls; benchmark active order/fill/funding paths and historical tape with provenance; repeat under 2-vCPU 4-GiB Linux with thread limits; run the workload matrix; investigate Goose only with deterministic equivalent-output fixtures. `INSUFFICIENT_EVIDENCE` is the only supported architecture verdict at this stage.
