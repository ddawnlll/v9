"""Canonical serialisation shared by the parity capture and diff harnesses.

The parity contract is byte-identity of *decision-relevant* output. That means
canonicalising away the things that are legitimately different between two runs
of the same replay (wall-clock audit stamps, object identity, mapping order)
while keeping everything a reviewer would treat as evidence: decision ordering,
stance contents, fill rows, opportunity lifecycle and audit fields.

Nothing here decides what is comparable; it only makes comparable things
serialisable. Sections that are genuinely volatile are listed in
``VOLATILE_KEYS`` and reported by the diff rather than silently dropped.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

# Keys that are run identity, not replay content:
#   recorded_time_ns -- wall-clock stamp of when the audit line was written;
#   init_id          -- Nautilus' per-order UUID, freshly generated each run and
#                       referenced by nothing downstream (venue_order_id and
#                       order_list_id carry the deterministic venue identity);
#   event_id         -- the same, for native order/fill/position event rows.
# Two identical replays must differ in exactly these; everything else must not.
# The diff reports how many such keys it had to ignore, so this list cannot grow
# silently and swallow a real divergence.
VOLATILE_KEYS = frozenset({"recorded_time_ns", "init_id", "event_id"})


def canonical(value: Any, *, drop_volatile: bool = True) -> Any:
    """Return a JSON-compatible value with deterministic ordering and types."""
    if isinstance(value, enum.Enum):
        return canonical(value.value, drop_volatile=drop_volatile)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        items = {
            str(key): canonical(item, drop_volatile=drop_volatile)
            for key, item in value.items()
            if not (drop_volatile and str(key) in VOLATILE_KEYS)
        }
        return {key: items[key] for key in sorted(items)}
    if isinstance(value, (list, tuple)):
        return [canonical(item, drop_volatile=drop_volatile) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (canonical(item, drop_volatile=drop_volatile) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    # Pydantic models keep field order from the model definition: use it.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return canonical(dump(mode="json"), drop_volatile=drop_volatile)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return canonical(dataclasses.asdict(value), drop_volatile=drop_volatile)
    # numpy scalars expose .item(); do not import numpy to find out.
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, type):
        try:
            return canonical(item(), drop_volatile=drop_volatile)
        except Exception:  # pragma: no cover - exotic scalar
            pass
    return f"<{type(value).__module__}.{type(value).__qualname__}:{value!r}>"


def dumps(value: Any, *, drop_volatile: bool = True) -> str:
    """Canonical JSON text: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        canonical(value, drop_volatile=drop_volatile),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def digest(value: Any, *, drop_volatile: bool = True) -> str:
    return hashlib.sha256(dumps(value, drop_volatile=drop_volatile).encode("utf-8")).hexdigest()


def digest_chunks(values: list[Any] | tuple[Any, ...], chunk: int) -> list[str]:
    """Digest a sequence in fixed-size chunks so a divergence can be localised."""
    out: list[str] = []
    for start in range(0, len(values), chunk):
        out.append(digest(list(values[start : start + chunk])))
    return out


def first_difference(left: Any, right: Any, path: str = "") -> str | None:
    """Bounded, human-readable description of the first structural difference."""
    if type(left) is not type(right) and not (
        isinstance(left, (int, float)) and isinstance(right, (int, float))
    ):
        return f"{path or '<root>'}: type {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        left_keys, right_keys = set(left), set(right)
        if left_keys != right_keys:
            missing = sorted(left_keys ^ right_keys)
            return f"{path or '<root>'}: key set differs ({', '.join(missing[:8])})"
        for key in sorted(left):
            found = first_difference(left[key], right[key], f"{path}.{key}" if path else str(key))
            if found is not None:
                return found
        return None
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return f"{path or '<root>'}: length {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            found = first_difference(a, b, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if left != right:
        return f"{path or '<root>'}: {left!r} != {right!r}"
    return None


def count_volatile(value: Any) -> dict[str, int]:
    """How many run-identity keys a capture had to ignore, per key.

    Recorded in the manifest so the volatile set cannot grow unnoticed: a change
    in these counts is a structural change in the evidence, not a tolerance.
    """
    counts: dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key) in VOLATILE_KEYS:
                    counts[str(key)] = counts.get(str(key), 0) + 1
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return counts
