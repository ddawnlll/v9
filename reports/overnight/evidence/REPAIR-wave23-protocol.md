# REPAIR wave23 — protocol, preregistration and factory audit findings

Branch `fix/wave23-protocol`, worktree `/Users/hootie/src/v9-worktrees/fixproto`, base
`fda4580` (`integration/wave1`). Fix commit `26ad3283b7828aa9ff82c905f502cc59f6351a28`
(evidence/report commit follows).

Every defect below was reproduced against the base commit before the fix. The reproduction
is a standalone probe (`/tmp/wave23_probe.py`, not committed) that exercises the same public
API the tests use; base output vs fixed output is quoted per defect. Each fix carries a test
that fails on base and passes after.

## Reproduction summary (base → fixed)

| # | Probe | Base `fda4580` | Fixed `26ad328` |
|---|---|---|---|
| 1 | register alpha 0.05, test at 0.20, p=0.12 | `PASS PASS_WITH_REGISTERED_PREREGISTRATION`, `primary_edge_eligible=True` | `refused ALPHA_MISMATCH` |
| 2 | `multiple_testing(..., method="HOLM")`, 5 hypotheses, p=0.03 | `corrected=True method=HOLM adjusted=0.03 rejected=True` | `refused CORRECTION_METHOD_NOT_DECLARED` |
| 3 | post-hoc gates (min_trades 100→3, max_dd 0.20→0.50) + 1 trade + 0.99 drawdown | `PASS` (no result input exists) | `refused POST_HOC_GATE_CHANGE`; registered gates + breached observations → `INVALID GATE_NOT_MET` |
| 4 | sealed token issued at 1.700e18 ns vs registration created 1.760e18 ns | `STOP ACCEPTED_NEGATIVE_OUTCOME` | `refused POST_HOC_PREREGISTRATION` |
| 4b | sealed read with no `issued_ns` | accepted (`issued_ns=0`) | `refused SEALED_READ_WITHOUT_ISSUE_TIME` |
| 5 | `from_entries` with a hand-written sealed row | rehydrated, `token_id="forged"` | `refused UNVERIFIED_AUTHORIZATION` / `TOKEN_DIGEST_MISMATCH` |

## Defect → fix → test

### 1. P1 `assess_verdict` alpha bypass (protocol.py)

- **Defect.** `assess_verdict` never compared `testing.alpha` with the registered
  `hypothesis_budget.alpha`, so a test run at alpha 0.20 against a registration that fixed
  0.05 returned a sealed `PASS` with `primary_edge_eligible=True`.
- **Fix.** The PASS path compares `Decimal(str(testing.alpha))` with the registered alpha and
  raises `VerdictRefused(code="ALPHA_MISMATCH", field="alpha")` on any difference. Decimal
  comparison is numeric, so `0.20` and `Decimal("0.20")` agree while `0.05` vs `0.20` does not.
- **Test.** `tests/test_research_protocol.py::test_a_wide_alpha_test_is_refused_against_the_registered_alpha`
  — asserts the wide report *does* reject p=0.12 (the bypass exists) and that the gate refuses it.

### 2. P1 `multiple_testing` string-method bypass (protocol.py)

- **Defect.** `method is CorrectionMethod.HOLM` is an identity test; the plain string `"HOLM"`
  compares equal but is not the member, so no branch ran and the raw p-values were returned
  while `corrected=True` and pydantic coerced `method` back to `HOLM`. In a family of 5,
  p=0.03 was reported corrected and rejected.
- **Fix.** `method` is validated as `CorrectionMethod | None` up front
  (`CORRECTION_METHOD_NOT_DECLARED`), the `None` case is explicit, an unimplemented member
  raises `CORRECTION_METHOD_UNIMPLEMENTED` instead of falling back to raw values, and the
  final `else` can no longer return unadjusted p-values as corrected.
- **Test.** `test_a_plain_string_correction_method_is_refused` — asserts the refusal code, and
  that the member really adjusts (`adjusted["H-001"] == 0.15`, `rejected=False`).

### 3. P1 PASS path ignored the gate vector (protocol.py, preregistration.py)

