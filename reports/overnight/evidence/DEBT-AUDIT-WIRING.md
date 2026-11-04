# DEBT-AUDIT-WIRING — the native audit run supplies its own audit verdict

Status: **IMPLEMENTED** (wiring + derivation + tests). A live native run's *report label*
remains `AUDIT_INCOMPLETE` until ledger linkage is persisted (see Known gaps); the run no
longer renders `AUDIT_BLOCKED` for lack of a caller-supplied verdict.

## Defect

The wave-1 audit repair made `audit/reporting.py` fail closed: `write_reports(run_dir)` with
no `AuditVerdict` merges to `AUDIT_BLOCKED`, because an absent verdict is an unearned pass.
`audit/pipeline.py::run_native_audit` still called `write_reports(run_dir)` (both call sites),
and so did `app/cli.py::cmd_audit --report-only`. Every audit run therefore rendered
`AUDIT_BLOCKED` even when its persisted artifacts supported a complete audit.

## Change

`src/v8_next/audit/pipeline.py`

- New impure-once / pure-on-disk entry point:

  ```python
  def derive_audit_inputs(run_dir: Path) -> AuditInputs
  # AuditInputs(verdict: AuditVerdict, lineage: LineageChain | None, as_of_ns: int | None)
  ```

  It reads only `trade_lifecycles.jsonl`, `fills.jsonl`, `events.jsonl`,
  `reconciliation.json`, `economic_metrics.json`, `data_quality.json`. Nothing from the
  in-memory replay crosses the boundary, so a report-only re-render reaches the same label.

- Dimension rules (completeness is delegated to `verdicts.derive_verdict`; the caller never
  sets it):
  - **mechanical** — per-row states come from `verdicts.verdict_from_trade_metrics` (the
    delivered same-bar/stop-first rules). Run precedence: any `INVALID` → `FAIL`; else any
    `NOT_IDENTIFIABLE` (same-bar tie, no post-entry bar, no stop, unmeasured excursion) →
    `NOT_IDENTIFIABLE`; else `PASS`; no lifecycle rows → `NOT_RUN`.
  - **economic** — `NOT_RUN` when there are no trade rows or no published reconciliation;
    `INCOMPLETE` when the conservation residual, tolerance, realized PnL or
    `unexplained_cashflows` is missing, when a closed lifecycle row publishes no
    gross/realized PnL, when `unexplained_cashflows != 0`, or when
    `abs(residual) > tolerance`; `PASS` only when all of them agree.
  - **pit** — `NOT_RUN` when `data_quality.json` is absent or carries no bars/tape digest;
    `FAIL` when an explicit look-ahead/OOS flag is set (`look_ahead`, `lookahead`,
    `future_data_used`, `uses_future_data`, `oos_data_touched`, …); `PASS` otherwise.
    A detected look-ahead is reported as `FAIL`, not as "did not run" — the only deviation
    from the literal "else NOT_RUN" wording, and strictly more fail-closed.
  - **completeness** — all four required ids (`position_id`, `campaign_id`,
    `opportunity_id`, `decision_id`) are reported from the persisted lifecycle rows only
    when *every* row names them; otherwise the key is withheld and `derive_verdict` yields
    `INCOMPLETE` with `MISSING_SOURCE_ID:<key>`. The witness tuple is one deterministic row's
    identities (never a mix); `MULTI_CAMPAIGN` records that the run aggregates campaigns.
    No id is ever synthesised.
- Lineage participation: `_lineage_from_disk` builds the five explicit layers from persisted
  rows and returns `None` when the run persisted no decision/order/fill/cashflow row, so a run
  that never claimed a chain is not downgraded for its absence. Joins are explicit ids only:
  - decisions: `ADMISSION_ACCEPTED` events (`decision_id`, `opportunity_id`, `campaign_id`,
    event/recorded time);
  - opportunities: `OPPORTUNITY_DETECTED` events referenced by a decision, with the campaign
    the admitting decision created;
  - orders: one record per `client_order_id` at its first `ORDER_*` event; `decision_id` is
    taken from the row, or from the campaign's decision only when that campaign names
    exactly one decision (an ambiguous campaign stays unjoined);
  - fills: `fills.jsonl` joined to their order by `client_order_id`/`venue_order_id`;
  - cashflows: `CASHFLOW_RECORDED` events joined to a fill by `trade_id`. The campaign comes
    from the fill, not from the event's own `campaign_id`, because the ledger appends those
    fee cashflows after the replay and the sequence-based attribution stamps them all with
    the last campaign; the explicit `trade_id` join is the fact.
  - The certification cut is the latest availability persisted with the chain, so re-deriving
    from the same directory cannot flip a `LATE_RECORD`.
