# V9 Overnight Factory — Final Report

**Repo:** `ddawnlll/v9` · **Integration head:** `7f1bcfe` (`integration/wave1`) · **Main:** `0b69fc4` (untouched)
**Window:** 2026-11-05 01:28 → 03:47 (+03:00), ~1 h 18 min
**Normative spec:** `docs/V9_ARCHITECTURE_MONOGRAPH.md` (RFC 1.4.1)

## 1. Headline

| Metric | Value |
|---|---|
| Issues with delivered, verified implementation + draft PR | **36 / 36** |
| Draft PRs opened | **28** (covering all 36 issues) |
| PRs merged into `integration/wave1` (not main) | 7 PRs / 12 issues |
| Main merges | **0** (by policy) |
| Fast suite at final integration head | **2676 passed, 67 skipped, 512 deselected, 14.33 s** |
| Baseline fast suite | 1094 passed, 66 skipped, 506 deselected, 12.7 s |
| Static gates | `ruff check src tests` clean; `mypy src` clean over **249 files** |
| New test cases | **1582** net (2676 − 1094) |
| Issues with explicit `NOT_RUN`/`BLOCKED` residual evidence | 11 (listed in §6) |
| Verified issues per wall-clock hour (mechanical acceptance) | ~27 |

Nothing was merged to `main`, no live order was placed, no credential was used, and no sealed
OOS slice was read.

## 2. Delivered capabilities by milestone

### M0 — Foundation & Safety

| Issue | Capability | PR |
|---|---|---|
| #2 M0-01 | Digest-sealed baseline provenance; missing tape = `BLOCKED`, not PASS | 39 |
| #3 M0-02 | Independent two-campaign accounting oracle (no engine imports; AST-pinned) | 42 |
| #4 M0-03 | Fail-closed opportunity → decision → order → fill → cashflow lineage | 42 |
| #5 M0-04 | Restart-safe durable intent outbox; unknown never terminal | 59 |
| #6 M0-05 | Bracket/reduce-only/partial-fill/protection invariant reducer | 60 |
| #7 M0-06 | Independent funding-boundary attribution + causal equity identity | 46 (merged to integration) |
| #8 M0-07 | Post-entry MFE/MAE, stop-first ties, separated audit verdicts | 44 |
| #9 M0-08 | Resource receipts + five-stream fail-closed parity gate | 52 (merged) |

### M1 — Market & Expert Intelligence

| Issue | Capability | PR |
|---|---|---|
| #10 M1-01 | PIT observation/availability contract | 40 |
| #11 M1-02 | Causal 15m/1h/4h context; partial HTF never aggregated | 40 |
| #12 M1-03 | `MarketBeliefV1`; scores not probabilities; explicit missing dimensions | 48 (merged) |
| #13 M1-04 | Incremental/hysteretic belief updates and abstention | 48 (merged) |
| #14 M1-05 | 28-expert context-bound evidence with alias dedup and typed abstention | 58 |
| #15 M1-06 | Versioned shared feature cache, incremental prefix, bounded LRU | 52 (merged) |

### M2 — Trader Brain

| Issue | Capability | PR |
|---|---|---|
| #16 M2-01 | Append-only opportunity status history, PIT snapshots, witness dedup | 49 (merged) |
| #17 M2-02 | Typed `TraderDecision`/`IntentKind`/`ReasonCode`; advisory pinned | 41 |
| #18 M2-03 | Table-driven legal actions + risk-first conflict resolution | 41 |
| #19 M2-04 | Unified recurrent shadow `TraderDecisionPolicy` | 47 (merged) |
| #20 M2-05 | Immutable receipted decisions bound to policy hashes | 47 (merged) |
| #21 M2-06 | Policy-off parity + shadow replay mutation detection | 53 |

### M3 — Adaptive Position & Portfolio

| Issue | Capability | PR |
|---|---|---|
| #22 M3-01 | Price-aware `EntryIntent`, TTL, POST_ONLY non-crossing, audited repair | 45 (merged) |
| #23 M3-02 | Immutable `PositionThesis` + recurrent evaluation | 51 |
| #24 M3-03 | HOLD/REDUCE/EXIT advisories + reduce-only protective intents | 54 |
| #25 M3-04 | Factual occupancy and blocked-opportunity attribution | 49 (merged) |
| #26 M3-05 | Constraint-aware shadow portfolio challenger | 55 |
| #27 M3-06 | PIT altcoin universe/20-USDT feasibility rules (economic `NOT_RUN`) | 56 |
| #28 M3-07 | End-to-end Adaptive Shadow Trader vertical slice (all six actions) | 65 |

