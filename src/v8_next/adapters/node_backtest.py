"""Run the portfolio backtest through ``BacktestNode`` against a parquet catalog.

Why this exists (F6)
--------------------
``portfolio_backtest.run_portfolio_backtest`` builds one ``BacktestEngine``, adds
instruments/bars/funding in memory, and runs. This module expresses the *same*
backtest as a catalog-fed ``BacktestNode`` run: ``BacktestRunConfig`` +
``BacktestVenueConfig`` + ``BacktestDataConfig``, with the venue's execution
models taken from the shared :mod:`v8_next.adapters.execution_models` profile and
the strategy taken from :mod:`v8_next.adapters.expert_strategy`, so a parity
check compares two run *mechanisms* over one semantic definition rather than two
independently drifting ones.

Strategy attachment mechanism (measured, not assumed)
----------------------------------------------------
In NautilusTrader 2.0.0rc4 ``BacktestEngineConfig`` has **no** strategies field,
so a node run cannot declare strategies in its config. What actually works is::

    node = BacktestNode([run_config])
    node.build()                       # creates the engine for the run config
    node.add_strategy(run_config_id, strategy)   # attaches to that engine
    node.run()                         # reuses the built engine

``node.run()`` only auto-builds when no engine exists yet, so attaching between
``build()`` and ``run()`` survives. That is the mechanism this module uses.

Accounting reuse
----------------
``generate_order_fills_report`` / ``generate_orders_report`` /
``generate_account_report`` are methods on the *node* keyed by run-config id,
while ``engine_state.economic_state`` and
``execution_telemetry.native_fill_records`` expect an object with an engine-like
surface. :class:`_NodeEngineView` adapts one to the other so both run paths
reuse the same accounting and telemetry code instead of a second implementation
that could disagree.

Known, named divergence from the direct-engine path
---------------------------------------------------
NautilusTrader 2.0.0rc4 ships no catalog writer for ``FundingRateUpdate``
(``ParquetDataCatalog`` has ``write_mark_price_updates`` but no
``write_funding_rate_updates``), so a catalog-fed run cannot settle funding.
``funding_catalog_support`` is published on every result and the parity test
asserts the resulting accounting divergence with both numbers rather than
claiming parity that was not achieved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.backtest import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestNode,
    BacktestRunConfig,
    BacktestVenueConfig,
)
from nautilus_trader.common import LogLevel
from nautilus_trader.config import LoggerConfig
from nautilus_trader.model import AccountType, InstrumentId, OmsType, Venue

from v8_next.adapters.catalog_tape import (
    BAR_STEP,
    FUNDING_CATALOG_SUPPORT,
    bar_type_str,
    catalog_inventory,
)
from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.execution_models import (
    DEFAULT_PROFILE,
    ExecutionProfile,
    resolve_profile,
    venue_kwargs,
)
from v8_next.adapters.execution_telemetry import execution_telemetry, native_fill_records
from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy, ExpertStrategyConfig
from v8_next.adapters.portfolio_backtest import SleeveSpec
from v8_next.domain.market import Candle
from v8_next.domain.positioning import PositioningReading


@dataclass(frozen=True)
class NodeRunConfig:
    """Everything the node run needs that the direct path took as arguments."""

    catalog_path: str
    legs: tuple[str, ...]
    venue: str = "BINANCE"
    currency: str = "USDT"
    initial_balance: Decimal = Decimal("10000")
    per_leg_notional: Decimal = Decimal("1000")
    bracket_stop_pct: Decimal | None = Decimal("0.02")
    bracket_target_pct: Decimal | None = Decimal("0.04")
    execution_profile: str | ExecutionProfile = DEFAULT_PROFILE
    run_config_id: str = "f6-catalog-node"
    start_ns: int | None = None
    end_ns: int | None = None


class _NodeEngineView:
    """Engine-like surface over a node run, for shared accounting/telemetry.

    Only the members ``economic_state`` and ``native_fill_records`` actually
    touch are provided; everything else raises so a future caller cannot
    silently depend on behaviour this adapter does not have.
    """

    def __init__(self, node: BacktestNode, run_config_id: str) -> None:
        self._node = node
        self._run_config_id = run_config_id
        self.cache = node.get_engine_cache(run_config_id)

    def generate_order_fills_report(self) -> Any:
        return self._node.generate_order_fills_report(self._run_config_id)

    def generate_orders_report(self) -> Any:
        return self._node.generate_orders_report(self._run_config_id)

    def generate_account_report(self) -> Any:
        return self._node.generate_account_report(self._run_config_id)


def build_run_config(cfg: NodeRunConfig) -> BacktestRunConfig:
    """Translate an explicit run definition into BacktestNode configuration.

    The venue's fill/fee/latency/margin models and the bar-execution flags come
    from ``venue_kwargs`` so the catalog run and the direct-engine run are
    configured from one source, not two.
    """
    profile = resolve_profile(cfg.execution_profile)
    venue_cfg = BacktestVenueConfig(
        name=cfg.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[f"{cfg.initial_balance} {cfg.currency}"],
        default_leverage=Decimal(1),
        liquidation_enabled=False,
        **venue_kwargs(profile),
    )
    data_cfg = BacktestDataConfig(
        data_type="Bar",
        catalog_path=str(cfg.catalog_path),
        instrument_ids=[InstrumentId.from_str(f"{raw}-PERP.{cfg.venue}") for raw in cfg.legs],
        bar_types=[bar_type_str(f"{raw}-PERP.{cfg.venue}") for raw in cfg.legs],
        start_time=cfg.start_ns,
        end_time=cfg.end_ns,
    )
    return BacktestRunConfig(
        venues=[venue_cfg],
        data=[data_cfg],
        engine=BacktestEngineConfig(
            bypass_logging=True,
            logging=LoggerConfig(stdout_level=LogLevel.WARNING),
        ),
        id=cfg.run_config_id,
        raise_exception=True,
        dispose_on_completion=False,
        start=cfg.start_ns,
        end=cfg.end_ns,
    )


def _strategies(
    legs: tuple[str, ...],
    sleeves: tuple[SleeveSpec, ...],
    source_candles: dict[str, dict[int, Candle]],
    readings: tuple[PositioningReading, ...],
    cfg: NodeRunConfig,
) -> list[ExpertEnsembleStrategy]:
    """One ensemble per (leg, sleeve), in the direct path's iteration order.

    Ordering matters: the trader assigns strategy ids in attach order, and the
    equivalence check compares position ids between the two run paths.
    """
    out: list[ExpertEnsembleStrategy] = []
    for raw in legs:
        instrument_id = f"{raw}-PERP.{cfg.venue}"
        for sleeve in sleeves:
            out.append(
                ExpertEnsembleStrategy(
                    ExpertStrategyConfig(
                        instrument_id=instrument_id,
                        bar_type_str=bar_type_str(instrument_id),
                        min_support_quorum=sleeve.quorum,
                        max_contradiction_tolerance=sleeve.tolerance,
                        target_notional=cfg.per_leg_notional
                        * Decimal(str(sleeve.notional_fraction)),
                        bracket_stop_pct=cfg.bracket_stop_pct,
                        bracket_target_pct=cfg.bracket_target_pct,
                        max_concurrent_positions=1,
                    ),
                    source_candles=source_candles[raw],
                    readings=readings,
                )
            )
    return out


def run_node_backtest(
    cfg: NodeRunConfig,
    legs_candles: dict[str, tuple[Candle, ...]],
    sleeves: tuple[SleeveSpec, ...],
    *,
    readings: tuple[PositioningReading, ...] = (),
) -> dict[str, Any]:
    """Execute the portfolio backtest through ``BacktestNode`` + catalog.

    ``legs_candles`` is the same window the direct path was given; the strategy
    needs it for its causal prefix cache, exactly as
    ``run_portfolio_backtest`` passes ``source_candles``. Bars themselves are
    read from the catalog by the node.
    """
    if not cfg.legs:
        raise ValueError("at least one instrument leg required")
    frac = sum(s.notional_fraction for s in sleeves)
    if abs(frac - 1.0) > 1e-9:
        raise ValueError(f"sleeve fractions must sum to 1.0, got {frac}")
    ven = Venue(cfg.venue)
    from nautilus_trader.model import Currency

    curr = Currency.from_str(cfg.currency)
    profile = resolve_profile(cfg.execution_profile)

    run_config = build_run_config(cfg)
    started = time.monotonic()
    node = BacktestNode([run_config])
    node.build()
    source_candles = {
        raw: {c.end_ns: c for c in legs_candles[raw]} for raw in cfg.legs
    }
    strategies = _strategies(cfg.legs, sleeves, source_candles, readings, cfg)
    for strat in strategies:
        node.add_strategy(cfg.run_config_id, strat)
    results = node.run()
    wall_time_s = round(time.monotonic() - started, 3)

    view = _NodeEngineView(node, cfg.run_config_id)
    # The view is a structural adapter exposing only what economic_state uses
    # (engine.cache.account_for_venue / orders / positions).
    account = economic_state(view, ven, curr)  # type: ignore[arg-type]
    opened: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    all_decisions: list[dict[str, Any]] = []
    for strat in strategies:
        opened.extend(strat.opened_positions)
        closed.extend(strat.closed_positions)
        all_decisions.extend(
            {**d, "instrument_id": str(strat.instrument_id)} for d in strat.decisions
        )

    fill_records, fill_report_type = native_fill_records(view)
    execution = execution_telemetry(profile, fill_records, fill_report_type, opened, all_decisions)
    inventory = catalog_inventory(cfg.catalog_path, catalog_path=cfg.catalog_path)
    trade_rows = inventory["per_type"].get("trade_ticks", {}).get("rows")
    try:
        node.dispose()
    except Exception:  # pragma: no cover - dispose best effort after a completed run
        pass

    return {
        "mechanism": "BacktestNode + BacktestRunConfig + BacktestVenueConfig + BacktestDataConfig",
        "strategy_attachment": "node.build(); node.add_strategy(run_config_id, strategy); node.run()",
        "run_config_id": cfg.run_config_id,
        "catalog_path": cfg.catalog_path,
        "catalog_inventory_digest": inventory["inventory_digest"],
        "catalog_data_types": inventory["data_types"],
        "trade_ticks_in_catalog": trade_rows,
        "bar_spec": BAR_STEP,
        "venue_config": {
            "name": cfg.venue,
            "oms_type": "NETTING",
            "account_type": "MARGIN",
            "starting_balances": [f"{cfg.initial_balance} {cfg.currency}"],
            "liquidation_enabled": False,
            "default_leverage": "1",
        },
        "legs": sorted(cfg.legs),
        "sleeves": [s.name for s in sleeves],
        "total_bars": sum(len(c) for c in legs_candles.values()),
        "decisions_count": len(all_decisions),
        "opened_positions": opened,
        "closed_positions": closed,
        "account": account,
        "execution": execution,
        "fill_signature": execution.get("fill_signature"),
        "fill_records": fill_records,
        "trades_fed": trade_rows if isinstance(trade_rows, int) else 0,
        # A catalog in this Nautilus build cannot carry funding at all; say so
        # instead of publishing 0 settlements, which would read as "no funding".
        "funding_settlements_fed": None,
        "funding_catalog_support": FUNDING_CATALOG_SUPPORT,
        "node_results": len(results),
        "wall_time_s": wall_time_s,
    }
