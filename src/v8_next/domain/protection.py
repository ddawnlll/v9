"""Canonical vocabulary for "is this position actually protected?".

Two consumers read a native protection verdict: the native-agnostic checker
(:mod:`v8_next.adapters.protection_invariants`) *produces* one from the observed
order/position event log, and recurrent position management
(:mod:`v8_next.economics.management`) *consumes* one to decide a fail-closed
advisory. If each kept its own enum, the single state that must force a resize --
``PARTIAL``, a live stop whose quantity does not cover the filled position --
could not be carried from producer to consumer without translation.

The enum therefore lives here, in the domain, and both modules import and
re-export this one object. There is exactly one type: a producer's verdict is a
consumer's input with no adapter dependency in either direction.

Only ``PROTECTED`` may ever be read as protected. Every other member is a
fail-closed state: it can force ``EXIT``/a protective resize, never ``HOLD``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ProtectionState",
]


class ProtectionState(StrEnum):
    """The protection verdict for one native position."""

    #: Open position, correctly sized, acknowledged, reduce-only protection.
    PROTECTED = "PROTECTED"
    #: Open position with no live loss protection (or a flat position).
    UNPROTECTED = "UNPROTECTED"
    #: Protection exists but its quantity does not equal the filled position quantity.
    PARTIAL = "PARTIAL"
    #: A protective order was rejected; nothing rejected may be read as protection.
    REJECTED = "REJECTED"
    #: A protective order, its side, its price or the position itself is unreadable.
    UNKNOWN = "UNKNOWN"
    #: A protective order is live but has no venue acknowledgement.
    GAP = "GAP"