- Both `write_reports` calls in `run_native_audit` now receive
  `verdict`/`lineage`/`as_of_ns` derived once from the artifacts written moments earlier.

`src/v8_next/app/cli.py`

- `cmd_audit --report-only` derives the same inputs from the persisted run dir and passes them
  to both report renders. This path previously always rendered `AUDIT_BLOCKED` too.

## Acceptance evidence

Command: `pytest -q tests/test_audit_verdict_wiring.py tests/test_audit_native.py tests/test_audit_report_closure.py`
→ **57 passed** (14 new wiring tests + the existing 43, unmodified).

New tests pin (persisted-fixture evidence, not mocks):

| Scenario | Derived label |
|---|---|
| complete lifecycles + all four source ids (no chain claimed) | `AUDIT_COMPLETE` |
| same, with a fully joined five-layer chain | `AUDIT_COMPLETE`, lineage `COMPLETE` |
| lifecycle row missing `decision_id` | `AUDIT_INCOMPLETE` (`MISSING_SOURCE_ID:decision_id`) |
| same-bar stop+target tie (`same_bar_stop_and_target=true`) | `AUDIT_NOT_IDENTIFIABLE` |
| no lifecycle/trade rows | `AUDIT_NOT_RUN` (never `AUDIT_COMPLETE`) |
| conservation residual outside tolerance | `AUDIT_INCOMPLETE` |
| closed row without realized PnL | `AUDIT_INCOMPLETE` |
| look-ahead flag set (`oos_data_touched=true`) | `AUDIT_FAIL`, reason `PIT_LOOK_AHEAD_FLAG:…` |
| fill with no order/campaign/decision | verdict `COMPLETE`, lineage not complete → report `AUDIT_INCOMPLETE` |
| rebuild from the same dir twice, then mutate one lifecycle row | identical `AuditInputs`/chain digest; label changes only after the file changes |
| `audit --report-only` on a valid run dir with no trades | `AUDIT_NOT_RUN` (previously `AUDIT_BLOCKED`) |

Static checks on the touched files: `ruff check` clean; `mypy --strict` clean on
`src/v8_next/audit/pipeline.py`, `src/v8_next/app/cli.py`, `tests/test_audit_verdict_wiring.py`.

Existing assertions updated: **none**. `tests/test_audit_native.py` (26 tests) and
`tests/test_audit_report_closure.py` (17) pass as written; neither encoded the un-wired
producer behaviour.

## End-to-end smoke (SYNTHETIC tape — wiring only, not market evidence)

`run_native_audit(tape=synthetic 90-bar jsonl, expected_bars=90, skip_equivalence=False)`
completed with `validation.status = PASS` and wrote reports whose label moved from
`AUDIT_BLOCKED` (pre-wiring) to `AUDIT_INCOMPLETE`. The derived verdict for that run is
`mechanical=economic=pit=completeness=PASS` (`AUDIT_COMPLETE`, sources aggregated over 5
campaigns); the report is `AUDIT_INCOMPLETE` solely because the persisted lineage cannot be
certified `COMPLETE` (see below). The synthetic tape is a plumbing fixture: it is **not**
market evidence and is not committed.

## Known gaps / follow-ups (not fixed here — outside this ticket's owned files)

1. **Ledger linkage debt surfaced, not created.** On the smoke run, lineage certification is
   `INCOMPLETE` because the persisted ledger does not stamp campaign/decision identity on
   every row:
   - the first campaign's `ADMISSION_ACCEPTED` and entry order carry no `campaign_id`
     (`assign_campaign_ids` drops campaign 0 and only forward-attributes campaigns ≥ 2);
   - native `ORDER_*` events carry `client_order_id` but no `decision_id`;
   - `fills.jsonl` carries no `campaign_id`/`decision_id`, so fills resolve only through
     their order.
   Until those identities are persisted, a real run's report will read `AUDIT_INCOMPLETE`
   even when all trade metrics and the reconciliation are complete. That is the accurate
   label for the evidence, and the fix belongs to the ledger/telemetry producers.
2. **No authorized DEV tape in this worktree**, so a full native replay on real market data
   was `NOT_RUN` here; the wiring was exercised via the synthetic smoke, the report-only CLI
   test, and the derivation tests against persisted directories.
3. **24–48 h soak / sealed OOS**: `NOT_RUN` (no tape, no sealed-OOS access; out of scope).
4. Funding is not applied by the engine (`funding_mode=FUNDING_UNAVAILABLE`); the economic
   dimension certifies the run's own reconciliation, and the report already discloses the
   funding mode separately.
