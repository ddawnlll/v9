"""Optional line-boundary timing of the unchanged single-ensemble replay.

This uses Python 3.12 monitoring and must not be treated as an uninstrumented baseline.
Markers are resolved by exact source lines and fail closed if the implementation
changes; stage times are exclusive of each other but include nested calls.
"""
from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path
from typing import Any, Callable

MARKERS = (
    ("instrument_setup", "cfg = config or ExpertStrategyConfig()"),
    ("bar_conversion", "bars = ["),
    ("execution_profile", "profile = resolve_profile(execution_profile) if execution_profile is not None else None"),
    ("engine_init", "engine = BacktestEngine("),
    ("venue_setup", "engine.add_venue("),
    ("instrument_add", "engine.add_instrument(instrument)"),
    ("data_add", "engine.add_data(bars)"),
    ("strategy_setup", "source_map = {c.end_ns: c for c in candles}"),
    ("engine_run", "engine.run()"),
    ("economic_state", "account_state = economic_state(engine, ven, curr)"),
    ("report_import", "from v8_next.audit.native_telemetry import extract_native_reports"),
    ("native_reports", "native_reports = extract_native_reports(engine)"),
    ("fill_report", "fill_records, fill_report_type = native_fill_records(engine)"),
    ("result_assembly", "return {"),
    ("engine_dispose", "engine.dispose()"),
)


def _rss_bytes() -> int | None:
    try:
        import os

        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return None


class StageTrace:
    """Trace a single invocation; repeat calls or missing markers raise errors."""

    def __init__(self, function: Callable[..., Any]):
        source, first_line = inspect.getsourcelines(function)
        self.code = function.__code__
        self.lines: dict[int, str] = {}
        for name, marker in MARKERS:
            matches = [first_line + i for i, line in enumerate(source) if line.strip() == marker]
            if len(matches) != 1:
                raise ValueError(f"stage marker {name} expected once, found {len(matches)}")
            self.lines[matches[0]] = name
        self.starts: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.called = False

    def __enter__(self) -> StageTrace:
        self.tool_id = 5
        if sys.monitoring.get_tool(self.tool_id) is not None:
            raise RuntimeError("Python monitoring tool ID 5 is already in use")
        sys.monitoring.use_tool_id(self.tool_id, "v9-performance-stages")
        events = sys.monitoring.events
        sys.monitoring.register_callback(self.tool_id, events.PY_START, self._start)
        sys.monitoring.register_callback(self.tool_id, events.LINE, self._line)
        sys.monitoring.register_callback(self.tool_id, events.PY_RETURN, self._return)
        sys.monitoring.set_local_events(self.tool_id, self.code,
                                        events.PY_START | events.LINE | events.PY_RETURN)
        return self

    def _start(self, code: Any, offset: int) -> None:
        if self.called:
            raise RuntimeError("stage trace received multiple invocations")
        self.called = True
        self._mark("function_entry")

    def _line(self, code: Any, line_number: int) -> None:
        if line_number in self.lines:
            name = self.lines[line_number]
            if name not in self.seen:
                self.seen.add(name)
                self._mark(name)

    def _return(self, code: Any, offset: int, value: Any) -> None:
        self._mark("function_exit")

    def _mark(self, name: str) -> None:
        self.starts.append({"phase": name, "timestamp": time.perf_counter(), "rss_bytes": _rss_bytes()})

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        sys.monitoring.set_local_events(self.tool_id, self.code, 0)
        for event in (sys.monitoring.events.PY_START, sys.monitoring.events.LINE,
                      sys.monitoring.events.PY_RETURN):
            sys.monitoring.register_callback(self.tool_id, event, None)
        sys.monitoring.free_tool_id(self.tool_id)
        if exc_type is None:
            expected = ["function_entry", *(name for name, _ in MARKERS), "function_exit"]
            actual = [event["phase"] for event in self.starts]
            if actual != expected:
                raise RuntimeError(f"stage trace incomplete or out of order: {actual}")

    def durations(self) -> list[dict[str, Any]]:
        return [
            {"phase": start["phase"], "wall_s": end["timestamp"] - start["timestamp"],
             "rss_start_bytes": start["rss_bytes"], "rss_end_bytes": end["rss_bytes"]}
            for start, end in zip(self.starts, self.starts[1:], strict=False)
        ]
