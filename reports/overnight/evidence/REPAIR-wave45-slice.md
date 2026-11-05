# REPAIR wave 45 — shadow slice / audit verdict / soak / ablations

**Branch:** `fix/wave45-slice` · **Worktree:** `/Users/hootie/src/v9-worktrees/fixslice`
**Base commit:** `35e4a41` (`integration/wave1`)

Seven independently reproduced defects, each fixed with a test that fails on the base
commit and passes after. No behaviour outside the owned modules changed: the only
schema additions are `TraceStep.allocation_reason`, `ParityBlock.engine_parity`,
`HardRiskLimits.instrument_committed` / `sector_committed`, `SoakReport.status_reason`
and `ArmResult.arm_kind` / `preregistration_digest`.

## Defect → fix → test

### 1. P1 fabricated per-instrument exposure vetoes every entry

- **Defect:** `ShadowTrader._allocate` charged the account-wide `committed_exposure` to
  every candidate instrument and to the account sector. Two candidates made the maps sum
  to `2x committed` (`BLOCKED_RISK/INCONSISTENT_EXPOSURE_INPUT`); one candidate with
  `committed > 0` was vetoed as `BLOCKED_EXPOSURE/EXISTING_INSTRUMENT_EXPOSURE`.
- **Fix:** `_allocate` no longer derives an attribution. Existing commitment is passed
  through only from the risk plane's own exact split
  (`HardRiskLimits.instrument_committed` / `sector_committed`); otherwise both maps stay
  `None` and `allocate_challenger` fails closed with its honest
  `UNKNOWN_EXPOSURE_DETAIL`. The trace now records the challenger's
  `allocation_reason` so the reason is observable, not just the outcome.
- **Tests (`tests/test_shadow_trader_slice.py`):**
  - `test_committed_exposure_with_a_real_split_allocates_both_candidates` — two
    candidates, `committed=100` with a real split on a third instrument/sector both
    allocate (`ADMITTED`, `CAPACITY_AVAILABLE`).
  - `test_committed_exposure_without_a_split_fails_closed_as_unknown` — same tape
    without a split yields `BLOCKED_EXPOSURE` / `UNKNOWN_EXPOSURE_DETAIL` (not
    `INCONSISTENT_EXPOSURE_INPUT`, not `EXISTING_INSTRUMENT_EXPOSURE`).
  - `test_zero_committed_exposure_needs_no_attribution` — `committed = 0` behaviour is
    unchanged (`ADMITTED`).

### 2. P2 economic PASS with zero closed campaigns

- **Defect:** `audit/pipeline.py::_economic_section` returned `PASS`
  (`REALIZED_PNL_RECONCILED`) when `closed_rows` was empty, so an open-only run could
  render `AUDIT_COMPLETE`.
- **Fix:** an empty `closed_rows` returns `INCOMPLETE` with
  `ECONOMIC_NO_CLOSED_CAMPAIGN` (label `OPEN_ONLY_NO_CLOSED_CAMPAIGN`) before any
  reconciliation arithmetic, so a balanced ledger with nothing realized cannot pass.
- **Test:** `tests/test_audit_verdict_wiring.py::
  test_open_only_run_cannot_earn_an_economic_pass` — exactly the open-only run dir
  (mechanical/economics otherwise complete) renders `AUDIT_INCOMPLETE`.

### 3. P3 unidentifiable persisted fills silently dropped → label can be COMPLETE

- **Defect:** when fills were persisted but every row lacked identity,
  `_lineage_from_disk` returned `(None, None)`, which the report read as "no chain
  claimed" instead of "chain claimed and broken".
- **Fix:** a persisted fill/order/decision/cashflow row now makes the run a chain
  claimant. If no row survives the joins, `_lineage_from_disk` returns an empty-but-
  declared `LineageChain` with `as_of_ns=0`, so `certify` reports the structural gaps and
  the report is blocked. A `fills.jsonl` line with no fill/time identity at all
  (`_is_fill_row`) is placeholder noise, not a claim.
- **Test:** `tests/test_audit_verdict_wiring.py::
  test_fills_without_any_identity_declare_a_broken_chain` — the run's own dimensions may
  pass, but the declared chain is uncertifiable and the rendered label is
  `AUDIT_BLOCKED`, never `AUDIT_COMPLETE`.

### 4. P2 soak COMPLETE from a 1 ms target

- **Defect:** `_status_for` only checked `duration_actual >= duration_target`; the target
  was caller-supplied with no floor, so `--duration 0.001` reported `COMPLETE`.
- **Fix:** added `MIN_SOAK_TARGET_S = 24 * 3600`. A target below the floor is
  `SHORT_RUN` with `status_reason = TARGET_BELOW_QUALIFICATION_FLOOR:…`, regardless of
  the measured duration; at or above it, `duration_actual < duration_target` is
  `SHORT_RUN` with `ACTUAL_BELOW_TARGET:…`. `SoakReport` re-derives and enforces the
  reason, so editing `duration_target_s` (or the reason) cannot promote a run.
- **Tests (`tests/test_soak_fault_injection.py`):**
  `test_one_millisecond_target_cannot_qualify_as_a_soak`,
  `test_floor_survives_a_caller_editing_only_the_target`,
  `test_full_floor_target_with_two_seconds_elapsed_is_short_run`.

### 5. P2 ablation delta across different tapes/engines

