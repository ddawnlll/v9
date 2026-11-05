# Advisory position-thesis bridge — implementation fixture, not native integration

Base inspection: `d0430a64ebdeb44545115826e06a2e00caa36bea`, RFC 1.4.1;
implementation `src/v8_next/evaluation/thesis_bridge.py`, tests
`tests/test_thesis_bridge.py`. This module is simulation-only and carries no
order submission, persistence, venue connection or execution authority.

`capture_entry` takes a caller-bound immutable decision JSON, belief and expert
projections, policy version, native fill identity and observed stop/target
records. It checks PIT ordering, freshness, identity bindings, a single
completed fill, linear USDT multiplier, opposite reduce-only sides, ACCEPTED
status, full quantity, distinct IDs and bracket geometry. It records all the
inputs in a frozen receipt, hashes the complete bundle and constructs the
existing immutable `PositionThesis`; original risk is fill quantity × contract
multiplier × initial stop distance. Caller provides the target horizon,
invalidation rules and freshness limits: this module invents no strategy
policy. Multiple fills, scale-ins and non-linear or non-USDT contracts are
explicitly unsupported, rather than approximated.

`evaluate_snapshot` binds the current native position and instrument, checks
available-at time and freshness of evidence and stop observations, verifies
reported stop identity, status, quantity, reduce-only side and no widening past
the original stop, then calls existing `evaluate_position` and
`evaluate_management`. Results are advisory-only. A missing, stale, widened or
unconfirmed stop cannot be called protected or admitted; unknown venue status
also cannot establish protection. Existing management may suggest a break-even
proposal, but this bridge cannot approve or execute one. The entry's original
risk and actual fill remain unchanged.

This is a **caller-supplied projection**, not an independent verifier of venue
or engine event authenticity. An integration adapter must first bind the
receipt to actual event IDs, distinguish event and availability times,
reconcile callbacks and lifecycle/partial fills, persist evidence with restart
idempotency, check risk and native protection independently, then choose whether
to call this advisory. A supplied `simulation_only` flag, ACCEPTED label or
order ID is not proof of a venue ACK. The current strategy does not call the
bridge. No native management order, amendment, actual target change, realized
P&L, or management replay is claimed here. An actual native event trace and
independent end-to-end audit remain **NOT_RUN**.

Executed in the isolated worktree:

```
uv run --locked --extra dev pytest -q tests/test_thesis_bridge.py
# 14 passed
uv run --locked --extra dev ruff check src/v8_next/evaluation/thesis_bridge.py tests/test_thesis_bridge.py
# All checks passed!
uv run --locked --extra dev mypy src/v8_next/evaluation/thesis_bridge.py
# Success: no issues found in 1 source file
```

Fixtures use hand-constructed values solely to check contract mechanics. No
market profitability or native execution result is inferred from them.
