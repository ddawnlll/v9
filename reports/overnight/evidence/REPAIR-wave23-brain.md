# REPAIR — wave 23, brain (M2-06 shadow parity / M3-06 universe / M3-03 management)

Branch `fix/wave23-brain` off `integration/wave1` @ `fda4580`. An independent audit reproduced
five defects; each is reproduced below as defect → fix → test, with the pre-fix result recorded.
Every added test was run against the pre-fix code and failed; none is a test that cannot fail.

## Defect 1 — P2 `UNKNOWN` belief presented as fresh (`evaluation/shadow_parity.py`)

**Defect.** `_belief_view` set `stale` only for `BeliefStatus.STALE` or `QualityState.STALE` and
`abstain` only for `BeliefStatus.ABSTAIN`. A belief whose aggregate quality is unreadable
(`UNKNOWN`/`CONTRADICTORY`/`MISSING`) while a regime is still named arrived at
`TraderDecisionPolicy` as *fresh*: the policy emitted a bare `HOLD` (or even considered `ENTER`) on
evidence nobody can read, with no `BELIEF_*` reason code.

**Fix.** Only `FRESH` + `QualityState.OK` is usable. `abstain` stays `BeliefStatus.ABSTAIN`
(keeps its more specific `EVIDENCE_INSUFFICIENT`/`BELIEF_ABSTAIN` branch); every other status or
non-`OK` quality is stale, so new risk is blocked with `BELIEF_STALE`. Written as
`status is not FRESH or quality is not OK`, so a future status value fails closed by construction.
`BeliefStatus` is consumed nowhere outside `v8_next.belief` and this module (grep-verified —
`src/v8_next/evaluation/shadow_parity.py:82,957,963` were the only non-belief uses), so this
mapping is the single authority.

**Tests** (`tests/test_shadow_parity.py`):

| test | pre-fix |
|---|---|
| `test_unknown_belief_can_never_enter_or_bare_hold` | FAIL (policy still `ENTER`s / no `BELIEF_*`) |
| `test_unknown_belief_on_an_open_position_cannot_hold` | FAIL (policy still `HOLD`s) |
| `test_every_non_fresh_status_is_unusable_and_abstain_keeps_its_branch` | FAIL |

Positive controls in the same tests: the identical cut with the unmodified belief yields `ENTER`
(entry cut) and `HOLD` (open-position cut), so the assertion discriminates the belief mapping, not
the fixture. The `UNKNOWN` fixture keeps the permissive `uncertainty` band on purpose: the
`BELIEF_*` gate alone must block, not the uncertainty ladder.

## Defect 2 — P2 receipts dropped the constraints that shaped the decision (`evaluation/shadow_parity.py`)

**Defect.** `DecisionLedger.append(decision, ReceiptInputs(inputs=...))` — no `constraints`. Every
receipt therefore certified an unconstrained decision: `VENUE_UNKNOWN`, `STALE`,
`GROSS_EXPOSURE_EXCEEDED`, `CAPACITY_FULL` etc. disappeared from the archive even though the policy
had reported them.

**Fix.** Request assembly extracted to `_decision_request(events, cut, belief)`; `shadow_decisions`
appends `policy.explain(request).constraints` alongside the input refs. `DecisionReceipt.create`
hashes the sorted constraint tuple, so the digest covers it.

**Tests** (`tests/test_shadow_parity.py`):

| test | pre-fix |
|---|---|
| `test_receipt_carries_the_provenance_that_shaped_the_decision[VENUE_UNKNOWN]` | FAIL (`() != ('VENUE_UNKNOWN',)`) |
| `…[POSITION_OPEN]` | FAIL |
| `…[SCALE_IN_DISABLED]` | FAIL |
| `test_receipt_digest_covers_the_constraint_set` | FAIL |

