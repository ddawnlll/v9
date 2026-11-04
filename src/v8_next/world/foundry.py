"""Lib-first world generators — thin envelopes over arch/numpy/scipy (R1).

Ownership split (no engine fork, no invented venue logic):
- stationary bootstrap resampling → ``arch`` (Politis-Romano variable blocks).
- stochastic-vol clustering, Hawkes-style excitation, Gaussian-copula
  co-movement, block resampling, regime switching, surgical splices →
  ``numpy``/``scipy`` arithmetic over a seed-pinned generator.
- execution/simulation/fills → NautilusTrader (never reimplemented here).

Isolation: generators never read research tape or holdout stores — the only
randomness source is the spec seed — so synthetic populations cannot leak
into research/holdout by construction. Families needing models we do not own
(learned-diffusion, agent-market) are refused with a named reason, never
silently substituted.

Provisioning: ``arch``/``scipy`` belong to the ``research`` extra, so both are
imported inside the one path that needs them. A module-scope import here made
every importer of ``v8_next.world`` uncollectable in a bare ``--extra dev``
environment (t_9715e0f2).
"""

from __future__ import annotations

import numpy as np

from v8_next.world.spec import WorldBar, WorldFamily, WorldReceipt, WorldSpec

__all__ = ["build_world"]

_BAR_NS = 3_600_000_000_000  # 1h bars on a synthetic clock (not venue time)

_SUPPORTED = frozenset(
    {
        WorldFamily.STRUCTURAL_REGIME,
        WorldFamily.BLOCK_RESAMPLED,
        WorldFamily.STATIONARY_BOOTSTRAP,
        WorldFamily.STOCHASTIC_VOLATILITY,
        WorldFamily.JUMP_CASCADE,
        WorldFamily.CROSS_ASSET_CONTAGION,
        WorldFamily.COUNTERFACTUAL_SURGERY,
    }
)


def build_world(spec: WorldSpec) -> WorldReceipt:
    """Generate one seed-pinned synthetic world (I1: same spec+seed ⇒ identical)."""
    if spec.family not in _SUPPORTED:
        raise ValueError(
            f"FAMILY_NOT_PORTED: {spec.family.value} needs a model this port does not own"
        )
    rng = np.random.default_rng(spec.seed)
    if spec.family is WorldFamily.STATIONARY_BOOTSTRAP:
        returns = _stationary_bootstrap_returns(spec, rng)
    elif spec.family is WorldFamily.BLOCK_RESAMPLED:
        returns = _block_resampled_returns(spec, rng)
    elif spec.family is WorldFamily.STOCHASTIC_VOLATILITY:
        returns = _stochastic_vol_returns(spec, rng)
    elif spec.family is WorldFamily.JUMP_CASCADE:
        returns = _hawkes_returns(spec, rng)
    elif spec.family is WorldFamily.CROSS_ASSET_CONTAGION:
        returns = _copula_returns(spec, rng)
    elif spec.family is WorldFamily.COUNTERFACTUAL_SURGERY:
        returns = _surgery_returns(spec, rng)
    else:
        returns = _regime_returns(spec, rng)
    return WorldReceipt.bind(spec, _to_bars(spec, returns))


def _to_bars(spec: WorldSpec, returns: np.ndarray) -> tuple[WorldBar, ...]:
    closes: list[float] = []
    price = spec.base_price
    for r in [float(x) for x in returns[: spec.n_bars]]:
        price = price * (1.0 + r)
        closes.append(price)
    bars: list[WorldBar] = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else spec.base_price
        high = max(open_, close) * 1.001
        low = min(open_, close) * 0.999
        bars.append(
            WorldBar(
                index=i,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000.0,
                ts_ns=i * _BAR_NS,
            )
        )
    return tuple(bars)


def _base_scale(spec: WorldSpec) -> float:
    return spec.volatility_annualized / float(np.sqrt(365 * 24))


