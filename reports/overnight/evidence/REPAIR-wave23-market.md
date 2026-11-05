# REPAIR wave23 — market / belief / funding / evidence / resource baseline

Branch `fix/wave23-market`, worktree `/Users/hootie/src/v9-worktrees/fixmarket`,
base `integration/wave1` @ `fda4580`.

Six defects reproduced by the independent audit. Each has a test that **fails on the base
commit and passes with the fix**; the two doc-only corrections have no executable test.

## 1. P1 — hysteresis streak counted ticks, not evidence

* **Defect.** `belief/update.py` put `decision_time_ns` inside `_dependency_fingerprint`, so two
  `advance` calls over the *same* observation set one nanosecond apart produced `INPUT_CHANGED`,
  incremented `candidate_streak`, and minted a `TransitionEvidence` with no new bar.
* **Fix.** The dependency fingerprint no longer carries the decision clock. `_resolve_regime` also
  gates both the streak increment and acceptance on
  `inputs.observation_ids != previous.input_observation_ids`; observation ids are content
  identities (payload and availability included), so a genuine new bar advances the streak and a
  clock-only call cannot. Staleness/decay paths are untouched (`STALENESS_CROSSED` still fires from
  `_aged`).
* **Tests (`tests/test_belief_updates.py`).**
  `test_clock_only_advance_cannot_advance_the_candidate_streak`
  — FAILS before (`update_trigger` returned `INPUT_CHANGED` at `cut + 1 ns`), PASSES after; it also
  proves a genuine new bar mints the transition.

## 2. P2 — future funding boundary folded into an as-of equity and reported PASS

* **Defect.** `audit/funding_boundary.py` attributed a settlement with
  `boundary_time_ns > as_of_ns` using a post-cut mark and added its amount to `funding_total` /
  `expected_final_equity`; with matching amount the report said PASS.
* **Fix.** A settlement beyond the cut is typed `FUNDING_BOUNDARY_IN_FUTURE`, skipped before any
  boundary claim/quantity/mark read, and excluded from `funding_total` and the equity identity. The
  issue is not a hard mismatch, so the report is `BLOCKED` (never PASS). Module docstring records
  the rule.
* **Test (`tests/test_funding_boundary_reconciliation.py`).**
  `test_settlement_boundary_after_the_as_of_cut_is_excluded_and_blocked`
  — audit case boundary 400 / as_of 300; FAILS before (PASS, `funding_total = -5.3`), PASSES after
  (`BLOCKED`, issue code, no attribution, `funding_total = 0`, equity `102000`).

## 3. P2 — evidence provenance depended on cache history

* **Defect.** `experts/evidence.py` hashed `cache.digest_for(frame)`, an append-only chain, so the
  same frame produced different provenance from a fresh cache vs a cache extended one bar, even
  though stances and all 28 `evidence_id`s were identical.
* **Fix.** Provenance now carries `frame_digest = canonical_digest("expert-evidence-frame", …)` over
  `instrument_id`, `decision_ns`, bar count, the last bar's `start_ns`/`end_ns`/`close`/
  `source_hash`, and the usable observation ids (content identities), together with
  `cache.version`. The path-dependent chain is never read.
* **Test (`tests/test_expert_evidence.py`).**
  `test_provenance_depends_on_frame_content_not_cache_history`
  — FAILS before (provenance digests differ across seeds), PASSES after; asserts the cache actually
  took the extension path (`extensions == 1`, `reseeds == 0`) while stances, 28 ids and provenance
  agree.

## 4. P2 — feature-value parity dropped half of two specs

* **Defect.** `evaluation/resource_baseline.py::_current_reading` collapsed every tuple to
  `value[-1]`, so the parity digest covered only `ema20` of `trend_emas` and only the low pivot of
  `significant_swings`.
* **Fix.** `_current_reading(spec, value)` collapses only true series kernels
  (`_SERIES_KERNELS = {"wilder_rsi"}`); pair-valued kernels keep both components, scalars are
  unchanged.
