"""Build a NautilusTrader ``ParquetDataCatalog`` from the real quad tape (F6).

The direct-engine path in :mod:`v8_next.adapters.portfolio_backtest` constructs
instruments and bars in memory and hands them to ``BacktestEngine.add_data``.
F6 needs the *same* input expressed as an on-disk catalog so a
``BacktestNode`` run can load it, which means two things must be provable:

1. the catalog holds exactly the tape it claims (same bars, same instruments,
   same count and timestamps) -- hence :func:`catalog_inventory`; and
2. the objects a catalog reader sees are the objects the direct path builds --
   hence this module reuses ``portfolio_backtest._instrument`` / ``._bars``
   rather than re-declaring them. A private import is deliberate: re-declaring
   the constructor here would let the two run paths drift apart silently, and
   the whole point of the F6 equivalence test is that they cannot.

Honesty contract
----------------
Every number this module publishes is read back from disk or from the source
tape; nothing is assumed. A data type the catalog cannot be queried for is
reported with ``rows: None`` and a named reason, never as a zero.
Funding rate updates have no catalog writer in NautilusTrader 2.0.0rc4, so a
catalog-fed run physically cannot carry funding; that absence is named in the
build summary (:data:`FUNDING_CATALOG_SUPPORT`) instead of being papered over.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import Bar, TradeTick
from nautilus_trader.persistence import ParquetDataCatalog

# Reused on purpose (see module docstring): the node path must feed byte-for-byte
# the same instruments and bars as the direct-engine path.
from v8_next.adapters.portfolio_backtest import _bars, _instrument, base_currency
from v8_next.adapters.trade_tape import trades_digest, trades_from_dump
from v8_next.evaluation.multitape import MultiTape, load_multitape
from v8_next.evaluation.tape_identity import TapeManifest, verify_manifest_against_file

#: Repository-relative defaults; external datasets remain optional and gitignored.
REPO_ROOT = Path(__file__).resolve().parents[3]
QUAD_TAPE = REPO_ROOT / "research" / "tape" / "quad-1h-12m"

#: Verified public daily aggTrades archives (Binance data.binance.vision), keyed
#: by raw instrument. Only carried into the catalog when a bounded trade window
#: is requested.
AGG_TRADES_DIRS: dict[str, Path] = {
    "BTCUSDT": REPO_ROOT / "research" / "tape" / "btcusdt-agg-trades-2025-07",
    "ETHUSDT": REPO_ROOT / "research" / "tape" / "ethusdt-agg-trades-2025-07",
}

#: Bar spec every leg is written with; the direct path uses the same string.
BAR_STEP = "1-HOUR-LAST-EXTERNAL"

#: NautilusTrader 2.0.0rc4 exposes no catalog writer for FundingRateUpdate, so a
#: catalog cannot carry funding. Named here so a reader never has to guess why a
#: catalog run has no funding settlements.
FUNDING_CATALOG_SUPPORT = "ABSENT_NO_CATALOG_WRITER_FOR_FUNDING_RATE_UPDATE"


def resolve_tape_path(explicit: Path | str | None = None) -> Path:
    """Resolve explicit > V8_TAPE_PATH > repo default independently of cwd."""
    selected = explicit if explicit is not None else os.environ.get("V8_TAPE_PATH")
    if selected is None:
        path = QUAD_TAPE
    else:
        if not str(selected).strip():
            raise ValueError("empty tape path is not a valid override")
        path = Path(selected).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"external tape not found: {path}")
    return path


def bar_type_str(instrument_id: str) -> str:
    """Bar type string for one leg, matching the direct-engine path exactly."""
    return f"{instrument_id}-{BAR_STEP}"


def default_catalog_root(base: Path | None = None) -> Path:
    """``<v8-next>/artifacts/catalog-f6`` (gitignored ``artifacts/`` tree)."""
    if base is not None:
        return Path(base)
    return Path(__file__).resolve().parents[3] / "artifacts" / "catalog-f6"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def compute_run_id(
    *,
    tape_sha256: str,
    limit: int | None,
    legs: tuple[str, ...],
    include_trades: bool,
    trades_digest_value: str | None,
) -> str:
    """Content-addressed run id: two catalogs with this id hold the same input."""
    payload = json.dumps(
        {
            "tape_sha256": tape_sha256,
            "limit": limit,
            "legs": sorted(legs),
            "bar_step": BAR_STEP,
            "include_trades": include_trades,
            "trades_digest": trades_digest_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_bytes(payload.encode())[:16]


def catalog_inventory(
    catalog: ParquetDataCatalog | str | Path,
    *,
    catalog_path: Path | str | None = None,
) -> dict[str, Any]:
    """Per-data-type row counts and first/last ``ts_event`` for a catalog.

    A type whose rows cannot be read is reported with ``rows: None`` plus a
    named reason -- a fabricated zero would let an empty catalog pass a
    "matches the tape" check.
    """
    cat = catalog if isinstance(catalog, ParquetDataCatalog) else ParquetDataCatalog(str(catalog))
    if catalog_path is None and not isinstance(catalog, ParquetDataCatalog):
        catalog_path = catalog

    data_types = sorted(cat.list_data_types())
    per_type: dict[str, Any] = {}
    for data_type in data_types:
        # sorted: list_instruments() order is not contractual, and an unstable
        # order made the inventory digest differ between two builds of the same
        # run (measured: 8d0cf762... vs b6d3a84a... on identical inputs).
        identifiers = sorted(str(i) for i in (cat.list_instruments(data_type) or []))
        entry: dict[str, Any] = {"identifiers": identifiers, "rows": None}
        if data_type == "instruments":
            # ``catalog.query("instruments")`` is not implemented in 2.0.0rc4
            # (raises IndexError); the typed accessor is the supported read.
            try:
                records = list(cat.instruments())
            except Exception as exc:  # pragma: no cover - defensive
                entry["rows_reason"] = f"QUERY_FAILED:{type(exc).__name__}"
                per_type[data_type] = entry
                continue
            entry["rows"] = len(records)
            entry["identifiers"] = sorted(str(i.id) for i in records)
            entry["first_ts_event_ns"] = None
            entry["last_ts_event_ns"] = None
            entry["timestamp_reason"] = "INSTRUMENTS_CARRY_NO_MARKET_TIMESTAMP"
            per_type[data_type] = entry
            continue
        try:
            records = (
                list(cat.query(data_type, identifiers=identifiers))
                if identifiers
                else list(cat.query(data_type))
            )
        except Exception as exc:
            entry["rows_reason"] = f"QUERY_FAILED:{type(exc).__name__}"
            per_type[data_type] = entry
            continue
        entry["rows"] = len(records)
        stamps = [
            int(rec.ts_event) for rec in records if getattr(rec, "ts_event", None) is not None
        ]
        if stamps:
            entry["first_ts_event_ns"] = min(stamps)
            entry["last_ts_event_ns"] = max(stamps)
        else:
            entry["first_ts_event_ns"] = None
            entry["last_ts_event_ns"] = None
            entry["timestamp_reason"] = "NO_TS_EVENT_FIELD"
        per_type[data_type] = entry
    body = {
        "catalog_path": str(catalog_path),
        "data_types": data_types,
        "per_type": per_type,
    }
    body["inventory_digest"] = _sha256_bytes(
        json.dumps(per_type, sort_keys=True, separators=(",", ":")).encode()
    )
    return body


def source_window(tape: MultiTape, limit: int | None) -> dict[str, Any]:
    """First/last bar timestamp and per-leg counts for the loaded window."""
    per_leg = {name: len(candles) for name, candles in sorted(tape.candles.items())}
    starts = [candles[0].end_ns for candles in tape.candles.values() if candles]
    ends = [candles[-1].end_ns for candles in tape.candles.values() if candles]
    return {
        "tape_path": tape.tape_path,
        "tape_sha256": tape.tape_sha256,
        "limit": limit,
        "legs": sorted(tape.candles),
        "bars_per_leg": per_leg,
        "total_bars": sum(per_leg.values()),
        "first_bar_end_ns": min(starts) if starts else None,
        "last_bar_end_ns": max(ends) if ends else None,
        "funding_rows_available": len(tape.funding),
        "funding_coverage_in_catalog": FUNDING_CATALOG_SUPPORT,
    }


def write_bars_from_tape(
    catalog: ParquetDataCatalog,
    tape: MultiTape,
    *,
    legs: tuple[str, ...] | None = None,
    currency: str = "USDT",
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
) -> dict[str, Any]:
    """Write the tape's CryptoPerpetual instruments and 1h EXTERNAL bars."""
    from nautilus_trader.model import BarType, Currency

    curr = Currency.from_str(currency)
    wanted = sorted(legs) if legs is not None else sorted(tape.candles)
    instruments: list[Any] = []
    bar_count = 0
    bars_by_type: dict[str, int] = {}
    for raw in wanted:
        candles = tape.candles.get(raw)
        if not candles:
            raise ValueError(f"leg {raw!r} absent from tape")
        instrument_id = f"{raw}-PERP.BINANCE"
        base = base_currency(raw)
        instruments.append(_instrument(instrument_id, raw, base, curr, maker_fee, taker_fee))
        bar_type = BarType.from_str(bar_type_str(instrument_id))
        bars = _bars(candles, bar_type)
        catalog.write_bars(bars)
        bars_by_type[bar_type_str(instrument_id)] = len(bars)
        bar_count += len(bars)
    catalog.write_instruments(instruments)
    return {
        "instruments_written": [str(i.id) for i in instruments],
        "bars_written": bar_count,
        "bars_by_type": bars_by_type,
    }