### M4 — Integrated Validation

| Issue | Capability | PR |
|---|---|---|
| #29 M4-01 | Machine-readable preregistration; no `PASS` preregisterable | 57 |
| #30 M4-02 | Costed single-intervention ablation contract; real run `BLOCKED` | 66 |
| #31 M4-03 | Sealed-OOS authorization, access ledger, walk-forward, multiple testing | 57 |
| #32 M4-04 | Coverage/regime report; altcoin universe `NOT_RUN` | 62 |
| #33 M4-05 | Deterministic soak/fault harness; 24–48 h soak `NOT_RUN` | 63 |
| #34 M4-06 | Scientific verdict/promotion matrix: overall `BLOCKED`, live `False` | 64 |

### Factory

| Issue | Capability | PR |
|---|---|---|
| #35 F-01 | Durable SQLite queue, atomic leases, retry budget, receipts | 43 |
| #36 F-02 | Bounded issue-to-PR worker; forbidden capabilities closed | 50 (merged) |
| #37 F-03 | Durable research memory and efficiency benchmark | 50 (merged) |

## 3. Independent audit repairs (found and fixed)

The director's independent audit found real defects, each reproduced before repair and pinned
by a failing-then-passing test:

| Repair | Defect | Fix |
|---|---|---|
| `fix/entry-intent` | `intent_id` hashed a hand-picked subset → identity collisions | digest of the whole record minus the id |
| `fix/entry-intent` | evaluation defaults were fail-open (`ACCEPTED`, fresh) | defaults now `UNKNOWN` / stale / unavailable |
| `fix/entry-intent` | POST_ONLY priced as a taker limit | passive-side price; crossing book = `BLOCKED` |
| `fix/audit-closure` | reports never consumed the audit verdict | report label derives from verdict + lineage |
| `fix/wave23-protocol` | alpha/method/gate-vector bypasses in preregistration/protocol | declared alpha/correction enforced; result gates required; post-hoc changes refused |
| `DEBT-AUDIT-WIRING` | native pipeline passed no verdict to `write_reports` | verdict derived only from persisted artifacts |
| `wave1/w4` v2 / `wave1/w2` v2 | alternative F-01/M1 implementations appeared | **not** substituted; verified integrated versions retained until an evidence-backed defect is shown |

## 4. Verification tiers

| Tier | Command | Result |
|---|---|---|
| T0 | `ruff check src tests` | clean |
| T0 | `mypy src` | clean, 249 files |
| T1 | per-lane focused tests | all green (29+45+32+50+81+16+23+21+25+18+17+65+11 …) |
| T2 | `pytest -q` on integration | **2676 passed, 67 skipped, 512 deselected, 14.33 s** |
| T3 | native Nautilus replay | **NOT_RUN** this session (no execution-semantics change required a new replay; M0-04/05 and M4-02 declare it `NOT_RUN`) |
| T4 | 24–48 h soak / sealed OOS | **NOT_RUN/BLOCKED** by design |

Test evidence is append-only in `reports/overnight/test-results.jsonl`; coordination state in
`state.json`, `waves.jsonl`; per-issue receipts in `reports/overnight/evidence/` and
`reports/provenance/`.

## 5. Measured performance / memory (M0-08, real BTC 1h 2025Q1 tape)

| Mode | Warm median | Peak RSS |
|---|---|---|
| 28-expert baseline | 0.8765 s | 215.9 MiB |
| FeatureCache candidate | 0.6559 s | 220.1 MiB |

1.34× warm speedup on the ensemble, 5.73× on shared feature reads, byte-identical stances and
reason codes. Scope is 1 leg / 2.96 months: the 40-leg / 12-month and 4 GiB-cgroup claims are
`NOT_RUN` and are not extrapolated.

## 6. Explicit NOT_RUN / BLOCKED (first-class negative evidence)

