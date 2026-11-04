# M0-07 — repair: the rendered report consumes the audit verdict

- Audit defect: **P2 — no report-level consumer** (`audit/reporting.py:136` +
  `audit/verdicts.py:143-148`)
- Worktree: `/Users/hootie/src/v9-worktrees/fixaudit`, branch `fix/audit-closure`
- Base: `integration/wave1` @ `7b288cc`
- Date: 2026-11-05

## Defect

`reporting.py` stamped `overall = "PARTIAL"/"COMPLETE"` from `gate_results` /
`validation_results` only. `AuditVerdict`, `audit_label()` and
`lineage.certify()` were correct but nothing rendered them, so "missing source IDs stop
`AUDIT_COMPLETE`" and "no report certifies COMPLETE with a missing join" were true of an
API nobody called. A run with an absent or incomplete audit verdict still rendered
`**DURUM:** COMPLETE`.

## What changed (`src/v8_next/audit/reporting.py`)

- New `resolve_report_status(*, verdict=None, lineage=None, gates=(), validation=None)`
  merges the evidence into one fail-closed report label, taking the most severe state:
  - with no `AuditVerdict` at all → `AUDIT_BLOCKED` (completeness was never established),
    regardless of how green the gates look;
  - the verdict's `completeness`, `mechanical`, `economic` and `pit` states each map to
    their own label (`PASS` → `AUDIT_COMPLETE`, otherwise `AUDIT_<STATE>`), and the most
    severe wins — so a complete-evidence/`FAIL`-mechanics verdict reads `AUDIT_FAIL`, not
    `AUDIT_COMPLETE`;
  - a supplied `LineageCertification` (or a `LineageChain` certified with `as_of_ns`)
    contributes its own verdict: `INCOMPLETE` → `AUDIT_INCOMPLETE`, `BLOCKED` →
    `AUDIT_BLOCKED`;
  - gates can only downgrade (`FAIL` → `AUDIT_FAIL`, `BLOCKED` → `AUDIT_BLOCKED`);
  - validation can only downgrade (`!= PASS`, including absent/empty validation evidence →
    `AUDIT_INCOMPLETE`).
- `build_context(run_dir, *, verdict=None, lineage=None, as_of_ns=None)` stores
  `audit_verdict` and `lineage_certification` in the context; `write_reports(...)` takes
  and forwards the same keywords. A `LineageChain` without `as_of_ns` is refused with
  `ValueError` rather than certified on an invented cut.
- Both renderers now call `_report_status(ctx)` instead of the local gate-only `overall`:
  `FINAL_DELIVERY_TR.md` prints `**DURUM:** <label>` and section 12 prints
  `- Denetim durumu: **<label>** (<verdict/lineage provenance>)`;
  `AUDIT_REPORT.md` prints `Audit status: **<label>** (...)` next to `Validation:`.
- The eleven report sections, the twelve Turkish headings and every other public report
  field are unchanged; callers that supply no verdict/chain still get a fully rendered
  report — it is just never labelled as a pass.

`audit/pipeline.py` and `app/cli.py` (the two importers of `write_reports`) are outside
this worker's ownership and were not edited; they call `write_reports(run_dir)`, so their
reports now render `AUDIT_BLOCKED` until a caller passes an `AuditVerdict`. That is the
intended fail-closed outcome, recorded under residual risks below.

## Exact labels (asserted)

| Input | Label |
|---|---|
| no verdict, no lineage (even with gates PASS + validation PASS) | `AUDIT_BLOCKED` |
| verdict with missing required source IDs (completeness `INCOMPLETE`) | `AUDIT_INCOMPLETE` |
| verdict `mechanical=PASS/FAIL`, completeness `PASS` | `AUDIT_FAIL` |
| verdict `mechanical=NOT_IDENTIFIABLE`, completeness `PASS` | `AUDIT_NOT_IDENTIFIABLE` |
| verdict `economic=INCOMPLETE` (completeness `INCOMPLETE`) | `AUDIT_INCOMPLETE` |
| complete verdict + `lineage.certify` missing the opportunity layer | `AUDIT_BLOCKED` |
| complete verdict + lineage chain whose decision names an unknown opportunity | `AUDIT_INCOMPLETE` |
| complete verdict + gate `FAIL` | `AUDIT_FAIL` |
| complete verdict + gate `BLOCKED` | `AUDIT_BLOCKED` |
| complete verdict + validation `FAIL` or absent | `AUDIT_INCOMPLETE` |
| complete verdict + complete lineage + validation `PASS` | `AUDIT_COMPLETE` |

`AUDIT_COMPLETE` is the only "pass" label; there is no `AUDIT_PASS` (a `PASS` dimension
maps to `AUDIT_COMPLETE`).

## Evidence

`tests/test_audit_report_closure.py` (new): **17 passed, 0 failed, 0 skipped**.

