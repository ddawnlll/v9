"""Optional per-expert call accounting using CPython profiling events.

Inclusive dispatcher wall time includes nested calls. Observer and dispatcher
self times exclude observed Python and C call events; unobservable native work
and profiler overhead remain in those values. Do not use as baseline timing.
"""
from __future__ import annotations

import statistics
import sys
import time
from types import FrameType
from typing import Any

from v8_next.experts.registry import (
    _DISPATCHERS,
    CANONICAL_28_EXPERTS,
    get_expert,
    observe_expert,
    observe_spec,
)


def _stats(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "count": len(values), "total_s": sum(values), "mean_s": statistics.mean(values),
        "p50_s": statistics.median(values), "p95_s": ordered[(95 * len(values) + 99) // 100 - 1],
        "max_s": ordered[-1],
    }


def _entry_expert(frame: FrameType) -> str:
    spec = frame.f_locals.get("spec")
    if spec is not None:
        return str(spec.expert_id)
    return str(frame.f_locals["expert_id"])


class ExpertTrace:
    def __init__(self) -> None:
        self.codes = {name: get_expert(name).observer_fn.__code__ for name in CANONICAL_28_EXPERTS}
        # The ensemble dispatches through one precomputed closure per expert
        # (observe_all_28); observe_spec(spec, ...) remains the reference for
        # the by-name path and observe_expert the public entry point. Track
        # all three, so the trace does not depend on which one the hot path
        # uses. Dispatcher closures carry their expert id as a real local
        # (`expert_id` default arg), so entry attribution is unchanged.
        self.entry_codes = {observe_spec.__code__, observe_expert.__code__} | {
            dispatch.__code__ for dispatch in _DISPATCHERS
        }
        self.samples: dict[str, list[tuple[float, float, float]]] = {
            name: [] for name in CANONICAL_28_EXPERTS
        }
        self.active: str | None = None
        self.stack: list[Any] = []
        self.start = 0.0
        self.last = 0.0
        self.root_self = 0.0
        self.observer_self = 0.0
        self.observer_seen = False

    def __enter__(self) -> ExpertTrace:
        if sys.getprofile() is not None:
            raise RuntimeError("cannot combine expert trace with an existing profiler")
        sys.setprofile(self._event)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        sys.setprofile(None)
        if exc_type is None and self.active is not None:
            raise RuntimeError("expert profiling ended with an active call")

    def _event(self, frame: FrameType, event: str, arg: Any) -> None:
        if self.active is None:
            if event == "call" and frame.f_code in self.entry_codes:
                name = _entry_expert(frame)
                if name not in self.codes:
                    raise RuntimeError(f"unexpected expert {name!r}")
                now = time.perf_counter()
                self.active = name
                self.start = self.last = now
                self.stack = [frame]
                self.root_self = self.observer_self = 0.0
                self.observer_seen = False
            return
        now = time.perf_counter()
        if len(self.stack) == 1:
            self.root_self += now - self.last
        if self.stack and isinstance(self.stack[-1], FrameType):
            top = self.stack[-1]
            if top.f_code is self.codes[self.active]:
                self.observer_self += now - self.last
        self.last = now
        if event == "call":
            self.stack.append(frame)
            if frame.f_code is self.codes[self.active]:
                self.observer_seen = True
        elif event == "c_call":
            self.stack.append(None)
        elif event in ("c_return", "c_exception"):
            if not self.stack or self.stack[-1] is not None:
                raise RuntimeError("unbalanced C profiling events")
            self.stack.pop()
        elif event == "return":
            if not self.stack or self.stack[-1] is not frame:
                raise RuntimeError("unbalanced Python profiling events")
            self.stack.pop()
            if not self.stack:
                if not self.observer_seen:
                    raise RuntimeError(f"observer not observed for {self.active}")
                self.samples[self.active].append((now - self.start, self.root_self, self.observer_self))
                self.active = None

    def results(self) -> dict[str, Any]:
        if any(not values for values in self.samples.values()):
            raise RuntimeError("one or more experts were not observed")
        return {
            name: {"inclusive_dispatch": _stats([v[0] for v in values]),
                   "dispatcher_self": _stats([v[1] for v in values]),
                   "observer_self": _stats([v[2] for v in values])}
            for name, values in self.samples.items()
        }
