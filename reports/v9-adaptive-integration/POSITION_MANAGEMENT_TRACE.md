# Position management trace

Native `BacktestEngine` events. Every tuple below comes from `lifecycle_events` / `management_log`
on the replay, not from a reconstructed narrative.

## Representative campaign (400-bar trace, position id `…AdaptiveSimStrategy-000`)

| event time (ns) | event | detail |
|---:|---|---|
| 1736175600000000000 | `THESIS_CREATED` | LONG, entry 100702.10, initial stop 97681.04, risk 27.189567 USDT, target 106744.226, `v9.position-lifecycle.v1` |
| 1736179200000000000 | `STOP_AMEND_REQUESTED` | `S-bbe44e7f7513-S`, 97681.04 → 100802.86, mode `NATIVE_MODIFY` |
| 1736179200000000000 | `STOP_AMEND_ACKED` | native `modify_order` accepted; same order identity; stop now above entry |
| 1736431200000000000 | `THESIS_CREATED` | next campaign (NETTING position id reused) |
| 1736758800000000000 | `STOP_AMEND_ACKED` | short campaign stop 95934.50 → 93510.44 |

The amendment is submitted as a native `modify_order` on the live stop. The order id does not
change, so the position never has two stops covering the same quantity and it never loses coverage
between a cancel and a replace. A wider stop is refused before submission. An unacknowledged stop is
never amended.

## Full-slice counters (2,160 bars)

| Metric | Value |
|---|---:|
| Closed campaigns / theses created | 23 |
| Management reassessments | 401 |
| `STOP_AMEND_REQUESTED` | 17 |
| `STOP_AMEND_ACKED` | 17 |
| `STOP_AMEND_REJECTED` | 0 |
| `TARGET_AMEND_REQUESTED` | 0 |
| `THESIS_EXIT` (thesis-driven) | 0 |
| Reassessment actions | `HOLD` 401 |
| Thesis status | `THESIS_INTACT` 401 |
| Protection state seen | `PROTECTED` 401 |
| Protection violations | 0 |

Every open-position bar was reassessed. All 23 campaigns ended through the existing native bracket
or portfolio path (target/stop/reduce/exit), not through a thesis-driven exit, because no
invalidation rule fired on this tape.

## Policy contracts

- Initial stop: `StopPolicyV1` (`v9.stop.structural.v1`) — prior 24-bar low/high, distance clamped to
  [0.5%, 3%] of entry, bracket-percentage fallback without history.
- Target: `TargetPolicyV1` (`v9.target.structural.v1`) — 2R baseline, extended to a H4 structural
  objective when it offers ≥ 1.5R; later extensions are one-way, at least 0.5R beyond the current
  target, and capped at 2.
- Invalidation: price stop, protection gap, expert contradiction ≥ 0.8, time limit 96 bars.
- Break-even amendment: fee-inclusive level, only when it tightens and stays on the safe side of the
  mark (`economics/management.py::_break_even_proposal`).

## Not demonstrated on this tape

`TARGET_AMEND_REQUESTED` and `THESIS_EXIT` counters are zero. Their mechanics are covered by
deterministic tests (`tests/test_position_lifecycle.py`, `tests/test_sim_protection_audit.py`), but
this run provides no native observation of either. No target amendment or thesis exit is claimed
from the backtest.