- **Defect.** `assess_verdict` accepted no `ExperimentResult`, never called
  `compare_against_result`/`gates()`, and `ExperimentResult.observed_trades`,
  `observed_drawdown` and `verdict` were dead. A post-hoc loosening of `minimum_trades`
  (100→3) and `max_drawdown` (0.20→0.50) still produced `PASS`.
- **Fix.** `experiment: ExperimentResult | None` is a required input of the PASS path:
  absent → `VerdictRefused(EXPERIMENT_RESULT_REQUIRED)`; present →
  `preregistration.compare_against_result(experiment)` (raises `PostHocGateChange` naming every
  differing field), the result's `verdict` must equal the claim (`RESULT_VERDICT_MISMATCH`),
  `observed_trades` must meet `minimum_trades` and `observed_drawdown` must not exceed
  `max_drawdown` (`INVALID GATE_NOT_MET`, reasons naming the gate). `VerdictResult` now records
  `experiment_digest`, so the PASS names the result it was assessed on.
- **Tests.** `test_pass_without_an_experiment_result_is_refused`,
  `test_post_hoc_gate_change_is_refused_even_with_a_significant_test` (asserts
  `minimum_trades` and `max_drawdown` are both named),
  `test_an_unmet_observed_gate_invalidates_the_claim` (1 trade; 0.99 drawdown),
  `test_the_experiment_result_must_report_the_claimed_verdict`,
  `test_pass_records_the_digest_of_the_result_it_was_assessed_on`.

### 4. P1 no preregistration-before-read ordering (protocol.py)

- **Defect.** `created_ns` and `AccessToken.issued_ns` were never compared, and `issued_ns`
  defaulted to 0, so a sealed read carried no time at all and a read predating the
  registration was accepted.
- **Fix.** `AccessLedger.request_access` now takes `issued_ns: int | None = None`; a
  `SEALED_OOS` read without it is refused (`SEALED_READ_WITHOUT_ISSUE_TIME`) after the
  authorization checks, while open roles still default to 0. `assess_verdict` refuses a sealed
  token issued before `preregistration.created_ns` (`POST_HOC_PREREGISTRATION`, field
  `issued_ns`).
- **Tests.** `test_a_sealed_read_issued_before_the_registration_is_refused`,
  `test_a_sealed_read_without_an_issue_time_is_refused`, `test_an_open_read_needs_no_issue_time`.

### 5. P2 forged/rehydrated ledger authority (protocol.py)

- **Defect.** `from_entries` copied rows verbatim: `token_id` was never re-derived and any
  `authorization_id` was accepted, so a hand-written sealed row rehydrated and conferred
  `primary_edge_eligible`.
- **Fix.** `AccessToken.mint` is now the single content-addressed derivation, used by
  `request_access` and by `from_entries`, which refuses a row whose `token_id` does not
  re-derive (`TOKEN_DIGEST_MISMATCH`). `from_entries(entries, authorizations=...)` requires
  every sealed row to name an authorization the caller supplies as a minted
  `SealedAuthorization` (a bare id raises `TypeError`); an unmatched id is
  `UNVERIFIED_AUTHORIZATION` — a content digest alone is not authority.
- **Tests.** `test_persisted_ledger_refuses_a_hand_written_token_id`,
  `test_persisted_ledger_refuses_a_sealed_row_without_a_minted_authorization`,
  `test_rehydration_accepts_a_ledger_whose_authorizations_are_supplied`, and the existing
  reuse test now builds a *well-formed* duplicate so it still exercises `SealedReuseViolation`.

### 6. P2 stale worker could block a newer live lease (factory/queue.py, factory/worker.py)

- **Defect.** `BoundedWorker._terminate` called `queue.block(issue_id)` — an owner-escalation
  door with no lease identity — so a worker resumed after expiry clobbered the issue a newer
  worker legitimately owned.
- **Fix.** `IssueQueue.block_lease(lease, reason)` validates the stored row with
  `_require_live` (state `/` token `/` worker `/` attempt) inside the write transaction,
  updates only `WHERE state='LEASED' AND token=?`, and closes the lease event. The worker's
  BLOCKED terminal uses it. `block()` stays as the explicitly lease-free owner door.