- Resolver: `test_no_audit_input_is_explicitly_blocked_not_complete`,
  `test_missing_source_ids_never_read_as_complete`,
  `test_mechanical_fail_is_labelled_fail_not_pass`,
  `test_mechanical_not_identifiable_is_labelled_not_identifiable`,
  `test_unresolved_dimension_downgrades_a_complete_verdict`,
  `test_lineage_incomplete_downgrades_overall`,
  `test_lineage_missing_join_keeps_report_incomplete`,
  `test_gate_failure_downgrades_even_with_complete_verdict`,
  `test_failing_validation_downgrades_even_with_complete_verdict`,
  `test_absent_validation_is_not_a_pass`,
  `test_everything_earned_is_the_only_path_to_complete`.
- End-to-end rendering through `write_reports` on a temp run dir (asserting the exact
  `**DURUM:**` line, not a substring): `test_rendered_report_without_audit_input_is_blocked`
  (`AUDIT_BLOCKED`), `test_rendered_report_with_missing_source_ids_is_incomplete`
  (`AUDIT_INCOMPLETE`), `test_rendered_report_with_mechanical_fail_never_reads_as_pass`
  (`AUDIT_FAIL`), `test_rendered_report_is_complete_only_with_verdict_and_lineage`
  (`AUDIT_COMPLETE`, then `AUDIT_BLOCKED` after the same verdict is paired with a chain
  missing its opportunity layer), `test_rendered_report_renders_a_supplied_certification_directly`
  (`AUDIT_COMPLETE`), `test_chain_without_cut_cannot_be_certified` (`ValueError`).

No pre-existing test encoded the old fail-open label, so none had to be changed: grep found
no test asserting the audit report's `DURUM` / `PARTIAL` status (the `PARTIAL` matches in
`tests/` are unrelated veto/reconciliation statuses), and the only report assertions are
the twelve Turkish *headings* in `tests/test_audit_native.py:278-281` and in
`pipeline.validate_audit_dir`, which are untouched.

### Pre-existing importers / exercises of `audit.reporting` (found by grep)

- `src/v8_next/audit/pipeline.py:1266,1268,1278` — `write_reports(run_dir)` (lazy import).
- `src/v8_next/app/cli.py:588,595,601` — `write_reports(run_dir)` in `audit --report-only`.
- No `tests/**` file imports `v8_next.audit.reporting` directly. The test files that
  exercise the reporting path indirectly were run anyway:
  `tests/test_audit_native.py` (26 passed, includes
  `test_report_only_mode_does_not_run_the_engine`), `tests/test_audit_kernel.py`
  (9 slow-marked tests, 9 passed), `tests/test_same_bar_metrics_fail_closed.py`
  (21 passed; consumes `audit.verdicts`).

## Commands run (worktree `/Users/hootie/src/v9-worktrees/fixaudit`)

```
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_audit_report_closure.py
# 17 passed in 0.16s
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_audit_native.py
# 26 passed in 0.80s
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_audit_kernel.py -m slow
# 9 passed in 0.13s
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_same_bar_metrics_fail_closed.py
# 21 passed in 0.14s
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_lineage_fail_closed.py \
    tests/test_accounting_oracle_two_campaign.py tests/test_audit_report_closure.py \
    tests/test_audit_native.py tests/test_audit_kernel.py tests/test_same_bar_metrics_fail_closed.py
# 99 passed, 9 deselected (the 9 deselected are the slow tests re-run above: 9 passed)
/Users/hootie/src/v9/.venv/bin/python -m ruff check src/v8_next/audit/reporting.py \
    tests/test_audit_report_closure.py
# All checks passed!
/Users/hootie/src/v9/.venv/bin/python -m mypy src/v8_next/audit/reporting.py
# Success: no issues found in 1 source file
```

## Residual risks

- `pipeline.py` / `cli.py` do not build an `AuditVerdict`, so runs driven by them render
  `AUDIT_BLOCKED`. Passing a verdict (or a lineage chain) is now required to certify;
  wiring that belongs to the pipeline owners, and the label is explicit rather than wrong.
- The report label is a single string; it carries no machine-readable breakdown. The
  provenance is rendered inline (`verdict mechanical=… economic=… pit=… completeness=…;
  lineage=…`), but consumers that need the states should read `AuditVerdict` directly.
- Severity ranking is fixed in `_STATUS_SEVERITY`; e.g. `BLOCKED` outranks `NOT_RUN`
  outranks `INCOMPLETE` outranks `INVALID` outranks `NOT_IDENTIFIABLE`/`FAIL`. Any label not
  in that table would raise `KeyError` rather than silently pass — new `VerdictState`
  members must be ranked explicitly.
- `as_of_ns` is caller-supplied; the report cannot know the true cut. A wrong cut is a
  caller error, but it can only certify what that cut allows (a later cut cannot make a
  missing join appear).
