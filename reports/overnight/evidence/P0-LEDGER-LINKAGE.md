# P0 — Ledger `campaign_id` / `decision_id` linkage

**Branch:** `fix/ledger-linkage` (merged into `integration/wave1`) · **Scope:** `adapters/expert_strategy.py`, `audit/pipeline.py`, `adapters/execution_telemetry.py`

## Problem

`audit/pipeline.derive_audit_inputs` could only render `AUDIT_COMPLETE` for a synthetic run:
a real native replay had (a) ORDER_* events without `decision_id`, (b) fills without
`campaign_id`/`decision_id`, (c) campaign numbering that dropped the first campaign. The audit
therefore reported `AUDIT_INCOMPLETE` for every honest run — correct fail-closed behaviour, but
it meant the audit could never be earned.

## Change

- The strategy's telemetry emission now carries `decision_id` (and `opportunity_id` where the
  decision has one) on every order/fill event that originates from a decision; the entry leg
  inherits the admission decision id deterministically.
- `assign_campaign_ids` numbers the first `POSITION_OPENED` as `C0001` and attributes
  order/fill/cashflow rows to the campaign interval they fall inside; ids are stable for a given
  tape/replay.
- Fills inherit `campaign_id`/`decision_id` from their order when resolvable. A fill that cannot
  be resolved stays unattributed, so lineage certifies `BLOCKED` instead of guessing.

## Verification (run in this session)

```
.venv/bin/python -m pytest -q tests/test_ledger_linkage.py          -> 7 passed
.venv/bin/python -m pytest -q tests/test_audit_verdict_wiring.py \
    tests/test_audit_native.py tests/test_audit_report_closure.py \
    tests/test_lineage_fail_closed.py                               -> 76 passed
.venv/bin/ruff check src tests                                      -> clean
.venv/bin/mypy src                                                  -> clean (253 modules)
```

Fail-closed cases exercised by `tests/test_ledger_linkage.py`: removing one order's
`decision_id` downgrades the derived verdict to `AUDIT_INCOMPLETE` naming the missing id (never
`COMPLETE`); an unresolvable fill stays unattributed and certification returns `BLOCKED`;
campaign ids are `C0001`-based, contiguous and identical across two derivations.

## Residual

- The full native replay label was not re-measured after the change: the replay runner consumes
  ~1–2 min per run and the linkage fix is exercised on persisted-artifact fixtures plus the
  inherited audit suite. Marked `NOT_RUN` for a live label until the next tape run.
- Producer-side identity for the *first* campaign's entry order depends on the admission
  decision existing in the same run; when it does not, the audit reports the gap rather than
  filling it in.
