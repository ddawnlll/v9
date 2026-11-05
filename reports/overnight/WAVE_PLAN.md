# V9 Overnight Factory — Wave Plan

Coordination doc. Machine state: `state.json`, `issues.json`, `waves.jsonl`,
`test-results.jsonl`, `blockers.json`.

## Baseline facts (measured 2026-11-05)

- Repo `ddawnlll/v9`, branch `main`, HEAD `0b69fc4`; no open PRs; 36 open issues (#2–#37).
- Package `src/v8_next/` (63,415 LOC, 209 modules), 193 test files, pytest fast loop is
  default (`-m "not slow"`), `uv` + `.venv` present.
- Host: 18 GiB RAM, 11 CPU — product target is 4 GiB but the host can run 4–5 workers.
- Missing V9 concepts (greenfield): `MarketBelief`, `TraderDecision`, `EntryIntent`,
  `PositionThesis`, `IntentKind`. Existing: `OpportunityBook`, `OpportunityStatus`,
  `audit/{lineage,cashflow,reconciliation,trade_metrics}`, `economics/{decisions,controller,allocation}`,
  `experts/registry` (28 experts), `telemetry/belief`.

## Issue dependency DAG (edges = hard prerequisites)

```
M0-01 (#2) ──┬── M0-02 (#3) ──┬── M0-03 (#4) ──┬── M0-06 (#7)
             │                │                └── M0-07 (#8)
             │                └── M0-05 (#6) ── M0-04 (#5)
             └── M0-08 (#9)
M1-01 (#10) ── M1-02 (#11) ── M1-03 (#12) ── M1-04 (#13) ── M1-05 (#14) ── M1-06 (#15)
M2-01 (#16) ; M2-02 (#17) ── M2-03 (#18) ; M2-04 (#19) ; M2-05 (#20) ; M2-06 (#21)
M3-01 (#22) ; M3-02 (#23) ; M3-03 (#24) ; M3-04 (#25) ; M3-05 (#26) ; M3-06 (#27) ; M3-07 (#28)
M4-01 (#29) ── M4-02 (#30) ── M4-03 (#31) ── M4-04 (#32) ── M4-05 (#33) ── M4-06 (#34)
F-01 (#35) ── F-02 (#36) ── F-03 (#37)
```

## Wave 1 (parallel implementation, disjoint file ownership)

| Worker | Issues | Files owned |
|---|---|---|
| W1 | M1-01, M1-02 | `src/v8_next/domain/observation.py`, `src/v8_next/domain/horizons.py`, `tests/test_pit_observation.py`, `tests/test_multi_horizon.py`, `reports/overnight/evidence/M1-01.md`, `…/M1-02.md` |
| W2 | M2-02, M2-03 | `src/v8_next/domain/decision.py`, `src/v8_next/economics/legality.py`, `tests/test_decision_contracts.py`, `tests/test_legal_actions.py`, evidence `M2-02.md`, `M2-03.md` |
| W3 | M0-02, M0-03 | `src/v8_next/audit/oracle_two_campaign.py`, `src/v8_next/audit/lineage.py` (extend), `tests/test_accounting_oracle_two_campaign.py`, `tests/test_lineage_fail_closed.py`, evidence `M0-02.md`, `M0-03.md` |
| W4 | F-01 | `src/v8_next/factory/__init__.py`, `src/v8_next/factory/queue.py`, `src/v8_next/factory/lease.py`, `tests/test_factory_queue_lease.py`, evidence `F-01.md` |
| W5 | M0-07 | `src/v8_next/audit/trade_metrics.py` (extend), `src/v8_next/audit/verdicts.py`, `tests/test_same_bar_metrics_fail_closed.py`, evidence `M0-07.md` |

No worker may edit `pyproject.toml`, `README.md`, `AGENTS.md`, `docs/**`, existing test files, or files owned by
another worker. No pushes, no merges, no `main` writes.

## Shared contracts (authoritative for later waves)

Later waves MUST import these names rather than redefining them. If a change is needed, the
integration step owns it (no side edits by workers).

### M1-01 — `src/v8_next/domain/observation.py`

```python
class QualityState(StrEnum):      # OK | STALE | MISSING | CONTRADICTORY | AMENDED_FUTURE | UNKNOWN
class ObservationKind(StrEnum):   # BAR | FUNDING | OPEN_INTEREST | MARK | QUOTE | LIQUIDATION
class Observation(BaseModel, frozen):
    instrument_id: str; kind: ObservationKind; event_time: int; available_at: int;
    received_at: int; published_at: int | None; source: str; sequence: int;
    quality: QualityState; payload: dict[str, Any]
class ObservationView:            # PIT cut; immutable
    cut_time_ns: int
    max_input_available_at: int
    def bars(instrument_id) -> tuple[Candle, ...]
    def quality(instrument_id) -> QualityState
    def observation_ids() -> tuple[str, ...]
class PitViolationError(ValueError)   # future/amended/contradictory input
def build_view(observations, *, cut_time_ns) -> ObservationView
```

### M1-02 — `src/v8_next/domain/horizons.py`

```python
class Timeframe(StrEnum):         # M15 | H1 | H4
class HorizonAlignment(BaseModel, frozen):
    timeframe: Timeframe; as_of_ns: int; complete: bool; quality: QualityState
class HorizonContext(BaseModel, frozen):
    instrument_id: str; decision_time_ns: int; alignments: dict[Timeframe, HorizonAlignment]
def assemble_context(views: Mapping[Timeframe, ObservationView], *, instrument_id: str,
                     decision_time_ns: int) -> HorizonContext
```

### M2-02 — `src/v8_next/domain/decision.py`

```python
class DecisionAction(StrEnum):    # WAIT | ENTER | HOLD | REDUCE | EXIT | ABSTAIN
class IntentKind(StrEnum):        # NONE | MARKET_ENTRY | LIMIT_ENTRY | PROTECTIVE | REDUCE | EXIT | CANCEL
class OpportunityLifecycle(StrEnum):  # WATCHING | ARMED | ADMITTED | INVALIDATED | EXPIRED
class VenueOrderStatus(StrEnum):  # SUBMITTED | ACCEPTED | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | UNKNOWN
class ReasonCode(StrEnum):        # stable string codes, e.g. BELIEF_STALE, VENUE_UNKNOWN, RISK_DENIED
class TraderDecision(BaseModel, frozen):  # advisory only; never a venue order
    decision_id: str; decision_time_ns: int; instrument_id: str;
    opportunity_id: str | None; action: DecisionAction; intent_kind: IntentKind;
    reason_codes: tuple[ReasonCode, ...]; policy_version: str; belief_version_hash: str | None;
    advisory_only: bool = True
```

`NONE` intent is legal only for `WAIT`/`ABSTAIN`; every non-`NONE` intent requires `ENTER`/`HOLD`/
`REDUCE`/`EXIT`; `TraderDecision` construction must reject mixed states.

### M2-03 — `src/v8_next/economics/legality.py`

```python
class PositionPresence(StrEnum):  # NONE | OPEN | PENDING_INTENT | UNCERTAIN
class LegalActionTable:           # table-driven
    def legal(position: PositionPresence, *, venue: VenueOrderStatus, stale: bool,
              armed_opportunity: bool) -> frozenset[DecisionAction]
def resolve_conflict(candidates: Sequence[DecisionAction]) -> DecisionAction  # deterministic precedence
```

Fail-closed: `VenueOrderStatus.UNKNOWN` ⇒ only `WAIT`/`ABSTAIN`; stale inputs ⇒ no `ENTER`.

## Validation tiers used by the director

- T0: `ruff check`, `mypy src` (scoped to changed modules where practical).
- T1: union of the changed tests, one runner per wave.
- T2: full fast suite (`pytest -q`) on the integration worktree.
- T3: native replay only for execution-semantics changes (none scheduled in wave 1).
- T4: reserved for milestone candidates.