The parametrised test independently recomputes `TraderDecisionPolicy.explain(...)` for every cut
and requires `receipt.constraints == explanation.constraints` and
`receipt.reason_codes == explanation.reason_codes`, then locates the one receipt carrying the named
code. `VENUE_UNKNOWN` is an active *constraint*; `POSITION_OPEN` and `SCALE_IN_DISABLED` are
policy *reason codes* (the policy never emits them as constraint strings), so each is asserted in
the field the policy actually writes it to. The digest test appends the same decision with and
without `("VENUE_UNKNOWN",)` and asserts distinct, both-verifying receipts.

## Defect 3 — P2 future metadata leaked into as-of eligibility (`adapters/universe.py`)

**Defect.** `resolve_metadata` scanned `metadata_version -> term set` contradictions over *all*
records, including `available_at > as_of_ns`. One future record reusing the current version label
with different terms flipped an as-of verdict from `ELIGIBLE` to `CONTRACT_CHANGED` — a PIT leak by
the rule the module claims to enforce.

**Fix.** Filter to `available_at <= as_of_ns` first, then scan contradictions over that usable set.
The ambiguity and `SUPERSEDED_METADATA_VERSION` rules already operated on the usable set and are
unchanged.

**Tests** (`tests/test_universe_feasibility.py`):

| test | pre-fix |
|---|---|
| `test_future_record_reusing_the_version_label_cannot_flip_an_as_of_verdict` (the audit's exact reproduction: `as_of = T0+10`, future record at `T0+110` reusing `v1` with a different tick size) | FAIL (`CONTRACT_CHANGED`, not `ELIGIBLE`) |
| `test_contradiction_scan_is_bounded_by_the_as_of_instant` (conflict available exactly at `as_of` → `CONTRACT_CHANGED`; same record 1 ns later → `ELIGIBLE`) | FAIL |

## Defect 4 — P2 the zero-native-mutation gate could not observe a mutation (`evaluation/shadow_parity.py`)

**Defect.** `capture_replay` digested the same frozen `ReplayScript` before and after the shadow
pass. The two readings were identical by construction, so `ShadowMutationError` was unreachable and
the test that "covered" it could never fail.

**Fix.** A `NativeSession` protocol is the only native surface a shadow pass is handed: `digest()`
plus the only mutating verbs the adaptive path could reach, `submit(intent)` and `cancel(id)`. For
`SHADOW`, `capture_replay` reads `session.digest()` before and after the pass; a change raises
`ShadowMutationError` with `DIGEST_DRIFT:native_session`. No sink (`SHADOW_SINK_MISSING`) or a sink
whose digest is not a digest (`SHADOW_SINK_MALFORMED`) fails closed. `shadow_step` defaults to the
production `shadow_decisions` and exists so a test can inject a step that reaches for the sink.
The fixture digests are still computed once, for the signature; the gate no longer pretends they
prove anything.

**Tests** (`tests/test_shadow_parity.py`), with `RecordingNativeSession` (digests its own
`submit`/`cancel` log):

| test | pre-fix |
|---|---|
| `test_mutating_adaptive_step_is_caught_by_the_native_sink_gate` | FAIL (`ShadowMutationError` unreachable) |
| `test_cancelling_adaptive_step_is_caught_by_the_native_sink_gate` | FAIL |
| `test_clean_shadow_pass_reads_the_sink_twice_and_leaves_it_alone` | FAIL (no sink API) |
| `test_shadow_capture_requires_an_observable_native_sink` | FAIL |

Proven now: with a live sink in scope, a shadow pass over the fixture issues no `submit`/`cancel`
and leaves the sink byte-identical, and a step that does call the sink is detected.
Still **NOT_RUN**: a Nautilus paper session around the shadow pass (a real `NativeSession` bound to
a paper engine). The gate is wired to the protocol and exercised by a recording double, not the
engine; no claim about engine-side non-mutation is made.

## Defect 5 — P3 two incompatible protection vocabularies (`economics/management.py` vs `adapters/protection_invariants.py`)

**Defect.** M3-03's `ProtectionState` had `PROTECTED/UNPROTECTED/UNKNOWN/REJECTED/GAP`;
M0-05's checker had the same five **plus `PARTIAL`** (live stop quantity ≠ filled position
quantity) and `evaluate_management` accepted only its own type. The one state that must force a
resize/`EXIT` could not be carried from producer to consumer: the checker's `PARTIAL` verdict was
not even expressible to management, and a second enum meant an `is` comparison across the boundary
was false.

