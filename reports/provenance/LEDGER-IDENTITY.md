# P0 — Ledger `campaign_id` / `decision_id` identity joins

**Branch:** `fix/ledger-identity` · **Base:** `15379fb` (`integration/wave1`) · **PR:** draft

## Defect (reproduced)

The report could read `MECHANICAL PASS` while the independent audit stayed
`AUDIT_INCOMPLETE` because the persisted native ledger did not carry the joins:

1. the **first** campaign's `ADMISSION_ACCEPTED` and entry order occur before the first
   `POSITION_OPENED`, so `assign_campaign_ids` left them without a `campaign_id`;
2. native `ORDER_*` callbacks carry `client_order_id` (and later `position_id`) but no
   `decision_id`;
3. `fills.jsonl` carries no `campaign_id`/`decision_id`, so fills can only resolve through
   their order — which was itself unjoined.

## Fix

### Producer — `src/v8_next/adapters/expert_strategy.py`

- Records explicit maps while the identity is known: `client_order_id -> decision_id /
  opportunity_id` and `position_id -> decision_id / opportunity_id`.
- `ORDER_SUBMITTED` now carries the submitted entry `client_order_id`.
- Every native `ORDER_*`, protective, `POSITION_OPENED` and `POSITION_CLOSED` audit row now
  carries the decision/opportunity it belongs to.
- TWAP child slices carry the parent decision identity through the pending queue.
- Additive only: no order construction, submission, sizing or risk logic changed.

### Pipeline — `src/v8_next/audit/pipeline.py`

- Pre-open entry attribution now uses the next `POSITION_OPENED`'s **decision identity** when
  both rows name it, with event ordering only as the fallback. This fixes campaign 0/first.
- A second pass propagates `campaign_id` from the admission row to every row naming the same
  `decision_id`, so native order rows no longer depend on a client-id suffix or timestamp.

## Evidence

New: `tests/test_ledger_identity_join.py` — **4 passed**

| Test | What it proves |
|---|---|
| `test_producer_binds_client_order_id_to_the_entry_decision` | submitted entry/bracket ids bind to decision + opportunity |
| `test_first_campaign_entry_leg_is_attributed_before_the_position_opens` | admission/entry accepted/filled all get `C0001` |
| `test_decision_identity_propagates_to_rows_without_a_campaign` | a no-suffix amended order joins by decision id alone |
| `test_persisted_chain_certifies_complete_from_disk` | opportunity → decision → order → fill → cashflow certifies `COMPLETE` |

```
pytest -q tests/test_ledger_identity_join.py tests/test_audit_verdict_wiring.py tests/test_expert_strategy.py
27 passed

ruff check src tests        -> clean
mypy src                    -> clean (250 files)
pytest -q                   -> 2727 passed, 72 skipped, 512 deselected (21.57 s)
```

CI on the reported head: **PR #67 / `15379fb` V9 gate pass** on both the push and
`pull_request` runs (~1 min), so the committed integration head is independently verified by
GitHub Actions, not only by the local suite.

## Not claimed

- No native replay was run in this worktree. The fix is proven on a persisted, native-shaped
  ledger; a real run must now be re-executed to confirm the rendered label reaches
  `AUDIT_COMPLETE` (it should, when every row carries the ids above).
- No economic, live-trading or promotion claim.

## Native end-to-end verification (after the fix)

A real Nautilus audit run on the authorized DEV tape was executed in this branch:

```
PYTHONPATH=src .venv/bin/python -m v8_next.app.cli audit \
  --tape research/tape/btcusdt-1h-2025Q1/tape.jsonl \
  --output-dir <scratch>/audit-ledger-full \
  --expected-bars 2160
```

| Item | Result |
|---|---|
| Tape SHA-256 | `84978f2d53a70afb769f760380b1587f073edfdc07d628c3a3be6773514673d7` |
| Opportunities / entries / closed | 123 / 45 / 44 |
| Native fills | 89 |
| Gates | **G1–G12 all PASS** |
| Validation | PASS, 0 violations |
| Lineage | **COMPLETE, 358 joins, 44 campaigns** |
| Verdict dimensions | mechanical PASS, economic PASS, PIT PASS, completeness PASS |
| Rendered label | **`AUDIT_COMPLETE`** (`**DURUM:** AUDIT_COMPLETE`) |

Before this fix the same producer path could leave the independent audit
`AUDIT_INCOMPLETE` despite `MECHANICAL PASS`. That gap is now closed with a real native
run, not only the synthetic ledger fixture.

The recorded net PnL (`-8.67470686 USDT`) is the **V8 baseline** result of that tape; it is
not a V9 economic claim and must not be compared with the earlier `-21.23` measurement
without equalized conditions.