- **Test.** `test_a_stale_worker_cannot_block_the_new_owner` — W1 expires, W2 claims, W1's
  `block_lease` and W1's whole `run_lease` both raise `LeaseMismatchError`, and the snapshot
  still shows W2's token `LEASED`.

### 7. P2 declared capabilities unvalidated (factory/worker.py)

- **Defect.** `_authorize` used `cap in FORBIDDEN_CAPABILITIES` and `cap.value`. A declared
  `frozenset({"sealed_oos_access"})` (not the member value) matched nothing and reached
  `PUBLISHED_DRAFT`; `frozenset({"MAIN_MERGE"})` matched, then `cap.value` raised
  `AttributeError`, which escaped `_drive` and left the lease held.
- **Fix.** `_declared_requests` requires every declared request to be a `Capability` member
  (and rejects a bare string as the container), validated for every step in `__init__` and
  re-validated per phase at drive time. `run_lease` catches an unexpected `Exception` from
  `_drive`, records a `BLOCKED` terminal (`DRIVER_FAILED:<Type>: <msg>`) and re-raises, so no
  lease is stranded; `BaseException` (e.g. `KeyboardInterrupt`) still propagates untouched, as
  the forced-restart contract requires.
- **Tests.** `test_a_declared_request_that_is_not_a_capability_fails_at_construction` (both
  spellings, `TypeError`, nothing claimed), `test_a_shape_shifting_declaration_blocks_and_releases_the_lease`
  (honest at construction, look-alike when the phase runs: `BLOCKED` recorded, `TypeError`
  propagates, nothing claimable afterwards).

### 8. P2 `verified_findings` counted draft publishes (factory/memory.py, factory/worker.py)

- **Defect.** The aggregate filtered `outcome=VERIFIED` with no `kind=FINDING`, and the worker
  mapped `PUBLISHED_DRAFT` to `VERIFIED`, so the campaign test reported
  `verified_findings (2) > findings (1)`.
- **Fix.** `verified_findings` counts `kind='FINDING' AND outcome='VERIFIED'`; a draft publish
  maps to the new `FindingOutcome.PENDING_REVIEW`; `completed_issues` counts distinct
  `issue_id` over `OUTCOME` rows whose outcome is in `COMPLETION_OUTCOMES`
  (`VERIFIED`, `PENDING_REVIEW`); `pending_review` is its own reported count; and the aggregate
  raises if `verified_findings > findings` (a query that could only break by a future edit
  reintroducing the join).
- **Tests.** `test_efficiency_counts_a_campaign` (now `verified_findings == 1 == findings`),
  `test_verified_findings_never_exceeds_recorded_findings` (2 OUTCOME-VERIFIED/PENDING_REVIEW
  rows + 1 FINDING → `verified_findings=0`, `findings=1`, `completed_issues=2`,
  `pending_review=1`), `test_a_draft_publish_is_recorded_as_pending_review`, and the worker
  tests now assert `PENDING_REVIEW` plus `verified_findings == 0 <= findings`.

### 9. P2 missing simple-control-policy arm (evaluation/preregistration.py)

- **Defect.** `ArmKind.SIMPLE_CONTROL_POLICY` was declared but never required, so an
  intervention could be compared only with the V8 baseline and random entry — never with a
  real non-V9 policy.
- **Fix.** `Preregistration.validate()` requires it alongside the V8 baseline and the
  random-entry arm.
- **Test.** `test_validation_rejects_control_arms_without_a_simple_policy` (negative: the
  omission is named on `control_arms`; positive: the standard registration still validates).
  Test helpers in `test_research_protocol.py` and `test_preregistration.py` now declare the arm.

### 10. P2 F-02 evidence receipt fabricated (reports/overnight/evidence/F-02.md)

- **Defect.** The receipt passed `budget_spent=28, budget_limit=28` — the pytest *test count* —
  so `remaining=0` on a `PUBLISHED_DRAFT` run, and `config_sha=None` while the prose claimed
  the value was the budget digest.
- **Fix.** The table and receipt were re-measured at `26ad328` (32/33/20 passed, wall and RSS
  from `/usr/bin/time -l`); `config_sha` is the real
  `TaskBudget(max_tokens=10_000, max_wall_s=60).digest` =
  `acd96095edc08e36b9a28ba97069b51a8358c87ddf3f1933a690bf9f37eb5189`; the budget fields are the
  happy-path run's token budget (`tokens_used=170` of `max_tokens=10_000`), labelled as
  self-reported by the steps, not the test count. The acceptance map gained the new guards.
