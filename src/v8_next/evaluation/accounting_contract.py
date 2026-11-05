"""Fail-closed comparison of explicitly declared economic replay contracts.

This is a comparison gate, not an accounting ledger or an execution engine.
Unknown inputs cannot establish equivalence; matching assumptions do not prove
that native cashflows, position identity, or marks were calculated correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class EconomicContract:
    """Assumptions needed before cross-run economic differences are meaningful.

    Values identify the *input model*, not outcomes (trade counts, actual fees,
    funding totals and exposure may differ as a result of an intervention).
    A missing value is unknown and blocks comparison, including against another
    missing value.
    """

    tape_sha256: str | None
    window: str | None
    instrument: str | None
    initial_balance: Decimal | None
    sizing_policy: str | None
    execution_profile: str | None
    maker_fee: Decimal | None
    taker_fee: Decimal | None
    funding_model: str | None
    mark_model: str | None
    capital_and_exposure_basis: str | None
    outcome_basis: str | None
    cutoff_policy: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {field.name: None if (value := getattr(self, field.name)) is None else str(value)
                for field in fields(self)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EconomicContract:
        values: dict[str, Any] = {}
        for field in fields(cls):
            value = payload.get(field.name)
            if value is not None:
                value = str(value)
                if field.name in {"initial_balance", "maker_fee", "taker_fee"}:
                    value = Decimal(value)
            values[field.name] = value
        return cls(**values)


@dataclass(frozen=True)
class ContractComparison:
    status: Literal["COMPARABLE_ASSUMPTIONS", "BLOCKED"]
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]


def compare_economic_contracts(
    left: EconomicContract, right: EconomicContract
) -> ContractComparison:
    """Compare assumptions; never treat unknown/blank assumptions as equal.

    This gate does not validate the source of a declaration or authorize using
    a cash-balance result as a marked-equity result.
    """
    missing: list[str] = []
    mismatched: list[str] = []
    for field in fields(EconomicContract):
        a = getattr(left, field.name)
        b = getattr(right, field.name)
        if any(
            value is None
            or (isinstance(value, str) and value.strip().upper() in {"", "UNKNOWN", "UNSPECIFIED"})
            or (isinstance(value, Decimal) and not value.is_finite())
            for value in (a, b)
        ):
            missing.append(field.name)
        elif a != b:
            mismatched.append(field.name)
    return ContractComparison(
        status="BLOCKED" if missing or mismatched else "COMPARABLE_ASSUMPTIONS",
        missing=tuple(missing),
        mismatched=tuple(mismatched),
    )