def write_trades_from_archives(
    catalog: ParquetDataCatalog,
    *,
    legs: tuple[str, ...],
    instrument_ids: dict[str, str],
    start_ns: int,
    end_ns: int,
    max_ticks: int | None,
    trade_dirs: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Write a bounded TradeTick slice sourced from verified aggTrades archives.

    The daily archives carry ~0.8M BTC rows/day, so the full 5-day window is not
    a bounded operation on this machine (swap is saturated). ``max_ticks`` caps
    what is written; the reported ``coverage`` states exactly how much of the
    window that is, never implying the whole window was fed.
    """
    dirs = trade_dirs if trade_dirs is not None else AGG_TRADES_DIRS
    per_leg: dict[str, Any] = {}
    tick_count = 0
    digest: str | None = None
    for raw in sorted(legs):
        source = dirs.get(raw)
        if source is None:
            per_leg[raw] = {"ticks": None, "reason": "NO_ARCHIVE_DIRECTORY_CONFIGURED"}
            continue
        if not Path(source).exists():
            per_leg[raw] = {"ticks": None, "reason": "ARCHIVE_DIRECTORY_ABSENT"}
            continue
        ticks, stats = trades_from_dump(
            source, instrument_ids[raw], start_ns=start_ns, end_ns=end_ns
        )
        selected: tuple[TradeTick, ...] = ticks
        truncated = False
        if max_ticks is not None and len(ticks) > max_ticks:
            selected = ticks[:max_ticks]
            truncated = True
        if selected:
            catalog.write_trade_ticks(list(selected))
        leg_digest = trades_digest(selected)
        digest = leg_digest if digest is None else _sha256_bytes(f"{digest}:{leg_digest}".encode())
        tick_count += len(selected)
        per_leg[raw] = {
            **stats,
            "ticks": len(selected),
            "in_window_available": stats["loaded"],
            "truncated_by_max_ticks": truncated,
            "trades_digest": leg_digest,
        }
    return {"ticks_written": tick_count, "per_leg": per_leg, "trades_digest": digest}


@dataclass(frozen=True)
class CatalogBuild:
    """Result of one catalog build: paths, provenance, and the read-back digest."""

    run_id: str
    catalog_path: str
    source: dict[str, Any]
    written: dict[str, Any]
    inventory: dict[str, Any]
    wall_time_s: float
    role_contract: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "catalog_path": self.catalog_path,
            "source": self.source,
            "written": self.written,
            "inventory": self.inventory,
            "wall_time_s": self.wall_time_s,
            "role_contract": self.role_contract,
        }


def build_catalog(
    destination_base: Path | None = None,
    *,
    tape_path: Path | str | None = None,
    limit: int | None = 120,
    legs: tuple[str, ...] | None = None,
    currency: str = "USDT",
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    include_trades: bool = False,
    trade_start_ns: int | None = None,
    trade_end_ns: int | None = None,
    max_ticks: int | None = 50_000,
    trade_dirs: dict[str, Path] | None = None,
    run_id: str | None = None,
    manifest: TapeManifest | None = None,
    require_protected_final: bool = False,
) -> CatalogBuild:
    """Materialise a run-scoped catalog under the gitignored artifacts tree.

    Returns a :class:`CatalogBuild` whose ``inventory`` was read back from the
    written catalog (not echoed from the inputs), so a reader can compare it
    against the source tape and prove what the catalog actually holds.

    ``manifest`` (NX01.R5) binds the build to a revision-bound tape identity: the
    physical tape is re-hashed and any disagreement fails closed. The manifest's
    role contract is carried on the result, so a catalog consumer cannot present
    a burned or usage-unknown window as a protected final; with
    ``require_protected_final`` that claim is refused outright.
    """
    started = time.monotonic()
    tape_path = resolve_tape_path(tape_path)
    tape = load_multitape(tape_path, limit=limit)
    role_contract: dict[str, Any] | None = None
    if manifest is not None:
        ok, reason = verify_manifest_against_file(manifest, tape_path)
        if not ok:
            raise ValueError(f"tape manifest does not match the physical tape: {reason}")
        if manifest.tape_sha256 != tape.tape_sha256:
            raise ValueError(
                "manifest/loader tape hash disagreement: "
                f"{manifest.tape_sha256} != {tape.tape_sha256}"
            )
        if require_protected_final and not manifest.final_eligible:
            raise ValueError(
                "this build claims a protected final window, but the manifest says "
                "final_eligible=false; refusing to open it"
            )
        role_contract = {
            "manifest_identity": manifest.identity_digest(),
            "data_id": manifest.data_id,
            "final_eligible": manifest.final_eligible,
            "roles": dict(sorted(manifest.role_map.items())),
            "protected_final_claim_refused": require_protected_final
            and not manifest.final_eligible,
        }
    wanted = tuple(sorted(legs)) if legs is not None else tuple(sorted(tape.candles))
    instrument_ids = {raw: f"{raw}-PERP.BINANCE" for raw in wanted}

    trades_digest_value: str | None = None
    if include_trades:
        if trade_start_ns is None or trade_end_ns is None:
            raise ValueError("include_trades requires trade_start_ns and trade_end_ns")
        trades_digest_value = f"window:{trade_start_ns}:{trade_end_ns}:max:{max_ticks}"

    rid = run_id or compute_run_id(
        tape_sha256=tape.tape_sha256,
        limit=limit,
        legs=wanted,
        include_trades=include_trades,
        trades_digest_value=trades_digest_value,
    )
    out_dir = default_catalog_root(destination_base) / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(out_dir))

    written: dict[str, Any] = write_bars_from_tape(
        catalog,
        tape,
        legs=wanted,
        currency=currency,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )
    if include_trades:
        written["trades"] = write_trades_from_archives(
            catalog,
            legs=wanted,
            instrument_ids=instrument_ids,
            start_ns=int(trade_start_ns),  # type: ignore[arg-type]
            end_ns=int(trade_end_ns),  # type: ignore[arg-type]
            max_ticks=max_ticks,
            trade_dirs=trade_dirs,
        )

    return CatalogBuild(
        run_id=rid,
        catalog_path=str(out_dir),
        source=source_window(tape, limit),
        written=written,
        inventory=catalog_inventory(catalog, catalog_path=out_dir),
        wall_time_s=round(time.monotonic() - started, 3),
        role_contract=role_contract,
    )


def load_bars(catalog_path: Path | str, bar_types: tuple[str, ...]) -> list[Bar]:
    """Read back the bars of the given bar types (for tape-vs-catalog checks)."""
    catalog = ParquetDataCatalog(str(catalog_path))
    return list(catalog.query_bars(list(bar_types)))