**Fix.** One canonical enum in `src/v8_next/domain/protection.py` (no imports of its own, so no
layering edge in either direction). `v8_next.adapters.protection_invariants` and
`v8_next.economics.management` both import and re-export it; neither defines a copy. `management`
cannot import the adapter (`tests/test_position_management.py` pins the AST-level ban on
`v8_next.adapters` imports), so the domain module is the only correct authority. `PARTIAL` is
handled fail-closed: `_PROTECTION_REASONS[PARTIAL] = (RISK_DENIED,)`, it is not `PROTECTED`, it
forces `EXIT` + `fail_closed=True` + `admission_ok=False`, and the protective-add proposal carries
`RISK_DENIED` alongside `UNPROTECTED`/`REJECTED`. The module docstring, which listed only four
fail-closed states, now names `PARTIAL`.

**Tests** (`tests/test_position_management.py`):

| test | pre-fix |
|---|---|
| `test_protection_state_is_one_shared_enum_not_two_vocabularies` (`is` across management, domain, adapter) | FAIL (two distinct classes) |
| `test_partial_protection_is_fail_closed_never_hold_never_protected` | FAIL (no `PARTIAL` member / `KeyError`) |
| `test_a_partial_verdict_from_the_native_checker_needs_no_translation` (real `BracketInvariantChecker` → `PARTIAL` → `evaluate_management`) | FAIL |
| `test_unreadable_or_refused_protection_is_never_reported_protected[PARTIAL]` (existing parametrisation, extended) | FAIL |

## Verification (this worktree)

```
$ /Users/hootie/src/v9/.venv/bin/python -m pytest -q \
    tests/test_shadow_parity.py tests/test_universe_feasibility.py \
    tests/test_position_management.py tests/test_protection_invariants.py
162 passed in 0.61s

$ /Users/hootie/src/v9/.venv/bin/python -m ruff check src/v8_next tests/test_shadow_parity.py \
    tests/test_universe_feasibility.py tests/test_position_management.py
All checks passed!

$ /Users/hootie/src/v9/.venv/bin/python -m ruff format --check <touched files>
all formatted except src/v8_next/adapters/protection_invariants.py, which already fails the format
check at HEAD (pre-existing debt; not reformatted here to avoid unrelated churn)

$ /Users/hootie/src/v9/.venv/bin/python -m mypy src/v8_next
Success: no issues found in 244 source files
```

Full fast suite NOT_RUN (worker protocol; the director runs it once per wave).

## Residual risks

- `NativeSession` is a protocol. Zero native mutation is proven against an injected sink double, not
  against a running paper engine (`NOT_RUN`).
- `RecordingNativeSession` digests its own call log. A production session must digest the engine's
  real order/position state; if it hashed something narrower, the gate would be weaker than it
  looks even though it can fail.
- Only `FRESH` + `OK` is usable. A belief with `FRESH` status and non-`OK` quality is treated as
  stale; `MarketBeliefV1` already forbids that combination, so this is a belt-and-braces branch.
- `PARTIAL` contributes `RISK_DENIED` only; it does not quantify the uncovered size, so the
  protective-add proposal restores the *thesis* stop for the whole quantity rather than the delta.
- The new `v8_next.domain.protection` module is an addition to the domain package; `docs/**` and
  `AGENTS.md` were not updated (not writable for this ticket). Any architecture map that enumerates
  domain modules needs this one line added by the integration owner.
- `M0-05.md` / `M3-03.md` evidence files describe the enum as local to those modules; they are not
  owned by this ticket and were not edited. The shared-enum change is recorded here and in the
  modules' docstrings.