* **Test (`tests/test_resource_baseline.py`).**
  `test_pair_valued_kernels_bind_both_components_in_the_digest`
  — FAILS before (mutating the first component left the digest unchanged; signature/TypeError on
  base), PASSES after; also proves the second component and the RSI series collapse.

## 5. P2 — funding parity stream was tautological

* **Defect.** `build_receipt` set `funding=funding_digest(tape_path)`. Baseline and candidate read
  the same tape, so the digests matched unconditionally, the stream could never FAIL, and it was
  counted PASS-eligible.
* **Fix.** `build_receipt` records `funding=None`; `parity_gate` already maps a missing digest to
  `BLOCKED`. The dead `funding_digest` helper and its deterministic-tape test were removed rather
  than left as a misleading public API. Receipt note now says funding is NOT_RUN.
* **Test (`tests/test_resource_baseline.py`).**
  `test_real_measurement_parity_and_blocked_gate` (slow, real tape) — updated from the stale
  `funding PASS` pin to `funding is None` on both receipts and `BLOCKED` in the gate, with
  `DIGEST_MISSING:funding:baseline+candidate` in the blockers.

## 6. P2 — M0-08 evidence overstated elimination

* **Defect.** `reports/overnight/evidence/M0-08.md` claimed full-prefix recomputation was eliminated
  based on cache `extensions/reseeds` counters, which only prove the cache's own append-only prefix.
* **Fix.** §3.2 now states exactly four engine-backed kernels became incremental (`wilder_rsi`,
  `trend_emas`, `significant_swings`, `macd_line`, 1.34× overall), that `ichimoku.py`,
  `bollinger.py`, `patterns.py`, `reclaim.py` still scan the full prefix every bar, and that the
  replay is superlinear (0.194 / 0.344 / 0.818 s at 540 / 1080 / 2160 bars). The acceptance bullet
  "eliminates full-prefix recomputation" is recorded as only partially met. §3.3 corrects the
  funding stream to BLOCKED and explains the tautology. §1 and §6 record PR #1
  acceptance/amendment as **NOT DECIDED** because its harness was not re-run. §5 warns that the
  committed JSON receipts predate this correction.

## Verification

```
cd /Users/hootie/src/v9-worktrees/fixmarket
# per-file
test_belief_updates.py                     10 passed
test_funding_boundary_reconciliation.py    16 passed
test_expert_evidence.py                    17 passed, 1 deselected (slow)
test_resource_baseline.py                  22 passed, 3 deselected (slow)
# combined
65 passed, 4 deselected in 0.50s

ruff check (4 modules + 4 tests)           All checks passed!
mypy --strict (4 modules)                  Success: no issues found in 4 source files
```

Slow real-tape runs with `V8_TAPE_ROOT=/Users/hootie/src/v9-worktrees/fixmarket/research/tape`:

* `tests/test_resource_baseline.py -m slow` — 3 passed, 22 deselected in 6.88 s.
* `tests/test_expert_evidence.py -m slow` — 1 passed, 17 deselected in 0.16 s.
* Decision parity digest unchanged: `8ecef5f05713f19ea9195f8ed9e03d407c5fee5bdb730db993afa840d5ac9b16`
  (baseline = candidate over 2,160 bars / 60,480 stances).
* Corrected feature-value parity digest:
  `0223d1e993e398ee180518a7525dcb69667bf1780289d92d63d4c8f63792af45`
  (direct = cached, 21,210 reads). The pre-fix digest `abf8b0c0…` is superseded because it omitted
  `ema5` and the high pivot.

## Residual risks

* Committed receipts under `reports/overnight/evidence/receipts/` still carry the pre-fix funding
  digest and the superseded `values_digest`; they are measurements from code SHA `e0871451…` and
  must be regenerated before being cited. `M1-06.md` also quotes the superseded values digest.
* The "four incremental kernels" claim is reproduced from the audit; this repair did not add
  instrumentation for the per-expert full-prefix scans.
* The belief dependency-fingerprint format changed. A persisted belief from the old format triggers
  one `INPUT_CHANGED` recompute on first contact; no schema version was bumped because the stored
  fingerprint is opaque and no other invariant depends on its pre-image.