- **Defect:** `compute_delta` gated only on `costed` + intervention label, so an arm
  measured on another tape/engine published a delta.
- **Fix:** `_require_comparable_provenance` refuses a delta unless both arms carry
  provenance and agree on `tape_sha256`, `engine`, `engine_version`,
  `execution_profile` and `execution_profile_digest`; the refusal names the mismatched
  fields.
- **Tests (`tests/test_ablations.py`):**
  `test_delta_refuses_arms_measured_on_different_tapes`,
  `test_delta_refuses_arms_measured_on_a_different_engine_or_profile`,
  `test_delta_accepts_arms_measured_on_one_basis`.

### 6. P3 vacuous `SHADOW_UNMUTATED` label

- **Defect:** `_parity` copied the policy-off capture's native digests into a fake
  `POLICY_SHADOW` signature and ran `assert_shadow_unmutated` against it — an equality
  that could never fail, presented as parity evidence.
- **Fix:** the fabricated shadow signature is removed. An adaptive run reports
  `verification="NO_EXECUTION_SURFACE"` (with the structurally empty
  `native_side_effects` and the AST-pinned absence of an order path) and pins
  `engine_parity="NOT_RUN"`. The two policy-off captures are still compared as a
  determinism check of the control replay, and the block's docstring says exactly that.
- **Tests:** `test_trace_reports_no_native_side_effects`,
  `test_control_capture_is_deterministic_and_no_engine_parity_is_claimed`,
  `test_disabled_adaptive_layer_produces_no_decision_and_no_native_drift`.
- **Evidence:** `reports/overnight/evidence/M3-07.md` now states engine-level parity is
  `NOT_RUN` and withdraws the `assert_shadow_unmutated` claim.

### 7. Defect 4 of the prior audit — no producer for the costed result

- **Defect:** nothing wrote `reports/evaluation/costed_result.json`, the artifact the
  economic verdict dimension reads.
- **Fix:** `publish_costed_result(arm, *, costs, net_return, verdict, …)` in
  `evaluation/ablations.py` writes the shared contract
  (`preregistration_digest`, `dataset_role`, `costs`, `net_return`, `verdict`, optional
  `uncertainty`). It refuses, with a typed `CostedResultError` and before writing
  anything, unless: the preregistration file exists and parses; its intervention kind is
  `V9_INTERVENTION`; the arm is costed, declares `arm_kind="V9_INTERVENTION"`, is the
  preregistered `arm_id`, and records the sha256 of the preregistration on disk; the
  declared costs equal the registered fee/funding/slippage vector; the dataset role is
  present; `net_return` is a finite decimal; and the verdict is one of
  `PASS/NO_EDGE/INCONCLUSIVE/STOP/INVALID`. The V8 baseline arm is refused by kind.
- **Tests (`tests/test_ablations.py`):**
  `test_publish_refuses_an_absent_or_uncosted_arm_and_writes_nothing`,
  `test_publish_refuses_the_v8_baseline_arm`,
  `test_publish_refuses_a_digest_or_cost_mismatch`,
  `test_publish_writes_a_digest_bound_result_the_verdict_accepts` — the last asserts the
  written file is accepted by `evaluation.verdict._economic` (`DimensionStatus.PASS`)
  with the file's digest bound to the preregistration.

## Verification

```
/Users/hootie/src/v9/.venv/bin/python -m pytest -q \
    tests/test_shadow_trader_slice.py tests/test_audit_verdict_wiring.py \
    tests/test_soak_fault_injection.py tests/test_ablations.py
# 88 passed

per file: shadow 28, audit 16, soak 20, ablations 24

/Users/hootie/src/v9/.venv/bin/python -m ruff check <8 touched files>
# All checks passed!

/Users/hootie/src/v9/.venv/bin/python -m mypy --strict \
    src/v8_next/app/shadow_trader.py src/v8_next/audit/pipeline.py \
    src/v8_next/factory/soak.py src/v8_next/evaluation/ablations.py
# Success: no issues found in 4 source files
```

**Base-commit failure evidence.** Running the same tests with the four source modules
reverted to `35e4a41` reproduces the defects: the soak/ablation tests fail at import
(`MIN_SOAK_TARGET_S`, `V9_INTERVENTION_KIND`, `publish_costed_result` absent), the shadow
fixtures cannot express attribution (`HardRiskLimits` extra inputs), and in particular:

```
test_open_only_run_cannot_earn_an_economic_pass:
  assert <VerdictState.PASS> is <VerdictState.INCOMPLETE>   # economic PASS on an open-only run
test_fills_without_any_identity_declare_a_broken_chain:
  assert None == LineageChain(...)                          # broken claim read as "no chain"
```

## Residual risks

- **Attribution source.** The slice now trusts the risk plane's declared
  `instrument_committed`/`sector_committed` splits. Whether the risk plane's split is
  itself truthful is outside this slice; the challenger still requires the maps to sum
  to `committed` and rejects any other shape.
- **Engine parity** remains `NOT_RUN`; no `NativeSession` is injected, so the slice makes
  no shadow-vs-native policy claim.
- **Soak duration.** The floor closes the 1 ms qualification hole; the harness still
  cannot produce a real 24–48 h wall-clock soak in this environment (`NOT_RUN` in
  M4-05).
- **Costed result** is a producer, not evidence: it refuses unless the caller supplies a
  costed, digest-bound preregistered V9 arm; publishing it for the V8 baseline or the
  random-entry control is impossible by construction.
