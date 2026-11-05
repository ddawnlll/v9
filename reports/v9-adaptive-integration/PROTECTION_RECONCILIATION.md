# Protection reconciliation

Base commit `d0430a6`; full slice 2,160 H1 bars; native `BacktestEngine` ledger.

## Reported violations and what they were

The prior report (`reports/provenance/P1-SIM-ARM.md`) listed, at an earlier base commit:

`MISSING_ACK=50`, `PROTECTION_QUANTITY_MISMATCH=2`, `POSITION_QUANTITY_CONFLICT=2`,
`PROTECTIVE_SIDE_INCONSISTENT=2`, `BRACKET_GEOMETRY_INCONSISTENT=1`.

At current HEAD the same full replay produced **46 `MISSING_ACK` and nothing else** when the ledger
was reduced per event. Each violation was inspected against the native callback trace:

| Class | Count | Verdict |
|---|---:|---|
| submission and ACK/fill share the **same `ts_ns`** (one engine step) | 42 | event-ordering artefact of publishing the submission before its same-step ACK; not an exposure gap |
| ACK was delivered by the venue but **dropped from the ledger** because the fill resolved first | 4 | genuine telemetry-binding defect |
| real unprotected exposure | 0 | — |
| quantity/identity mapping error | 0 | — |

### Root cause of the 4 real gaps

`_admit_protection_event` buffered a protective `ORDER_SUBMITTED` until its ACK. When the entry
fill arrived first, the resolution branch popped the pending submission, published submission +
fill, and left the ACK orphaned in `_pending_protective_ack`. The next `_flush_pending_pairs` then
popped the ACK, found no submission, and silently discarded it. The venue *had* acknowledged; the
ledger lost the acknowledgement, so the invariant checker correctly reported `MISSING_ACK`.

Fix (`adaptive_strategy.py`):

- the resolution branch also pops and publishes the buffered ACK **before** the fill/cancel/reject;
- `_flush_pending_pairs` publishes an ACK even when its submission was already resolved, instead of
  dropping it.

Fail-before/pass-after: `tests/test_sim_protection_audit.py::test_buffered_ack_is_not_dropped_when_the_fill_arrives_first`.

### Root cause of the 42 same-step cases

`_measure` batched a submission with the next matching resolution by scanning **forward without a
bound**, which could also hide a genuine cross-bar gap. It now batches only events sharing the same
`ts_ns` (one engine step):

```
while index < len(events):
    ts_ns = events[index].ts_ns
    end = index + 1
    while end < len(events) and events[end].ts_ns == ts_ns:
        end += 1
    checker.apply_batch(events[index:end])
    index = end
```

`tests/test_sim_protection_audit.py::test_audit_batches_one_engine_step_only` proves a submission
whose ACK is one bar late is still reported.

## Two additional defects found while integrating management

| Symptom | Cause | Fix |
|---|---|---|
| every open position read as `UNPROTECTED`, management tried to add another stop | bracket child `_OrderFacts` never carried the native position id | `_bind_bracket_to_position` on entry fill and on `on_position_opened` |
| `PROTECTION_QUANTITY_MISMATCH` (`stop=0.018 position=0.009`) during an amendment | replacement stop was submitted while the old one was still live, so stop coverage counted twice | amend the live order with native `modify_order` (same identity, `AMENDED` event), never submit a duplicate; refuse if the live stop has no ACK or a submission is in flight |

## Current verdict

Full slice, bounded audit:

```
state = PROTECTED
violations = 0
position snapshots and protection events applied in ledger order
```

The arm never labels a position `PROTECTED` on submission alone: `_protection_state` requires a live,
acknowledged stop whose quantity covers the position, otherwise `GAP`/`PARTIAL`/`UNPROTECTED`, and
`evaluate_management` escalates an unreadable protection state to `EXIT` with `admission_ok=False`.

No violation was suppressed by weakening the checker. The new `AMENDED` event is a first-class
identity-preserving amendment, not an exemption.