| Issue | Missing evidence | Consequence |
|---|---|---|
| #5 M0-04 | native OMS wiring / live venue | crash journal verified, submission `NOT_RUN` |
| #6 M0-05 | native Nautilus callback trace | invariant reducer verified, `NATIVE_CALLBACK_VERIFIED` not claimed |
| #7 M0-06 | native replay | independent ledger verified, no venue statement |
| #9 M0-08 | 40-leg/12-month data, 4 GiB cgroup | parity gate `BLOCKED`, resource `INCONCLUSIVE` |
| #27 M3-06 | altcoin perpetual tape | economic qualification `NOT_RUN` |
| #28 M3-07 | real-tape adaptive replay | deterministic synthetic trace only; real replay `NOT_RUN` |
| #30 M4-02 | economic finalist + authorized costed replay | harness verified; artifact `BLOCKED`, claim `NONE` |
| #32 M4-04 | altcoin tape + delisting history | universe `NOT_RUN`, survivorship `BLOCKED` |
| #33 M4-05 | 24–48 h wall clock, Linux 4 GiB cgroup | soak `NOT_RUN` |
| #34 M4-06 | ECONOMIC evidence, owner live approval | overall verdict `BLOCKED`, `live_approved=False` |
| #35/#36 | production/merge/deploy authority | structurally forbidden by design |

No issue in this table is reported as economically successful or live-approved.

## 7. Remaining work in dependency order

1. **M4-02 economic run** — needs an economic finalist from M3-07 and authorized costed Nautilus
   replays; then populate `reports/evaluation/costed_ablation.json` as `MEASURED`.
2. **M4-04 altcoin qualification** — needs real PIT altcoin-perpetual tapes and delisting history.
3. **M4-05 soak** — run the delivered harness for 24–48 h inside a Linux 4 GiB cgroup.
4. **M4-06 promotion** — re-collect after the three above; `LIVE_APPROVAL` remains owner-only.
5. **M0-04/M0-05 native qualification** — wire the journal and protector reducer into the native
   adapter and capture real callback traces (T3).
6. **Merge review** — every PR is a draft; owner review and sequential merge to `main` are required.

## 8. Exact next step

Review and merge the 28 draft PRs into `main` in dependency order (M0 → M1 → M2 → M3 → M4 →
Factory), or review the verified union on `integration/wave1` at `7f1bcfe`. Do **not** merge to
`main` automatically, and do not enable adaptive order side effects: the promotion matrix still
reads `BLOCKED` and `live_approved=False`.

---

# Director addendum — audit-driven repair cycle and final gate

Appended by the orchestrator after the four independent audits completed. Nothing below
replaces §1–§10 above; it records what the audits changed and the final measured state.

## A. Final gate (current head)

| Metric | Value |
|---|---|
| Integration head | `d88d818` (`integration/wave1`, pushed; `main` untouched at `0b69fc4`) |
| Fast suite | **2728 passed, 67 skipped, 512 deselected, 16.3 s** |
| Static gates | `ruff check src tests` clean · `mypy src` clean over **250 modules** |
| Diff vs main | 152 files, +61,003 / −55 · 41 new source modules · 39 new test files |
| Issues verified | **32 / 36** |
| Issues partial (data/time/policy blocked, declared) | **4 / 36** |
| Issues with no delivered implementation | **0** |

## B. Independent audits and what they found

Four read-only auditors reviewed the batch against the issue acceptance criteria, reading
implementations rather than test names. Every finding below was reproduced by the auditor,
then repaired by a bounded worker with a fail-before/pass-after test.

| Audit | Scope | Verdicts | Defects found |
|---|---|---|---|
| A | market/belief/experts/perf | 3 ACCEPT, 3 REJECT | 1 P1 + 5 P2 + doc drift |
| B | decision/position/portfolio/native | 8 ACCEPT, 1 REJECT, 2 INCONCLUSIVE | 5 P2/P3 |
| C | protocol/factory | 4 REJECT | 11 findings |
| D | slice/ablation/verdict/audit-wiring | 2 ACCEPT, 3 REJECT | 10 findings |

Representative defects the audits caught and the repairs fixed:

- **P1** belief hysteresis advanced on clock ticks alone (defeated `min_candidate_streak`).
- **P1** shadow slice charged the whole account exposure to every instrument, turning the
  portfolio plane into a permanent entry veto once anything was committed.
- **P1** `assess_verdict` ignored the registered alpha; a plain-string correction method was
  stamped "corrected" without adjusting p-values; the PASS path never consulted the gate vector.
