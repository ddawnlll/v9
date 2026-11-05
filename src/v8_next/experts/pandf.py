"""Close-based economic P&F hypothesis; no per-box materialization."""

from dataclasses import dataclass, replace
from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import per_bar_memo


@dataclass(frozen=True)
class Column:
    direction: int
    start: int
    origin: Decimal
    extreme: Decimal
    steps: int


def _columns_validated(closes: tuple[Decimal, ...], box: Decimal) -> tuple[Column, ...]:
    """Three-box reversal core over already-validated positive finite inputs."""
    if not closes:
        return ()
    neg3box = -3 * box
    completed: list[Column] = []
    current = Column(1, 0, closes[0], closes[0], 0)
    for i, close in enumerate(closes[1:], 1):
        movement = (close - current.extreme) * current.direction
        if movement >= box:
            steps = int(movement / box)
            current = replace(
                current,
                extreme=current.extreme + current.direction * steps * box,
                steps=current.steps + steps,
            )
        elif movement <= neg3box:
            completed.append(current)
            direction = -current.direction
            origin = current.extreme + direction * box
            steps = int(abs(close - origin) / box)
            current = Column(direction, i, origin, origin + direction * steps * box, steps)
    return (*completed, current)


def columns(closes: tuple[Decimal, ...], box: Decimal) -> tuple[Column, ...]:
    """Three-box reversal, first close seeds an X column as in the source."""
    if not box.is_finite() or box <= 0:
        raise ValueError("positive finite box required")
    if any(not c.is_finite() or c <= 0 for c in closes):
        raise ValueError("positive finite closes required")
    return _columns_validated(closes, box)


@dataclass(frozen=True)
class PointFigureSetup:
    direction: str
    column_start_ns: int
    observed_ns: int
    box: Decimal
    stop: Decimal
    target: Decimal
    steps: int


@per_bar_memo
def _frame_columns(frame: CausalFrame, box: Decimal) -> tuple[Column, ...]:
    """Share the immutable transformation between variants on the same bar."""
    return _columns_validated(tuple(c.close for c in frame.candles), box)


def pandf_setup(frame: CausalFrame, variant: str = "a") -> PointFigureSetup | None:
    if variant not in ("a", "b", "c", "d"):
        raise ValueError("unsupported P&F variant")
    if not frame.continuous:
        raise ValueError("source gap")
    bars = frame.candles
    if len(bars) < 20:
        return None
    box = sum((c.high - c.low for c in bars[-14:]), Decimal(0)) / 14
    if not box.is_finite() or box <= 0:
        if not box.is_finite():
            # Unreachable for validated frame candles (finite Decimal ranges),
            # but preserved: the old path raised from columns() here.
            raise ValueError("positive finite box required")
        return None
    # Frame candles already guarantee positive finite Decimal closes
    # (Candle.__post_init__), so the validated core runs without rescanning
    # the whole prefix a second time. `box` is checked above; `columns` keeps
    # its own validation for direct callers.
    transformed = _frame_columns(frame, box)
    last = transformed[-1]
    direction = 1 if variant in ("a", "c") else -1
    needed = 2 if variant in ("a", "b") else 3
    matching = [c for c in transformed if c.direction == direction]
    if last.direction != direction or len(matching) < needed:
        return None
    if any((last.extreme - prior.extreme) * direction <= 0 for prior in matching[-needed:-1]):
        return None
    stop = last.origin
    target = stop + direction * last.steps * box * 3
    if (bars[-1].close - stop) * direction <= 0 or (target - bars[-1].close) * direction <= 0:
        return None
    return PointFigureSetup(
        "LONG" if direction == 1 else "SHORT",
        bars[last.start].end_ns,
        bars[-1].end_ns,
        box,
        stop,
        target,
        last.steps,
    )


def observe_pandf(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "a"
) -> Stance:
    if variant not in ("a", "b", "c", "d"):
        raise ValueError("unsupported P&F variant")
    reason = context_reason(frame, opportunity, 20)
    setup = None
    if reason is None:
        setup = pandf_setup(frame, variant)
        reason = "POINT_FIGURE_BREAKOUT" if setup else "NO_POINT_FIGURE_BREAKOUT"
    return replace(
        directional_stance(
            frame,
            opportunity,
            family="pandf-breakout",
            dependency="ohlcv-point-figure-v1",
            mechanism="boxed-price-breakout",
            direction=setup.direction if setup else None,
            reason=reason,
        ),
        variant_id=variant,
    )
