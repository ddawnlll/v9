"""The reconciliation outcome surface — port of ``v8-core/src/analysis/outcome.rs``
(issue #121; the ten RECONCILE fields of the frozen oracle ``tools/regret.py``, FT010).

Reconciliation compares **values**, not identities: ``candidate_id`` and ``action_id``
are bit-encoded identity strings and are excluded from the comparison
(PARITY_AND_IDENTITY_SPEC §3). The ten compared fields are frozen:

* exact  : ``endpoint``, ``label_status``, ``horizon_bars``, ``ambiguous_bars``
* float  : ``net_r``, ``entry_price``, ``risk_unit_price``, ``mae_r``, ``mfe_r``,
           ``market_move_r`` — compared with ``|a - b| <= RECONCILE_TOLERANCE``

``label_available_time`` is carried by a replay outcome but is **not** compared (FT010
excluded field), so a surface can never smuggle it into the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RECONCILE_TOLERANCE: float = 1e-12

RECONCILE_EXACT_FIELDS: tuple[str, ...] = (
    "endpoint",
    "label_status",
    "horizon_bars",
    "ambiguous_bars",
)

RECONCILE_FLOAT_FIELDS: tuple[str, ...] = (
    "net_r",
    "entry_price",
    "risk_unit_price",
    "mae_r",
    "mfe_r",
    "market_move_r",
)

RECONCILE_FIELD_COUNT: int = 10

#: The one replay field reconciliation deliberately does not compare (FT010).
RECONCILE_EXCLUDED_FIELDS: tuple[str, ...] = ("label_available_time",)

#: Every field a surface must carry, in the canonical order.
SURFACE_FIELDS: tuple[str, ...] = (
    "candidate_id",
    "action_id",
    *RECONCILE_EXACT_FIELDS,
    *RECONCILE_FLOAT_FIELDS,
)


@dataclass(frozen=True)
class OutcomeSurface:
    """One replayed action projected onto the ten compared fields plus its identity."""

    candidate_id: str
    action_id: str
    endpoint: str
    label_status: str
    horizon_bars: int
    ambiguous_bars: int
    net_r: float
    entry_price: float
    risk_unit_price: float
    mae_r: float
    mfe_r: float
    market_move_r: float

    def field_values(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in SURFACE_FIELDS}

    def values_match(self, other: "OutcomeSurface") -> bool:
        """True iff the ten value fields agree; identity is ignored (PARITY_AND_IDENTITY_SPEC §3).

        Exact fields compare with strict equality, float fields within
        ``RECONCILE_TOLERANCE``. Mirrors ``tools/regret.py:reconcile_actual_actions``.
        """
        for name in RECONCILE_EXACT_FIELDS:
            if getattr(self, name) != getattr(other, name):
                return False
        for name in RECONCILE_FLOAT_FIELDS:
            if abs(getattr(self, name) - getattr(other, name)) > RECONCILE_TOLERANCE:
                return False
        return True

    def mismatched_field(self, other: "OutcomeSurface") -> str | None:
        """The first non-agreeing field name, in canonical order, or ``None``.

        The reconcile plane needs the *field* that diverged, not only that something did
        (``reconcile.rs`` records ``field_mismatch`` as the mismatch reason).
        """
        for name in RECONCILE_EXACT_FIELDS + RECONCILE_FLOAT_FIELDS:
            left, right = getattr(self, name), getattr(other, name)
            if name in RECONCILE_EXACT_FIELDS:
                if left != right:
                    return name
            elif abs(left - right) > RECONCILE_TOLERANCE:
                return name
        return None

    def as_dict(self) -> dict[str, Any]:
        values = self.field_values()
        return {
            **values,
            "fields_compared": RECONCILE_FIELD_COUNT,
            "fields_excluded": list(RECONCILE_EXCLUDED_FIELDS),
        }


def reconcile_surface(outcome: Any, *, candidate_id: str, action_id: str) -> OutcomeSurface:
    """Project a replay outcome onto the surface.

    ``outcome`` is any object exposing the ten fields (the ported replay campaign does).
    A missing field is a hard failure — never a zero placeholder — because a fabricated
    reconciliation field would make every comparison agree or disagree for the wrong reason.
    """
    missing = [
        name
        for name in (*RECONCILE_EXACT_FIELDS, *RECONCILE_FLOAT_FIELDS)
        if not hasattr(outcome, name)
    ]
    if missing:
        raise ValueError(f"outcome is missing reconcile fields: {sorted(missing)}")
    return OutcomeSurface(
        candidate_id=candidate_id,
        action_id=action_id,
        **{
            name: getattr(outcome, name)
            for name in (*RECONCILE_EXACT_FIELDS, *RECONCILE_FLOAT_FIELDS)
        },
    )