- **Test.** Not a test: the values are the measured run above and the assertions in
  `test_happy_path_reaches_published_draft_with_full_receipt`
  (`receipt.tokens_used == 170`, `receipt.config_sha == worker.budget.digest`).

### 11. P3 failed step's returned capabilities ignored (factory/worker.py)

- **Defect.** `if not result.ok: return BLOCKED` ran before `_authorize(result.requests)`, so a
  failing step that asked for `MAIN_MERGE` was recorded as a plain step failure and the audit
  trail never named the capability.
- **Fix.** `_authorize(phase, result.requests)` runs before the `ok` branch, so the refusal (and
  the receipt/durable block it carries) names the capability while the run fails closed.
- **Test.** `test_a_failed_step_still_has_its_returned_capabilities_audited` — `verify` fails
  *and* returns `MAIN_MERGE`; the run raises `WorkerAuthorizationError` with
  `UNAUTHORIZED_CAPABILITY:MAIN_MERGE`, not `STEP_FAILED`.

## Verification

Commands (worktree root, `python` = `/Users/hootie/src/v9/.venv/bin/python`):

```
python -m pytest -q tests/test_research_protocol.py tests/test_preregistration.py \
    tests/test_factory_worker.py tests/test_factory_memory_benchmark.py \
    tests/test_factory_queue_lease.py
# 180 passed, 1 skipped in 0.64s   (skip: statsmodels absent for the cross-check test)

python -m ruff check src/v8_next/evaluation/protocol.py \
    src/v8_next/evaluation/preregistration.py src/v8_next/factory/{worker,memory,queue}.py \
    tests/test_{research_protocol,preregistration,factory_worker,factory_memory_benchmark}.py
# All checks passed!

python -m mypy src/v8_next/evaluation/protocol.py src/v8_next/evaluation/preregistration.py \
    src/v8_next/factory/{worker,memory,queue}.py
# Success: no issues found in 5 source files
```

Per file: `test_research_protocol.py` 56 passed / 1 skipped, `test_preregistration.py`
39 passed, `test_factory_worker.py` 32 passed, `test_factory_memory_benchmark.py` 33 passed,
`test_factory_queue_lease.py` 20 passed.

Base-vs-fix proof: the four owned test files were copied into a `fda4580` worktree and run
there — 30 failed (each new guard, plus the two changed-outcome assertions), 130 passed. The
protocol-level base/fix comparison is the probe table above.

## Residual risks

- **A hand-built `TestingReport` is still trusted.** `assess_verdict` checks the report's
  method, alpha, family name/size and the rejection of the registered hypothesis, but it cannot
  re-derive the adjustment: the report does not carry the raw p-values or the hypothesis list.
  A caller who constructs `TestingReport(...)` directly with `corrected=True` and unadjusted
  values bypasses `multiple_testing`. Closing that needs the raw p-values persisted in the
  report (and a family digest check), which is a schema change beyond this repair.
- **`issued_ns` is caller-supplied.** The gate refuses reads issued before the registration,
  but nothing binds the instant to a trusted clock; a caller willing to forge a timestamp can
  still claim the read happened after registration. It is now a required, recorded, ordered
  field rather than an ignored default — not a trusted-time attestation.
- **`PENDING_REVIEW` is a new vocabulary member.** Any consumer mapping `FindingOutcome`
  exhaustively outside this repo must add it; in-repo consumers are the worker mapping and the
  efficiency aggregate, both updated.
- **The worker's `DRIVER_FAILED` path re-raises the original exception** after recording
  `BLOCKED`; a caller that does not inspect the queue still sees the `TypeError`/
  `AttributeError`, as intended, but should not assume `run_lease` always returns a `WorkerRun`.
- **`block_lease` does not check expiry.** It validates state/token/worker/attempt (per the
  audit's instruction), so a worker whose own lease expired but which nobody reclaimed can
  still block its own issue. That is harmless (terminal, no competing owner) but it is not an
  expiry gate.