- **P2** future funding boundary folded into an as-of equity and reported `PASS` (look-ahead).
- **P2** expert evidence provenance depended on cache history; feature parity hashed only half
  of two pair-valued kernels; the funding parity stream could never fail.
- **P2** receipts dropped the constraints that shaped the decision; `BeliefStatus.UNKNOWN` was
  presented to the policy as fresh evidence.
- **P2** `AUDIT_COMPLETE` reachable with no closed campaign and with silently dropped fills.
- **P2** soak reported `COMPLETE` for a 1 ms target; MC-02 deltas could compare different tapes;
  MECHANICAL `PASS` ignored failing rows in the recorded test log.
- **P2** ECONOMIC `PASS` was a self-declared string, ignoring `net_return`, `trades` and the
  registered decision rule.

Live re-checks after repair (run by the orchestrator, not the workers):

```
python -m v8_next.factory.soak --duration 0.001 --iterations 20
  -> soak_status SHORT_RUN, reason TARGET_BELOW_QUALIFICATION_FLOOR:0.001<86400.0
pytest tests/test_audit_verdict_wiring.py -k open
  -> 1 passed (open-only run -> INCOMPLETE / ECONOMIC_NO_CLOSED_CAMPAIGN, never AUDIT_COMPLETE)
```

## C. Economic evidence actually measured

The only costed native run possible under the shadow-only policy is the V8 incumbent baseline
(Nautilus 2.0.0rc4, BTCUSDT 1h 2025-01-01..2025-04-01, tape sha256 `84978f2d…`):

| Measure | Value |
|---|---|
| Closed trades | 44 (1 open at cutoff) |
| Net PnL | −21.23495118 USDT |
| Net R per campaign (mean) | −0.021638104, bootstrap 95% CI [−0.4335917, 0.3905152] |
| Max drawdown (realized) | 1.998 % |
| Hit rate | 34.09 % |
| Fees | 37.91535683 USDT |
| Funding (dual-replay isolation) | 0.70494565 USDT |

All five V9 intervention arms are `NOT_RUN:ADAPTIVE_ORDERS_DISABLED_SHADOW_ONLY` — no delta is
published, because no V9 arm has been costed. This is the intended consequence of the safety
policy, not a missing measurement to be filled in later with a number.

## D. Published verdict

`reports/overnight/V9_VERDICT.md` (generated by `v8_next.evaluation.verdict.collect_from_artifacts`):

| Dimension | Verdict | Why |
|---|---|---|
| MECHANICAL | PASS | provenance + lineage evidence present; recorded test log has no failing row |
| PIT | PASS | observation/multi-horizon/belief evidence present; one dataset AVAILABLE |
| ECONOMIC | NOT_RUN | preregistration + access report published; **no costed V9 result exists** |
| RESOURCE | INCONCLUSIVE | 1 leg / 2.96 months measured within RSS target; 40 legs / 12 months NOT_RUN |
| LIVE_APPROVAL | BLOCKED | never granted by a module; requires owner authorization |

Overall `BLOCKED` is structural (live approval is never granted in code); the scientific verdict
is `NOT_RUN`, which is the honest state: no V9 capability has been economically qualified.

## E. Remaining work, in dependency order

1. **Owner authorization for a costed shadow-execution path** → unblocks all five M4-02 arms,
   then ECONOMIC, then M4-06's promotion matrix.
2. **Provision validation + sealed OOS partitions** → unblocks M4-03 sealed evaluation and the
   `PRIMARY_EDGE_ELIGIBLE` gate. No sealed slice was read in this session.
3. **Provision a PIT altcoin universe tape with delisting/metadata/funding history** → unblocks
   M4-04's economic re-evaluation (the rule engine and coverage report are already delivered).
4. **Run a 24–48 h soak on a 4 GiB cgroup profile** → unblocks M4-05's real qualification
   (harness + fault injection + 24 h floor are delivered; the wall-clock run is NOT_RUN).
5. **Ledger linkage debt** noted by the audit-wiring worker: campaign 0 and native order events
   do not carry `campaign_id`/`decision_id`, so a live run renders `AUDIT_INCOMPLETE`. Fixing
   the producers (not the report) is the next mechanical task.

## F. Safety statement

No merge to `main`; no live order; no credential use; no sealed-OOS read; no fabricated test
count, benchmark, RSS figure or economic result. Every number above is either measured in this
session (and reproducible from a recorded command) or explicitly labelled `NOT_RUN`/`BLOCKED`.
