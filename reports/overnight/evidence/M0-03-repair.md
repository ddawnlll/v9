# M0-03 — repair: the opportunity layer is a real, required join

- Audit defect: **P2 — no opportunity layer** (`audit/lineage.py:422-433` on `integration/wave1`)
- Worktree: `/Users/hootie/src/v9-worktrees/fixaudit`, branch `fix/audit-closure`
- Base: `integration/wave1` @ `7b288cc`
- Date: 2026-11-05

## Defect

The module documented the chain as `opportunity -> decision -> order -> fill -> cashflow`,
but there was no opportunity record. `LineageDecision.opportunity_id` was only tested for
blankness, so a decision naming a nonexistent, duplicated or cross-campaign opportunity
still certified `COMPLETE` with every join "resolved".

## What changed (`src/v8_next/audit/lineage.py`)

- New frozen record `LineageOpportunity(opportunity_id, campaign_id, instrument_id,
  created_ns, available_ns)`.
- `LineageChain` gains `opportunities: tuple[LineageOpportunity, ...] = ()` as its first
  layer; `digest()` digests it, so provenance covers the root of the chain.
- `certify` now:
  - requires the opportunity layer when any decision references one — otherwise
    `MissingJoin("<opportunities>", ...)` and the verdict is `BLOCKED`
    (structural gap, same class as any other absent layer);
  - flags a repeated opportunity id with `DuplicateRecord` (`DUPLICATE_RECORD`) and
    **excludes it from the resolution index**, so a decision naming it cannot silently
    bind to whichever duplicate was indexed last; the referencing decision additionally
    gets a typed `MissingJoin` whose detail says the id is duplicated;
  - resolves every decision's `opportunity_id` to exactly one opportunity
    (`joins_checked` increments per resolution). Unknown id → `MissingJoin`;
    campaign disagreement → `CampaignMismatch`; instrument disagreement →
    `MislinkedRecord`;
  - checks opportunity `campaign_id` / `instrument_id` blankness (`MissingJoin`) and its
    `created_ns` / `available_ns` against the certification cut (`LateRecord` → `BLOCKED`).
- Backward compatibility kept: a chain with no decisions is still `BLOCKED` (no decision
  layer), and an empty chain still certifies nothing (`BLOCKED`, `joins_checked == 0`).
- `LineageOpportunity` exported in `__all__`; module docstring updated to five layers and
  to state that an unresolvable `opportunity_id` is a `MISSING_JOIN`.

No other module was edited. `audit/__init__.py` (not owned by this worker) still exports
only the PIT auditor; the new names import from `v8_next.audit.lineage`.

## Evidence

`tests/test_lineage_fail_closed.py`: **17 passed, 0 failed, 0 skipped** (was 12; the 12
pre-existing tests are kept, two of them extended with the new layer — see below — and 5
were added).

| Acceptance requirement | Test | Observed |
|---|---|---|
| full valid chain incl. opportunity → `COMPLETE` | `test_complete_chain_certifies_with_every_join_checked` | `COMPLETE`, `joins_checked == 4` (was 3: opportunity→decision is now counted), digest 64 hex |
| decision → unknown opportunity | `test_decision_with_unknown_opportunity_is_a_typed_missing_join` | `INCOMPLETE`, `MissingJoin` on `DEC-1`, detail names `OPP-UNKNOWN`, `require_complete` raises |
| duplicate opportunity id | `test_duplicated_opportunity_id_cannot_resolve_to_one_record` | `INCOMPLETE`, `DuplicateRecord` on `OPP-1`, plus `MissingJoin` on `DEC-1` ("duplicated"); `require_complete` raises `LineageViolation` |
| cross-campaign opportunity | `test_cross_campaign_opportunity_is_a_campaign_mismatch` | `INCOMPLETE`, `CampaignMismatch` on `DEC-1` |
| opportunity instrument mismatch | `test_opportunity_instrument_mismatch_is_mislinked` | `INCOMPLETE`, `MislinkedRecord` on `DEC-1` |
| decisions with no opportunity layer | `test_decisions_without_any_opportunity_layer_are_blocked` | `BLOCKED`, `MissingJoin("<opportunities>")` |
| no decisions at all still `BLOCKED` | `test_missing_layers_block_and_never_certify_complete` | unchanged from before the repair (`BLOCKED`) |
| digest still order-independent | `test_provenance_digest_is_order_independent` | equal digests forward/backward with the opportunity layer present |

Two pre-existing tests were extended, not weakened: the shared `_chain()` fixture now
supplies `OPPORTUNITY` (otherwise no chain could ever certify `COMPLETE` again), the
digest test builds its two chains with two opportunities, and `joins_checked` moved
3 → 4 because there is now one more real join to check.

## Commands run (worktree `/Users/hootie/src/v9-worktrees/fixaudit`)

```
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_lineage_fail_closed.py
# 17 passed in 0.20s
/Users/hootie/src/v9/.venv/bin/python -m ruff check src/v8_next/audit/lineage.py tests/test_lineage_fail_closed.py
# All checks passed!
/Users/hootie/src/v9/.venv/bin/python -m ruff format --check src/v8_next/audit/lineage.py tests/test_lineage_fail_closed.py
# 2 files already formatted
/Users/hootie/src/v9/.venv/bin/python -m mypy src/v8_next/audit/lineage.py
# Success: no issues found in 1 source file
```

## Residual risks

- `certify` still validates identity and joins, not money; cashflow conservation remains
  `audit.cashflow`'s job.
- Duplicate opportunity ids are reported as `DUPLICATE_RECORD` (the code shared by
  decision/order/cashflow ids), not a new code; `LineageViolationCode` is unchanged so
  existing consumers keep working.
- An opportunity that no decision references is not itself a violation (an admitted
  opportunity may legitimately not be traded); it only affects the digest.
- `certify` does not order-check opportunity→decision event time; an inversion there is
  not currently a typed failure.
