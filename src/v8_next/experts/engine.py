"""Persistent causal per-instrument feature state with O(1)-amortized bar updates.

The experts are pure functions of a growing prefix: every bar re-reads rows
``[0, n)`` of an immutable numeric cache, so full-prefix recomputation is
O(n) per bar and O(N^2) per replay. This engine carries the append-only state
that makes repeated prefix queries incremental without changing any verdict.

Soundness contract (read before touching):

* The engine adopts a frame's numeric cache object by *identity* and the
  producers (:func:`build_candle_dataframe`, ``extend_causal_frame``) never
  mutate it — they only take ``head`` views. Same object plus a longer prefix
  therefore proves the retained rows are unchanged, and only the newcomers
  need verification.
* Every fast path checks what it must: continuity (raises ``source gap`` as
  the legacy path does), newcomer finiteness (falls back to the legacy path,
  which raises as before), and exact length relations. Anything unprovable —
  no cache, a replaced cache, a shorter prefix — falls back to full
  recomputation *without touching state*, so the verdict is always the legacy
  one.
* Pivot confirmation is exact by construction: a pivot at ``i`` needs full
  ``strength`` windows on both sides, so extending the prefix from ``m`` to
  ``n`` can only confirm indices in ``(m - strength - 1, n - strength - 1]``.
  Retained confirmations never change, and per-index ``max``/``min`` over
  identical doubles reproduces the rolling kernels bit-for-bit (selection on
  the same values, no arithmetic).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl

from v8_next.economics.decisions import numeric

if TYPE_CHECKING:
    from v8_next.domain.market import CausalFrame


class FeatureEngine:
    """Append-only indicator state for one instrument feed.

    Owned by the strategy (one per instrument leg), threaded through
    ``observe_all_28`` on the per-bar bundle, and invisible to direct callers
    (tests, research scripts), which keep the legacy full-recompute path.
    """

    __slots__ = (
        "_closes",
        "_confirmed",
        "_ema",
        "_finite",
        "_highs",
        "_ind_n",
        "_lows",
        "_n",
        "_rsi",
        "_tape",
        "_wilder",
    )

    #: EMA spans tracked for trend (5/20) and MACD (12/26) handoffs.
    EMA_SPANS = (5, 12, 20, 26)
    #: Form-B update factors, fixed doubles shared by every advance.
    _EMA_FACTORS = {span: 2 / (span + 1) for span in EMA_SPANS}
    #: Wilder RSI period tracked (the only period wired experts use).
    RSI_PERIOD = 14

    def __init__(self) -> None:
        self._tape: pl.DataFrame | None = None
        self._n = 0
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._finite = True
        #: (high, strength) -> [through, [confirmed indices ascending]].
        self._confirmed: dict[tuple[bool, int], list[Any]] = {}
        #: span -> current EMA value (seeded at handover, form-B updates after).
        self._ema: dict[int, float] = {}
        #: (avg_gain, avg_loss) for the Wilder RSI period, or None unseeded.
        self._wilder: tuple[float, float] | None = None
        #: Retained full RSI history (legacy-exact values, appended per bar).
        self._rsi: list[float | None] | None = None
        #: Prefix length the indicator state (ema/wilder/rsi) covers.
        self._ind_n = 0

    def _extend_values(self, frame: CausalFrame, n: int) -> bool:
        """Retain rows ``[self._n, n)``; False when finiteness is unprovable."""
        tape = self._tape
        assert tape is not None
        old = self._n
        if n > old:
            high_new = tape["high"].slice(old, n - old)
            low_new = tape["low"].slice(old, n - old)
            close_new = tape["close"].slice(old, n - old)
            if (
                not high_new.is_finite().all()
                or not low_new.is_finite().all()
                or not close_new.is_finite().all()
                or high_new.null_count()
                or low_new.null_count()
                or close_new.null_count()
            ):
                return False
            self._highs.extend(high_new.to_list())
            self._lows.extend(low_new.to_list())
            self._closes.extend(close_new.to_list())
            self._n = n
        return True

    def _is_pivot(self, i: int, *, high: bool, strength: int) -> bool:
        """Exact per-index port of the rolling pivot predicate.

        ``rolling_max(strength).shift(1)`` is null below ``i == strength`` and
        ``rolling_max(strength).shift(-strength)`` is null above
        ``i == n - strength - 1``; elsewhere both windows are full. Strict
        inequality on both sides preserves equal-price handling.
        """
        vals = self._highs if high else self._lows
        if i < strength or i + strength >= len(vals):
            return False
        value = vals[i]
        if high:
            return value > max(vals[i - strength : i]) and value > max(vals[i + 1 : i + strength + 1])
        return value < min(vals[i - strength : i]) and value < min(vals[i + 1 : i + strength + 1])

    def confirmed_pivots(
        self, frame: CausalFrame, *, high: bool, strength: int
    ) -> tuple[int, ...] | None:
        """Append-only confirmed pivots, or None when falling back to full compute."""
        if strength < 1:
            raise ValueError("positive pivot strength required")
        if not frame.continuous:
            raise ValueError("source gap")
        n = len(frame.candles)
        tape = frame._prefix_df  # noqa: SLF001 -- same-package numeric-cache identity
        if tape is None:
            return None
        if not self._adopt_track(frame, n) or not self._finite:
            # No adoptable state: poison only on proven-newcomer failure (the
            # legacy fallback then raises, as it must). A poisoned engine
            # heals on the next reseed, which re-proves finiteness natively.
            if self._tape is not None:
                self._finite = False
            return None
        key = (high, strength)
        entry = self._confirmed.get(key)
        if entry is None:
            indices = [
                i for i in range(n) if self._is_pivot(i, high=high, strength=strength)
            ]
            self._confirmed[key] = [n - strength - 1, indices]
            return tuple(indices)
        through, indices = entry
        stop = n - strength - 1
        for i in range(through + 1, stop + 1):
            if self._is_pivot(i, high=high, strength=strength):
                indices.append(i)
        entry[0] = stop
        return tuple(indices)

    def _adopt_track(self, frame: CausalFrame, n: int) -> bool:
        """Adopt the frame's cache for value retention; False to fall back."""
        tape = frame._prefix_df  # noqa: SLF001 -- same-package numeric-cache identity
        if tape is None:
            return False
        if tape is not self._tape or n < self._n:
            # Foreign tape or a shorter prefix: reseed values fully and drop
            # all derived state, which may cover rows this prefix lacks.
            # Finiteness is re-proven natively: the reseed is trusted storage,
            # never a verdict — unproven prefixes fall back to the legacy
            # path, which raises exactly as before.
            high_all = tape["high"].slice(0, n)
            low_all = tape["low"].slice(0, n)
            close_all = tape["close"].slice(0, n)
            self._tape = tape
            self._highs = high_all.to_list()
            self._lows = low_all.to_list()
            self._closes = close_all.to_list()
            self._n = n
            self._finite = bool(
                high_all.is_finite().all()
                and low_all.is_finite().all()
                and close_all.is_finite().all()
            ) and not (high_all.null_count() or low_all.null_count() or close_all.null_count())
            self._confirmed = {}
            self._ema = {}
            self._wilder = None
            self._rsi = None
            self._ind_n = 0
            return True
        return self._extend_values(frame, n)

    def adopt_verified(self, frame: CausalFrame) -> None:
        """Adopt a legacy-verified prefix (call only after full compute succeeded).

        Same-tape growth keeps derived state (pivot entries stay valid: their
        ``through`` only moves forward); a foreign tape or shorter prefix
        reseeds values and drops derived state inside ``_adopt_track``.
        """
        self._adopt_track(frame, len(frame.candles))

    def retained_series(self) -> tuple[list[float], list[float], int] | None:
        """Retained (highs, lows, n) for scalar tail checks, or None if unusable."""
        if self._tape is None or not self._finite:
            return None
        return self._highs, self._lows, self._n

    def tail_threshold(self, n: int, period: int = 14) -> float | None:
        """Mean high-low range over the latest ``period`` rows via the same kernel.

        Computed on zero-copy cache slices — identical doubles, identical order,
        identical reduction — never on resummed Python floats.
        """
        tape = self._tape
        if tape is None:
            return None

        value = (tape["high"].slice(n - period, period) - tape["low"].slice(n - period, period)).mean()
        return numeric(value) if value is not None else None

    @staticmethod
    def _rsi_value(avg_gain: float, avg_loss: float) -> float:
        """Scalar port of the legacy RSI select: flat 50, gain-only 100."""
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    def _advance(self) -> None:
        """Roll seeded indicator state forward to the retained prefix length."""
        old, new = self._ind_n, self._n
        if new <= old:
            return
        closes = self._closes
        if self._ema:
            for close in closes[old:new]:
                for span, ema in self._ema.items():
                    factor = self._EMA_FACTORS[span]
                    self._ema[span] = ema + factor * (close - ema)
        if self._wilder is not None and self._rsi is not None:
            period = self.RSI_PERIOD
            alpha = 1 / period
            avg_gain, avg_loss = self._wilder
            previous = closes[old - 1]
            rsi = self._rsi
            for close in closes[old:new]:
                delta = close - previous
                previous = close
                gain = delta if delta > 0 else 0.0
                loss = -delta if delta < 0 else 0.0
                avg_gain = avg_gain + alpha * (gain - avg_gain)
                avg_loss = avg_loss + alpha * (loss - avg_loss)
                rsi.append(self._rsi_value(avg_gain, avg_loss))
            self._wilder = (avg_gain, avg_loss)
        self._ind_n = new

    def _ready_indicators(self, frame: CausalFrame, n: int) -> bool:
        """Track the frame and advance seeded families; False to fall back."""
        tape = frame._prefix_df  # noqa: SLF001 -- same-package numeric-cache identity
        if tape is None:
            return False
        if not self._adopt_track(frame, n) or not self._finite:
            if self._tape is not None:
                self._finite = False
            return False
        self._advance()
        return True

    def discard_indicators(self) -> None:
        """Drop indicator state (keep pivot/value retention); later bars fall back."""
        self._ema = {}
        self._wilder = None
        self._rsi = None
        self._ind_n = 0

    def adopt_ema(
        self,
        frame: CausalFrame,
        known: dict[int, float],
        verify_diff: float | None = None,
    ) -> None:
        """Hand over EMA state from legacy-verified endpoints.

        Missing spans are filled with the same Polars kernels the legacy path
        runs, over the same prefix rows; ``known`` (the legacy path's own
        outputs) and ``verify_diff`` (a legacy MACD scalar) are verified
        exactly, and any mismatch discards EMA state so later bars fall back
        instead of serving doubted values.
        """
        n = len(frame.candles)
        if not self._adopt_track(frame, n) or not self._finite:
            return
        tape = self._tape
        assert tape is not None
        # Advance seeded families first: afterwards every retained span is a
        # Python-recurrence value the `known` check verifies end to end.
        self._advance()
        prefix = tape["close"].slice(0, n)
        filled = dict(self._ema)
        for span in self.EMA_SPANS:
            if span not in filled:
                filled[span] = numeric(prefix.ewm_mean(span=span, adjust=False)[-1])
        if any(span not in filled or filled[span] != value for span, value in known.items()):
            self._ema = {}
            return
        if (
            verify_diff is not None
            and 12 in filled
            and 26 in filled
            and filled[12] - filled[26] != verify_diff
        ):
            self._ema = {}
            return
        self._ema = filled
        self._ind_n = n

    def ema_pair(self, frame: CausalFrame) -> tuple[float, float] | None:
        """Current EMA(5)/EMA(20), or None when falling back to full compute."""
        n = len(frame.candles)
        if not self._ready_indicators(frame, n):
            return None
        if 5 not in self._ema or 20 not in self._ema:
            return None
        return self._ema[5], self._ema[20]

    def macd_value(self, frame: CausalFrame) -> float | None:
        """Current MACD line (EMA12 minus EMA26), or None to fall back."""
        n = len(frame.candles)
        if not self._ready_indicators(frame, n):
            return None
        if 12 not in self._ema or 26 not in self._ema:
            return None
        return self._ema[12] - self._ema[26]

    def adopt_rsi(
        self, frame: CausalFrame, result: tuple[float | None, ...]
    ) -> None:
        """Hand over Wilder state: Polars seed plus form-B catch-up to the endpoint.

        The retained RSI history comes from the legacy path's own output
        (exact); the smoothed averages are rebuilt and then verified against
        the legacy endpoint — a mismatch discards Wilder state instead of
        serving doubted values.
        """
        n = len(frame.candles)
        period = self.RSI_PERIOD
        if n <= period or not self._adopt_track(frame, n) or not self._finite:
            return
        tape = self._tape
        assert tape is not None
        self._advance()
        closes = tape["close"].slice(0, n)
        deltas = closes.diff().slice(1)
        gains = deltas.clip(lower_bound=0)
        losses = (-deltas).clip(lower_bound=0)
        avg_gain = numeric(gains.head(period).mean())
        avg_loss = numeric(losses.head(period).mean())
        alpha = 1 / period
        gain_list = gains.slice(period).to_list()
        loss_list = losses.slice(period).to_list()
        for gain, loss in zip(gain_list, loss_list, strict=True):
            avg_gain = avg_gain + alpha * (gain - avg_gain)
            avg_loss = avg_loss + alpha * (loss - avg_loss)
        if self._rsi_value(avg_gain, avg_loss) != result[-1]:
            self._wilder = None
            self._rsi = None
            return
        self._wilder = (avg_gain, avg_loss)
        self._rsi = list(result)
        self._ind_n = n

    def rsi_tuple(self, frame: CausalFrame, n: int) -> tuple[float | None, ...] | None:
        """Retained full RSI history, or None when falling back to full compute."""
        if not self._ready_indicators(frame, n):
            return None
        if self._wilder is None or self._rsi is None or len(self._rsi) != n:
            return None
        return tuple(self._rsi)

    @staticmethod
    def latest_above(
        indices: tuple[int, ...],
        highs: list[float],
        lows: list[float],
        threshold: float,
    ) -> int | None:
        """Latest pivot whose bar range clears ``threshold`` (scalar float port)."""
        for position in range(len(indices) - 1, -1, -1):
            index = indices[position]
            if highs[index] - lows[index] >= threshold:
                return index
        return None
