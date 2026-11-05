# REPAIR-wave45-verdict — independent audit repairs for M4-06

Branch: `fix/wave45-verdict` (worktree `/Users/hootie/src/v9-worktrees/fixverdict`).
Base: `integration/wave1` HEAD `35e4a41` (2693 tests passing).

Owned files changed:

- `src/v8_next/evaluation/verdict.py`
- `tests/test_v9_verdict_matrix.py`
- `reports/overnight/V9_VERDICT.md` (regenerated from the repaired collector)
- `reports/overnight/evidence/M4-06.md` (rewritten; all quoted hashes recomputed)
- `reports/overnight/evidence/REPAIR-wave45-verdict.md` (this file)

## Defect 1 (P2) — ECONOMIC `PASS` was a self-declared string

**Before:** `_economic` mapped `costed["verdict"]` straight through `_COSTED_VERDICTS`; it never
required `net_return > 0`, never compared `trades` with `minimum_trades`, never consulted
`accepted_outcomes` or the registered decision rule. The audit repro (prereg
`accepted_outcomes=[NO_EDGE, INCONCLUSIVE, STOP, INVALID]`, `minimum_trades=30`, costed
`net_return=-0.50`, `trades=1`, `verdict=PASS`) rendered `ECONOMIC PASS`.

**Fix (`verdict.py`):** `_pass_claim_problems` + `_decision_rule_problems`. A `PASS` claim is
refused with one named reason per failed predicate unless:

- `net_return > 0`;
- an integer `trades >= preregistration.minimum_trades`;
- `PASS` is not pre-registered in `accepted_outcomes` (the repository's own
  `Preregistration.validate` forbids that: PASS is claimed by evidence, never preregistered);
- the registered rule is met concretely: `uncertainty.correction == "HOLM"`,
  `family_size == hypothesis_budget.max_hypotheses`, a numeric `[ci_lower, ci_upper]` that
  excludes zero and contains `net_return`, and `p_value` or the interval alpha at most
  `alpha / family_size`.

Non-`PASS` outcomes pass through only if listed in `accepted_outcomes`. A digest bound to a
different preregistration remains refused.

**Tests (fail at base, pass after):**

- `test_economic_registered_pass_is_earned_not_declared` (positive)
- `test_economic_pass_refused_without_positive_net_return` (audit repro, both reasons)
- `test_economic_pass_refused_below_minimum_trades`
- `test_economic_pass_refused_without_integer_trade_count`
- `test_economic_verdict_must_be_an_accepted_outcome`
- `test_economic_pass_refused_when_pass_is_preregistered`
- `test_economic_pass_refused_when_ci_includes_zero`
- `test_economic_pass_refused_without_holm_correction`
- `test_economic_pass_refused_without_corrected_rule` (4 params: uncorrected CI, p-value that
  fails Holm, understated family, missing uncertainty)
- `test_economic_pass_refused_without_registered_decision_rule`
- `test_economic_bundle_bound_to_another_preregistration_is_refused`

## Defect 2 (P2) — MECHANICAL `PASS` ignored test-log contents

**Before:** `_mechanical` parsed `reports/overnight/test-results.jsonl` only to count rows; a row
`{"passed":0,"failed":2693,"result":"FAILED"}` still yielded `MECHANICAL PASS`.

**Fix:** `_test_log_failures` inspects every row; non-zero `failed`/`errors` or a
`result`/`status` starting with fail/error blocks the dimension with the row index, the field
value and the log path. The log is included in the evidence refs even when it blocks.

**Tests (fail at base, pass after):**

- `test_failing_test_log_blocks_mechanical` (asserts `failed=2693` and the log path in reasons)
- `test_any_failure_marker_in_the_test_log_blocks_mechanical` (errors / result=error /
  status=FAILED_IMPORT)
- `test_passing_test_log_keeps_mechanical_pass` (no false positive)

## Defect 3 (P3) — stale, non-reproducible evidence hashes

**Before:** `M4-06.md` quoted `V9_VERDICT.md` hash `25b329f6…`, an ECONOMIC row that predated the
committed preregistration/access report, and regenerated resource-receipt hashes.

**Fix:** `V9_VERDICT.md` was regenerated with `collect_from_artifacts(Path('.'))` +
`render_markdown` and `M4-06.md` was rewritten around the files on disk at this commit; every
`path`/`sha256` pair is now checked by
`test_published_evidence_hashes_match_disk`, which parses the quoted pairs, re-hashes each file
and fails on any stale digest (28 pairs currently). The ECONOMIC row now explains the real
reason for `NOT_RUN` (no costed V9 intervention result), and the document states that the overall
verdict is `BLOCKED` only because `LIVE_APPROVAL` is never granted, while the scientific verdict
(`NOT_RUN`) is reported separately.

**Test:** `test_published_evidence_hashes_match_disk`.

## Verification

```
$ /Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_v9_verdict_matrix.py
85 passed in 0.28s

$ ruff check src/v8_next/evaluation/verdict.py tests/test_v9_verdict_matrix.py
All checks passed!

$ mypy src/v8_next/evaluation/verdict.py
Success: no issues found in 1 source file
```

Base-commit control run (base `verdict.py` restored, new tests kept):

```
16 failed, 68 deselected in 0.50s
```

So each repaired predicate has a test that fails without the fix.

Regeneration + hash check output is pasted verbatim in §4 of `M4-06.md`
(`verified 28 quoted evidence hashes against disk`).

## Residual risks

- The registered decision rule is evaluated against the `uncertainty` block schema defined here
  (`ci_lower`/`ci_upper`/`family_size`/`correction`/`p_value` or `ci_alpha`); aliases
  `ci95_low`/`ci95_high` and `alpha` are accepted. A future costed-result producer must emit
  that shape or the PASS claim is refused (fail-closed, not permissive).
- Only the declared family size is checked (`max_hypotheses`); the actual number of hypotheses
  tested is not independently reconstructed from a trial ledger.
- The resource dimension remains `INCONCLUSIVE` (scope below target) and ECONOMIC remains
  `NOT_RUN`; this repair changes gate enforcement, not measurement.