def _stationary_bootstrap_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    # arch owns the resampling; numpy owns only the seed-pinned base.
    # Lazily imported: `arch` is declared in the `research` extra only, and this
    # family is the one path that needs it (a module-scope import made every
    # importer of `v8_next.world` uncollectable without the extra).
    try:
        from arch.bootstrap import StationaryBootstrap  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "ARCH_NOT_PROVISIONED: the STATIONARY_BOOTSTRAP family needs the `arch` "
            "package from the `research` extra "
            "(uv sync --project v8-next --locked --extra research); "
            "no substitute generator is used"
        ) from exc
    base = rng.normal(0.0, _base_scale(spec), size=max(spec.n_bars * 2, 64))
    block = max(2, min(24, spec.n_bars // 4))
    seed = int(rng.integers(0, 2**31 - 1))
    bs = StationaryBootstrap(block, base, seed=seed)
    for sample, _kw in bs.bootstrap(1):
        arr = np.asarray(sample[0] if isinstance(sample, (list, tuple)) else sample)
        return np.asarray(arr).ravel()[: spec.n_bars]
    raise ValueError("BOOTSTRAP_EMPTY: arch produced no resample")


def _block_resampled_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    base = rng.normal(0.0, _base_scale(spec), size=max(spec.n_bars * 2, 64))
    block = 12
    out: list[float] = []
    while len(out) < spec.n_bars:
        start = int(rng.integers(0, len(base) - block))
        out.extend(base[start : start + block])
    return np.asarray(out[: spec.n_bars])


def _stochastic_vol_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    # GARCH(1,1)-style clustering: vol-of-vol via numpy recursion.
    scale, out, var = _base_scale(spec), [], _base_scale(spec) ** 2
    for _ in range(spec.n_bars):
        shock = float(rng.normal())
        var = 0.000001 + 0.85 * var + 0.10 * (shock * scale) ** 2
        out.append(float(np.sqrt(var)) * shock)
    return np.asarray(out)


def _hawkes_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    # Self-exciting downside jumps: exponential kernel over numpy draws.
    out, intensity = [], spec.jump_frequency / float(spec.n_bars)
    for _ in range(spec.n_bars):
        intensity = 0.05 * spec.jump_frequency / float(spec.n_bars) + 0.82 * intensity
        if rng.random() < intensity:
            out.append(float(rng.normal(spec.jump_mean, spec.jump_std)))
            intensity += 0.6
        else:
            out.append(float(rng.normal(0.0, _base_scale(spec))))
    return np.asarray(out)


def _copula_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    # Gaussian-copula co-movement (tail contagion proxy): correlated leg
    # averaged back to one synthetic symbol so the receipt stays single-asset.
    import importlib

    norm = importlib.import_module("scipy.stats").norm

    rho, n = 0.7, spec.n_bars
    z1 = rng.normal(size=n)
    z2 = rho * z1 + float(np.sqrt(1.0 - rho**2)) * rng.normal(size=n)
    u = norm.cdf(z2)
    leg = norm.ppf(0.01 + 0.98 * u) * _base_scale(spec)
    own = rng.normal(0.0, _base_scale(spec), size=n)
    return np.asarray(0.5 * (own + leg))


def _surgery_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    # Multi-axis splice on a synthetic carrier (never real tape): a level
    # shift plus a volatility splice at fixed fractions of the window.
    base = rng.normal(0.0, _base_scale(spec), size=spec.n_bars)
    k1, k2 = spec.n_bars // 3, 2 * spec.n_bars // 3
    base[k1:] = base[k1:] - spec.jump_mean
    base[k2:] = base[k2:] * 2.0
    return np.asarray(base)


def _regime_returns(spec: WorldSpec, rng: np.random.Generator) -> np.ndarray:
    # 3-state drift switcher standing in for the 9-state Rust regime engine
    # at this substrate layer (family label preserved; fuller engine is #456+).
    drifts = [0.0008, 0.0, -0.0008]
    state, out = 1, []
    for _ in range(spec.n_bars):
        if rng.random() < 0.06:
            state = int(rng.integers(0, 3))
        out.append(drifts[state] + float(rng.normal(0.0, _base_scale(spec))))
    return np.asarray(out)
