# Goose continuity predicate feasibility — partial

This experiment compares **one predicate**, `CausalFrame.continuous`, for every prefix of the 2,160 BTCUSDT hourly candles in the monograph's 2025-01-01–2025-04-01 development window. It is not a backtest, feature pipeline, engine comparison, or strategy-decision parity test. Only the three pinned monthly archives were read. Input and output digests and all samples are in `goose-samples.json`; archive, source, compiler and machine identity are in `environment.json`.

Before timing, the Goose prototype matched the production predicate on empty, single, contiguous, early-gap and late-gap synthetic fixtures. On the actual development timestamps, all 2,160 prefix results matched the production result and the NumPy result. This establishes predicate parity only for these fixtures; it does not validate candle/frame construction, point-in-time availability, indicators, opportunity/decision logic, order handling or funding.

On macOS ARM64, five repetitions gave these medians:

| Work | Median | Timing boundary |
|---|---:|---|
| Production Python predicate | 98.20 ms | First `continuous` access over prepared CausalFrame objects, including conversion of booleans to a result string; frame creation and validation excluded |
| Goose predicate | 0.601 ms | Naive prefix scans and result-buffer construction on already parsed timestamps |
| NumPy alternative | 0.084 ms | Vectorized equality and cumulative conjunction on already constructed arrays; conversion to result string excluded |
| Goose process | 9.49 ms | Fresh process, reading/parsing the timestamp CSV, computation and captured output; excludes generating the CSV |
| Goose input read and parse | 0.140 ms | Reported inside the Goose process |

The narrow hot-loop ratio is about 163× versus the Python predicate in this sample; the timer boundaries and representations differ, and the much faster NumPy result uses an incremental vectorized algorithm rather than the same repeated scans. No FFI was implemented. Neither process RSS measurement is a comparable allocation of the predicate: Python's process contains loaded libraries, Candle objects and prepared frames, while Goose's process holds just timestamp arrays. The code records whole-process high-water marks but **does not claim memory savings**. Compilation took 0.13 s in a repeated build with an already warm Goose compiler/runtime cache; it is not a fresh toolchain installation measurement. The five fresh Goose processes have a median 9.49 ms including startup, with individual values retained; they share warm filesystem caches.

The separate profiled development replay recorded 0.275 s inclusive in `continuous` in a 3.260 s profiled process. Illustratively, treating that instrumented fraction as removable and assuming perfect replacement yields an Amdahl ceiling of roughly 1.09× for that *profiled fixture*, before marshaling. Profiling overhead and inclusive-call accounting mean this is not a measured bound on the uninstrumented system. In particular it does not justify a research-engine rewrite. Incremental continuity in the existing implementation is a plausible candidate to investigate with full causal and gap parity; validation and other work remain.

Goose on the target Linux environment, native FFI, realistic historical portfolio inputs, full deterministic decision parity, and end-to-end timing remain **UNVERIFIED**. The evidence supports investigating an existing-language optimization first, but is insufficient for a migration decision. Reproduce with `reproduce.sh`; no binaries or market data are committed.
