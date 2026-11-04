# M3-01 repair — entry intent identity, fail-closed defaults, POST_ONLY economics

Objective: repair the three defects the independent wave-1 audit reproduced in
`src/v8_next/economics/entry.py`. The M3-01 proposal contract, the advisory pin, the PIT quote
rule, TTL semantics and the revision/cancel model are otherwise unchanged.

Branch: `fix/entry-intent` (base `integration/wave1`, `7b288cc`).

## Defects fixed

### 1. P2 — `intent_id` identity collision

`make_intent_id` hashed nine hand-picked fields, so two proposals differing only in
`size_policy_id`, `structural_stop_ref`, `venue_version`, `policy_version` or
`kill_conditions` shared an id and `is_duplicate()` returned `True`.

Fix: identity is now the digest of the **whole record minus `intent_id`**
(`record.model_dump(mode="json")` → drop `intent_id` → existing `_digest`). There is no field
list to keep in sync, so any field added to `EntryIntent` is covered automatically.

- `make_intent_id(record: EntryIntent) -> str` (signature changed from a keyword subset; the
  hand-picked subset was the defect).
- `build_intent` validates the proposal once with a private placeholder id and then calls
  `_stamp_identity`, which sets the digest of that record. `cancel()` also re-stamps after changing
  `state`/`cancel_reasons`, so every intent's `intent_id` equals the digest of its own content.
- Determinism/stability preserved: identical records reproduce the same id; no wall clock and no
  randomness enter the digest beyond the record's own `created_ns`.

### 2. P2 — fail-open safety defaults

`evaluate_entry` defaulted `venue_status=ACCEPTED`, `belief_stale=False`,
`belief_available=True`, so a call carrying only a quote returned `ENTRY_READY` and the
`VENUE_UNKNOWN` guard was dead unless the caller opted in.

Fix: defaults inverted to fail closed — `venue_status=UNKNOWN`, `belief_stale=True`,
`belief_available=False`. A caller must now assert the safety evidence it actually has. Additional
coercion/validation failures are BLOCKED, never permissive:

- an unlisted/unreadable venue value (e.g. `"PENDING_NEW"`) → `EntryMode.BLOCKED` /
  `EntryReason.VENUE_UNKNOWN` (not coerced, not `ACCEPTED`);
- non-bool belief flags → `EntryMode.BLOCKED` / `EntryReason.DATA_CONTRADICTORY`.

`VenueOrderStatus` strings are still accepted for wire convenience but are normalised through the
enum, so an unknown string cannot leak through.

### 3. P3 — POST_ONLY priced as a taker limit

`_reference_price` returned the ask for a LONG and that same value was emitted as the post-only
limit; bid 99 / ask 101 produced a post-only buy at 101, which is immediately marketable.

Fix: POST_ONLY has its own branch. A LONG post-only rests on the **bid**, a SHORT post-only rests on
the **ask**. If the passive price is at/through the opposite touch (`bid >= ask` for a buy,
`ask <= bid` for a sell) the evaluation is `EntryMode.BLOCKED` with the new
`EntryReason.POST_ONLY_WOULD_CROSS`, never `TOUCHED_ONLY`. If either touch is missing, passivity
cannot be proven and the result is `WAIT_BELIEF` / `DATA_MISSING`. A resting post-only order has no
touch evidence at all, so its fill certainty is `UNKNOWN`. `LIMIT` keeps its prior taker-limit
semantics (`ask` for LONG, `bid` for SHORT, `TOUCHED_ONLY` with a spread).

`EntryMode.BLOCKED` is additive to the mode vocabulary (it joins `NOT_YET_VALID` as a
non-decision state); `POST_ONLY_WOULD_CROSS` is additive to `EntryReason`.

## Test changes (and why)

`tests/test_entry_intent.py`: 18 → 27 tests, all passing.

- `test_intent_identity_is_deterministic_and_idempotent` now calls `make_intent_id(record)`. The old
  assertion recomputed the id from the hand-picked keyword subset, i.e. it encoded the defect.
- `test_identity_covers_every_field_that_defines_the_proposal` (new) asserts distinct ids and
  `is_duplicate() == False` for ±`size_policy_id`, ±`structural_stop_ref`, ±`venue_version`,
  ±`policy_version`, ±`kill_conditions` and ±`friction_status`.
- `test_cancelled_record_reproduces_its_own_identity` (new) pins the re-stamp invariant.
- Evaluation tests that expect a live state now pass explicit fresh safety evidence through a local
  `evaluate()` helper. The old calls relied on the fail-open defaults that are the defect; every
  assertion they made is unchanged. `test_not_yet_valid_is_explicit`, the expiry test and the
  cancelled-state test still use the raw call because the TTL/state checks legitimately precede the
  venue gate.
- New fail-closed tests: omitted safety inputs, venue `ACCEPTED` with omitted belief freshness,
  unreadable venue string.
- New post-only tests: passive-side limit for LONG and SHORT, strict non-marketability (shoe-leather),
  locked-book crossing → `BLOCKED` + `POST_ONLY_WOULD_CROSS`, and one-sided quotes → `DATA_MISSING`.
- No existing assertion was weakened or deleted.

## Executed verification (worktree `/Users/hootie/src/v9-worktrees/fixentry`)

```
/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_entry_intent.py
27 passed in 0.15s

/Users/hootie/src/v9/.venv/bin/python -m pytest -q tests/test_entry_intent.py tests/test_decision_contracts.py
107 passed in 0.19s

/Users/hootie/src/v9/.venv/bin/python -m ruff check src/v8_next/economics/entry.py tests/test_entry_intent.py
All checks passed!

/Users/hootie/src/v9/.venv/bin/python -m ruff format --check src/v8_next/economics/entry.py tests/test_entry_intent.py
2 files already formatted

/Users/hootie/src/v9/.venv/bin/python -m mypy src/v8_next/economics/entry.py
Success: no issues found in 1 source file
```

0 failed, 0 skipped, 0 xfailed. Full suite NOT_RUN (worker protocol: only owned tests plus the
M2-02 contract test requested by the assignment).

Source SHA: implementation commit `3d1075bfda1e206125e79e3b66904ff65a2b6d13` on branch
`fix/entry-intent`. This evidence file is committed on top of it.

## Residual risks / limits

- Full-record identity includes `created_ns`, `state` and `cancel_reasons`, as the audit
  instructed. Consequence: a repeated signal rebuilt at a later clock is not a duplicate, and
  `cancel()` mints a new id for the terminal copy (the id always matches the record's content).
  Consumers that need cross-rebuild idempotence must replay the original record, not rebuild it.
- With normal two-sided quotes (`bid < ask`) a passive-side post-only price can never cross, so the
  crossing branch fires only on a locked/crossed book (`bid == ask`, the quote validator forbids
  `bid > ask`). This is the only marketable case representable by `MarketQuote`; a future
  explicit order-price field would need the same guard.
- Missing either touch makes a post-only proposal wait rather than rest; that is deliberate
  (passivity unprovable) but conservative for one-sided venues.
- `EntryMode.BLOCKED` / `EntryReason.POST_ONLY_WOULD_CROSS` are additive enum members. Renaming them
  later changes the replay surface.
- Still no native side effect, no order authority, no fill-probability claim. BLOCKED items: none.
