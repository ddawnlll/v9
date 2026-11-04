"""Declared cost model for the swing family's decision-plane replay.

One campaign's cost is split into separate, individually labelled fields so that a
number can never silently borrow authority from a neighbour:

* ``fee_cost_return`` — **measured**: the venue's published taker fee, applied by the
  replay on both legs.
* ``funding_cost_return`` — **measured**: real settlement rows read from the tape's
  ``funding`` channel, applied at the real settlement timestamps while the position is
  open, signed by direction. Coverage is checked against the venue grid derived from
  the declared funding interval; a partial span is reported as partial, never as
  complete and never as zero.
* ``slippage_modelled_return`` — **modelled**: a declared, data-supported rule (a number
  of ticks, or a fraction of the entry bar's range). It is an assumption and is named as
  one; it may never be reported as measured execution shortfall.
* ``execution_shortfall_measured_return`` — **measured only**. Without mark price or an
  order book this stays ``None`` with status ``MISSING_NO_MARK_OR_BOOK_DATA``. It is not
  filled with the modelled number.

A missing measurement is ``None`` plus a named status; it is never ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Literal

SWING_COST_MODEL_VERSION = "v87-swing-cost-v1"

#: Named, declared slippage rules. The provenance string says what the rule assumes so a
#: reader can falsify it from the tape.
SLIPPAGE_MODELS: dict[str, dict[str, Any]] = {
    "none": {
        "assumption": "no slippage; the comparison floor, not a claim about execution",
        "data_requirement": "none",
    },
    "ticks": {
        "assumption": "adverse move of a fixed number of instrument ticks per leg",
        "data_requirement": "instrument tick size (declared on the contract)",
    },
    "entry_bar_range_fraction": {
        "assumption": (
            "adverse move equal to a fixed fraction of the entry bar's high-low range per "
            "leg; the fraction is an assumption, the range is measured from the tape"
        ),
        "data_requirement": "1h OHLC bars (present in the tape)",
    },
}

EXECUTION_SHORTFALL_MISSING = "MISSING_NO_MARK_OR_BOOK_DATA"
FUNDING_APPLIED = "APPLIED"
FUNDING_MISSING_NO_ROWS = "MISSING_NO_FUNDING_ROWS_FOR_SPAN"
FUNDING_PARTIAL = "MISSING_PARTIAL_FUNDING_COVERAGE"
FUNDING_CONFLICTING = "INVALID_CONFLICTING_FUNDING_ROWS"
FUNDING_OFF_GRID = "MISSING_OFF_GRID_FUNDING_SETTLEMENTS"


@dataclass(frozen=True)
class SlippageModel:
    """A declared slippage rule plus the parameters it needs."""

    model: Literal["none", "ticks", "entry_bar_range_fraction"] = "none"
    ticks: int = 0
    tick_size: Decimal = Decimal("0")
    range_fraction: float = 0.0

    def __post_init__(self) -> None:
        if self.model not in SLIPPAGE_MODELS:
            raise ValueError(f"unknown slippage model {self.model!r}; known: {sorted(SLIPPAGE_MODELS)}")
        if self.model == "ticks" and (self.ticks < 0 or self.tick_size <= 0):
            raise ValueError("the ticks model needs a non-negative tick count and a positive tick size")
        if self.model == "entry_bar_range_fraction" and self.range_fraction < 0:
            raise ValueError("range_fraction must be non-negative")

    def provenance(self) -> dict[str, Any]:
        spec = SLIPPAGE_MODELS[self.model]
        return {
            "model": self.model,
            "assumption": spec["assumption"],
            "data_requirement": spec["data_requirement"],
            "ticks": self.ticks,
            "tick_size": str(self.tick_size),
            "range_fraction": self.range_fraction,
            "class": "MODELLED_ASSUMPTION_NOT_A_MEASUREMENT",
        }


@dataclass(frozen=True)
class FundingCharge:
    settlement_ms: int
    rate: Decimal
    #: return contribution on the position's notional: a long pays a positive rate
    return_contribution: float


@dataclass(frozen=True)
class FundingResult:
    charges: tuple[FundingCharge, ...]
    applied: int
    expected: int
    status: str
    interval_hours: float
    dropped_rows_in_tape: int = 0

    @property
    def cost_return(self) -> float:
        return round(sum(charge.return_contribution for charge in self.charges), 12)

    @property
    def complete(self) -> bool:
        return self.status == FUNDING_APPLIED


def base_symbol(symbol: str) -> str:
    """The base symbol both the funding channel and the venue-qualified ids reduce to.

    The tape's funding channel carries ``BTCUSDT`` while a candle's instrument id is
    venue-qualified (``BTCUSDT-PERP.BINANCE``); funding must not go unmatched because of
    that difference, and it must not be matched loosely either — hence one declared rule.
    """
    return symbol.split("-")[0].split(".")[0]


def _grid_points(entry_ns: int, exit_ns: int, interval_hours: float) -> int:
    """Settlements on the venue grid in ``(entry, exit]``.

    The venue settles on epoch-aligned instants (8h -> 00:00/08:00/16:00 UTC), not off
    the entry time, so the grid is derived from the timeline rather than from the entry.
    """
    if exit_ns <= entry_ns:
        return 0
    step = int(Decimal(str(interval_hours)) * Decimal(3_600_000_000_000))
    if step <= 0:
        return 0
    return exit_ns // step - entry_ns // step


def funding_for_position(
    funding_rows: Iterable[Any],
    *,
    instrument: str,
    direction: Literal["LONG", "SHORT"],
    entry_ns: int,
    exit_ns: int,
    interval_hours: float = 8.0,
    dropped_rows_in_tape: int = 0,
) -> FundingResult:
    """Real funding charges for one holding interval.

    A settlement applies when ``entry_ns < settlement_ns <= exit_ns``: the position is
    taken at the decision instant and held through the following settlements, so a
    settlement exactly at the open instant is not charged. A long pays a positive rate
    (``-rate``), a short receives it (``+rate``).
    """
    sign = 1.0 if direction == "LONG" else -1.0
    entry_ms = entry_ns // 1_000_000
    exit_ms = exit_ns // 1_000_000
    by_settlement: dict[int, FundingCharge] = {}
    conflicting = False
    interval_seen: float | None = None
    wanted = base_symbol(instrument)
    for row in funding_rows:
        row_instrument = getattr(row, "instrument", None)
        if row_instrument is not None and base_symbol(str(row_instrument)) != wanted:
            continue
        settlement_ms = int(row.funding_time_ms)
        if not (entry_ms < settlement_ms <= exit_ms):
            continue
        rate = Decimal(str(row.funding_rate))
        interval_seen = float(getattr(row, "interval_hours", interval_hours))
        charge = FundingCharge(
            settlement_ms=settlement_ms,
            rate=rate,
            return_contribution=-sign * float(rate),
        )
        existing = by_settlement.get(settlement_ms)
        if existing is not None:
            # Overlapping capture pages can repeat a settlement. Charge it once;
            # a different rate for the same boundary is a data conflict, not a
            # second charge.
            conflicting = conflicting or existing.rate != rate
            continue
        by_settlement[settlement_ms] = charge

    charges = sorted(by_settlement.values(), key=lambda charge: charge.settlement_ms)
    interval = interval_hours if interval_seen is None else interval_seen
    step_ns = int(Decimal(str(interval)) * Decimal(3_600_000_000_000))
    off_grid = bool(
        step_ns > 0
        and any((charge.settlement_ms * 1_000_000) % step_ns != 0 for charge in charges)
    )
    expected = _grid_points(entry_ns, exit_ns, interval)
    applied = len(charges)
    if conflicting:
        status = FUNDING_CONFLICTING
    elif dropped_rows_in_tape > 0:
        # A malformed row anywhere in the tape means the span cannot be called
        # complete even when the count happens to match the grid.
        status = FUNDING_PARTIAL
    elif expected == 0:
        # Nothing on the declared grid, but a settlement row still arrived in the
        # span: it is off-grid evidence, not a clean zero-charge completion.
        status = FUNDING_OFF_GRID if off_grid else FUNDING_APPLIED
    elif applied == 0:
        status = FUNDING_MISSING_NO_ROWS
    elif applied != expected or off_grid:
        # Both a missing settlement and an off-grid/extra row invalidate the
        # count-only proof: applied == expected can still hide one of each.
        status = FUNDING_PARTIAL if not off_grid else FUNDING_OFF_GRID
    else:
        status = FUNDING_APPLIED
    return FundingResult(
        charges=tuple(charges),
        applied=applied,
        expected=expected,
        status=status,
        interval_hours=interval,
        dropped_rows_in_tape=dropped_rows_in_tape,
    )


def modelled_slippage_return(
    model: SlippageModel,
    *,
    entry_price: Decimal,
    entry_bar_low: Decimal | None,
    entry_bar_high: Decimal | None,
    legs: int = 2,
) -> float:
    """The declared slippage rule, always adverse (<= 0).

    ``ticks`` scales from the declared tick size; ``entry_bar_range_fraction`` scales from
    the measured entry bar range, so the input is data while the fraction is the declared
    assumption. ``legs`` is 2 for an entry and an exit.
    """
    if model.model == "none":
        return 0.0
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if model.model == "ticks":
        per_leg = float(Decimal(model.ticks) * model.tick_size / entry_price)
    else:
        if entry_bar_low is None or entry_bar_high is None:
            raise ValueError("entry_bar_range_fraction needs the entry bar's low and high")
        if entry_bar_high < entry_bar_low:
            raise ValueError("entry bar high is below its low")
        per_leg = float((entry_bar_high - entry_bar_low) / entry_price) * model.range_fraction
    return round(-abs(per_leg) * legs, 12)


@dataclass(frozen=True)
class CampaignCost:
    """Separated cost fields for one campaign; nothing is collapsed into one number."""

    policy_id: str
    direction: str
    decision_ns: int
    exit_ns: int
    gross_return: float
    fee_cost_return: float
    funding_cost_return: float
    funding_applied: int
    funding_expected: int
    funding_status: str
    slippage_modelled_return: float
    slippage_model: str
    net_return_measured_only: float
    net_return_with_modelled_slippage: float
    execution_shortfall_measured_return: None = None
    execution_shortfall_status: str = EXECUTION_SHORTFALL_MISSING
    model_version: str = SWING_COST_MODEL_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "direction": self.direction,
            "decision_ns": self.decision_ns,
            "exit_ns": self.exit_ns,
            "gross_return": self.gross_return,
            "fee_cost_return": self.fee_cost_return,
            "funding_cost_return": self.funding_cost_return,
            "funding_applied": self.funding_applied,
            "funding_expected": self.funding_expected,
            "funding_status": self.funding_status,
            "slippage_modelled_return": self.slippage_modelled_return,
            "slippage_model": self.slippage_model,
            "net_return_measured_only": self.net_return_measured_only,
            "net_return_with_modelled_slippage": self.net_return_with_modelled_slippage,
            "execution_shortfall_measured_return": self.execution_shortfall_measured_return,
            "execution_shortfall_status": self.execution_shortfall_status,
            "model_version": self.model_version,
        }


def campaign_cost(
    *,
    decision: Any,
    outcome: Any,
    funding_rows: Iterable[Any],
    slippage: SlippageModel,
    entry_bar_low: Decimal | None = None,
    entry_bar_high: Decimal | None = None,
    dropped_rows_in_tape: int = 0,
) -> CampaignCost:
    """Split one replay outcome into separated cost fields."""
    funding = funding_for_position(
        funding_rows,
        instrument=decision.instrument_id,
        direction=decision.direction,
        entry_ns=int(decision.decision_ns),
        exit_ns=int(outcome.exit_ns),
        dropped_rows_in_tape=dropped_rows_in_tape,
    )
    slippage_return = modelled_slippage_return(
        slippage,
        entry_price=Decimal(str(decision.entry_reference)),
        entry_bar_low=entry_bar_low,
        entry_bar_high=entry_bar_high,
    )
    gross = float(outcome.gross_return)
    # the replay reports the fee as a positive magnitude (net = gross - fee); every field
    # this model publishes is a signed cost so the columns can be added without a convention
    fee = -abs(float(outcome.fee_cost_return))
    measured_only = round(gross + fee + funding.cost_return, 12)
    return CampaignCost(
        policy_id=decision.policy_id,
        direction=decision.direction,
        decision_ns=int(decision.decision_ns),
        exit_ns=int(outcome.exit_ns),
        gross_return=gross,
        fee_cost_return=fee,
        funding_cost_return=funding.cost_return,
        funding_applied=funding.applied,
        funding_expected=funding.expected,
        funding_status=funding.status,
        slippage_modelled_return=slippage_return,
        slippage_model=slippage.model,
        net_return_measured_only=measured_only,
        net_return_with_modelled_slippage=round(measured_only + slippage_return, 12),
    )


def aggregate(costs: Iterable[CampaignCost]) -> dict[str, Any]:
    """Family-level totals that keep the fields apart and name what is incomplete."""
    rows = list(costs)
    funding_incomplete = [row for row in rows if row.funding_status != FUNDING_APPLIED]
    return {
        "model_version": SWING_COST_MODEL_VERSION,
        "campaigns": len(rows),
        "gross_return_sum": round(sum(row.gross_return for row in rows), 12),
        "fee_cost_return_sum": round(sum(row.fee_cost_return for row in rows), 12),
        "funding_cost_return_sum": round(sum(row.funding_cost_return for row in rows), 12),
        "funding_settlements_applied": sum(row.funding_applied for row in rows),
        "funding_settlements_expected": sum(row.funding_expected for row in rows),
        "funding_incomplete_campaigns": len(funding_incomplete),
        "slippage_modelled_return_sum": round(sum(row.slippage_modelled_return for row in rows), 12),
        "net_return_measured_only_sum": round(sum(row.net_return_measured_only for row in rows), 12),
        "net_return_with_modelled_slippage_sum": round(
            sum(row.net_return_with_modelled_slippage for row in rows), 12
        ),
        "execution_shortfall_measured_return_sum": None,
        "execution_shortfall_status": EXECUTION_SHORTFALL_MISSING,
        "reading": (
            "measured_only totals contain fees and real funding only; the modelled slippage "
            "total is an assumption and is reported separately; the measured execution "
            "shortfall is absent, not zero"
        ),
    }
