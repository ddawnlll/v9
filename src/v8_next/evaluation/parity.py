"""Policy-bound external parity adapters (D-153 §§2.6, 3.2-3.3, 4.6, D-116).

Epistemic & Parity Invariants:
1. Production evaluation has NO fixed-vector paths or hardcoded arrays.
2. Every parity evaluation is bound to physical artifacts on disk via SHA-256 (ArtifactBinding).
3. Missing, corrupt, empty, or unhashable artifacts yield DATA_BLOCKED, never 0 difference.
4. Floating-point parity is exact (IEEE-754 bit-pattern equality), tolerance comparison is forbidden.
5. Adapters are diagnostic instruments, carrying ZERO economic authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from enum import StrEnum
from pathlib import Path

import polars as pl
from pydantic import BaseModel, ConfigDict

MAPPING_VERSION = "semantic-mapping-v1"
PARITY_IDENTITY_VERSION = "parity-adapter-v1"
NON_SOVEREIGN_INSTRUMENT_STATUS = "external_instrument_non_sovereign"


class ReferenceEngine(StrEnum):
    LEAN = "QuantConnect-LEAN"
    SKFOLIO = "skfolio"
    VECTORBT = "vectorbt"
    RUST_V8_CORE = "v8-core-oracle"


class ParityOutcomeKind(StrEnum):
    EXACT_MATCH = "PARITY_EXACT_MATCH"
    DIVERGED = "PARITY_DIVERGED"
    UNSUPPORTED_SEMANTICS = "PARITY_UNSUPPORTED_SEMANTICS"
    UNPAIRED_RECORDS = "PARITY_UNPAIRED_RECORDS"
    DATA_BLOCKED = "DATA_BLOCKED"


class ArtifactBinding(BaseModel):
    """Immutable cryptographic binding to a physical file on disk."""

    model_config = ConfigDict(frozen=True)

    role: str
    path: str
    sha256_hex: str
    bytes: int

    @classmethod
    def from_file(cls, role: str, path: str | Path) -> ArtifactBinding:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Artifact file not found on disk: {p}")
        content = p.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        return cls(role=role, path=str(p.resolve()), sha256_hex=digest, bytes=len(content))

    def verify(self) -> tuple[bool, str]:
        p = Path(self.path)
        if not p.is_file():
            return False, f"FILE_MISSING: {self.path}"
        content = p.read_bytes()
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != self.sha256_hex:
            return False, f"HASH_MISMATCH: expected {self.sha256_hex}, got {actual_digest}"
        if len(content) != self.bytes:
            return False, f"SIZE_MISMATCH: expected {self.bytes} bytes, got {len(content)}"
        return True, "OK"


class ParitySubject(BaseModel):
    """Policy and case identity that a parity result belongs to."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_hash: str
    policy_id: str
    commit_hash: str
    binary_digest: str
    family: str = "trend"

    def missing_fields(self) -> list[str]:
        out = []
        for field in ("case_id", "case_hash", "policy_id", "commit_hash", "binary_digest"):
            if not getattr(self, field, "").strip():
                out.append(field)
        return out


class SemanticMapping(BaseModel):
    """Rules for pairing records and translating schemas across two engines."""

    model_config = ConfigDict(frozen=True)

    mapping_version: str = MAPPING_VERSION
    pairing_key: str = "trade_id"
    supported_order_types: tuple[str, ...] = ("MARKET", "LIMIT", "STOP_MARKET")
    pnl_field: str = "pnl"
    fill_time_field: str | None = "fill_time_ns"
    sequence_field: str | None = None

    def mapping_hash(self) -> str:
        types = sorted(self.supported_order_types)
        obj = [
            "SemanticMapping",
            self.mapping_version,
            self.pairing_key,
            self.pnl_field,
            types,
            self.fill_time_field,
            self.sequence_field,
        ]
        return hashlib.sha256(json.dumps(obj, separators=(",", ":")).encode()).hexdigest()

    def supports_order_type(self, order_type: str) -> bool:
        return order_type in self.supported_order_types


class EngineVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    engine: ReferenceEngine
    version: str
    engine_build_hash: str | None = None

    @classmethod
    def create(cls, engine: ReferenceEngine, version: str, build_hash: str | None = None) -> EngineVersion:
        if not version.strip() or version.strip() in ("PLACEHOLDER", "UNKNOWN", "TODO"):
            raise ValueError(f"Invalid engine version: {version}")
        if build_hash is not None and (not build_hash.strip() or build_hash.strip() in ("PLACEHOLDER", "UNKNOWN")):
            raise ValueError(f"Invalid engine build hash: {build_hash}")
        return cls(engine=engine, version=version, engine_build_hash=build_hash)

    def identity(self) -> str:
        if self.engine_build_hash:
            return f"{self.engine.value}@{self.version}+{self.engine_build_hash}"
        return f"{self.engine.value}@{self.version}"


class ParityDiagnostics(BaseModel):
    """Diagnostic divergence measurements. Forbidden from use as pass/fail gate."""

    model_config = ConfigDict(frozen=True)

    paired_records: int
    mismatched_records: int
    mean_abs_divergence_bps: float | None = None
    max_abs_divergence_bps: float | None = None
    terminal_divergence_bps: float | None = None
    terminal_sign_disagreement: bool = False
    fill_timing_mae_ms: float | None = None
    max_drawdown_divergence_bps: float | None = None


class ParityReceipt(BaseModel):
    """Self-verifying parity evaluation receipt bound to physical artifacts."""

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    identity_version: str = PARITY_IDENTITY_VERSION
    subject: ParitySubject
    mapping_hash: str
    engine_identity: str
    native_artifact: ArtifactBinding
    reference_artifact: ArtifactBinding
    outcome: ParityOutcomeKind
    detail: str
    diagnostics: ParityDiagnostics | None = None
    computed_at_timestamp_ns: int
    instrument_status: str = NON_SOVEREIGN_INSTRUMENT_STATUS
    reconciliation_gaps: tuple[str, ...] = (
        "D-116 monetary accounting (commissions, funding, wallet balance) uncompared",
    )

    @classmethod
    def create(
        cls,
        subject: ParitySubject,
        mapping: SemanticMapping,
        engine: EngineVersion,
        native_artifact: ArtifactBinding,
        reference_artifact: ArtifactBinding,
        outcome: ParityOutcomeKind,
        detail: str,
        computed_at_timestamp_ns: int,
        diagnostics: ParityDiagnostics | None = None,
    ) -> ParityReceipt:
        m_hash = mapping.mapping_hash()
        e_id = engine.identity()

        canon = [
            "ParityReceipt",
            PARITY_IDENTITY_VERSION,
            subject.policy_id,
            subject.case_id,
            m_hash,
            e_id,
            native_artifact.sha256_hex,
            reference_artifact.sha256_hex,
            outcome.value,
            detail,
            computed_at_timestamp_ns,
        ]
        receipt_id = hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()

        return cls(
            receipt_id=receipt_id,
            subject=subject,
            mapping_hash=m_hash,
            engine_identity=e_id,
            native_artifact=native_artifact,
            reference_artifact=reference_artifact,
            outcome=outcome,
            detail=detail,
            diagnostics=diagnostics,
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    def is_agreement(self) -> bool:
        return self.outcome == ParityOutcomeKind.EXACT_MATCH


def _load_ledger_polars(path: Path, mapping: SemanticMapping) -> pl.DataFrame:
    """Load jsonl / json / parquet ledger using Polars."""
    if path.suffix == ".parquet":
        df = pl.read_parquet(path)
    else:
        # Default JSON lines
        try:
            df = pl.read_ndjson(path)
        except Exception:
            df = pl.read_json(path)
    return df


def evaluate_parity(
    subject: ParitySubject,
    mapping: SemanticMapping,
    engine: EngineVersion,
    native_binding: ArtifactBinding,
    reference_binding: ArtifactBinding,
    computed_at_timestamp_ns: int,
) -> ParityReceipt:
    """Evaluate exact parity between two physical ledgers using Polars."""
    # 1. Subject validation
    missing = subject.missing_fields()
    if missing:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail=f"BLOCKED_PARITY_SUBJECT_INCOMPLETE: {','.join(missing)}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 2. Physical artifact verification (tamper-at-rest guard)
    ok_nat, err_nat = native_binding.verify()
    if not ok_nat:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail=f"DATA_BLOCKED_PARITY_ARTIFACT [native]: {err_nat}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    ok_ref, err_ref = reference_binding.verify()
    if not ok_ref:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail=f"DATA_BLOCKED_PARITY_ARTIFACT [reference]: {err_ref}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 3. Pre-check empty files
    if native_binding.bytes == 0:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail="DATA_BLOCKED_PARITY_LEDGER_EMPTY [native]",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )
    if reference_binding.bytes == 0:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail="DATA_BLOCKED_PARITY_LEDGER_EMPTY [reference]",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 4. Load ledgers into Polars
    try:
        df_nat = _load_ledger_polars(Path(native_binding.path), mapping)
        df_ref = _load_ledger_polars(Path(reference_binding.path), mapping)
    except Exception as e:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail=f"DATA_BLOCKED_PARITY_ARTIFACT_UNREADABLE: {e}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 4. Check empty ledgers
    if df_nat.height == 0:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail="DATA_BLOCKED_PARITY_LEDGER_EMPTY [native]",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )
    if df_ref.height == 0:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail="DATA_BLOCKED_PARITY_LEDGER_EMPTY [reference]",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 5. Check pairing key & pnl field existence
    k = mapping.pairing_key
    pnl_col = mapping.pnl_field
    for name, df in (("native", df_nat), ("reference", df_ref)):
        if k not in df.columns:
            return ParityReceipt.create(
                subject=subject,
                mapping=mapping,
                engine=engine,
                native_artifact=native_binding,
                reference_artifact=reference_binding,
                outcome=ParityOutcomeKind.DATA_BLOCKED,
                detail=f"DATA_BLOCKED_PARITY_RECORD_INVALID [{name}]: missing key '{k}'",
                computed_at_timestamp_ns=computed_at_timestamp_ns,
            )
        if pnl_col not in df.columns:
            return ParityReceipt.create(
                subject=subject,
                mapping=mapping,
                engine=engine,
                native_artifact=native_binding,
                reference_artifact=reference_binding,
                outcome=ParityOutcomeKind.DATA_BLOCKED,
                detail=f"DATA_BLOCKED_PARITY_RECORD_INVALID [{name}]: missing pnl field '{pnl_col}'",
                computed_at_timestamp_ns=computed_at_timestamp_ns,
            )

    # 6. Check duplicate keys (Ambiguous keys fail-closed)
    if df_nat.get_column(k).n_unique() != df_nat.height:
        dups = df_nat.get_column(k).filter(df_nat.get_column(k).is_duplicated()).to_list()
        dup_val = dups[0] if dups else "unknown"
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail=f"BLOCKED_PARITY_AMBIGUOUS_KEYS [native]: duplicate pairing key {dup_val}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )
    if df_ref.get_column(k).n_unique() != df_ref.height:
        dups = df_ref.get_column(k).filter(df_ref.get_column(k).is_duplicated()).to_list()
        dup_val = dups[0] if dups else "unknown"
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail=f"BLOCKED_PARITY_AMBIGUOUS_KEYS [reference]: duplicate pairing key {dup_val}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 7. Check non-finite values in pnl
    pnl_nat_vals = df_nat.get_column(pnl_col).to_list()
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in pnl_nat_vals):
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail="DATA_BLOCKED_PARITY_RECORD_INVALID [native]: non-finite pnl",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )
    pnl_ref_vals = df_ref.get_column(pnl_col).to_list()
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in pnl_ref_vals):
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.DATA_BLOCKED,
            detail="DATA_BLOCKED_PARITY_RECORD_INVALID [reference]: non-finite pnl",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 8. Check order type semantics support if present
    for _name, df in (("native", df_nat), ("reference", df_ref)):
        if "order_type" in df.columns:
            order_types = df.get_column("order_type").drop_nulls().unique().to_list()
            for ot in order_types:
                if not mapping.supports_order_type(str(ot)):
                    return ParityReceipt.create(
                        subject=subject,
                        mapping=mapping,
                        engine=engine,
                        native_artifact=native_binding,
                        reference_artifact=reference_binding,
                        outcome=ParityOutcomeKind.UNSUPPORTED_SEMANTICS,
                        detail=f"PARITY_UNSUPPORTED_SEMANTICS: {ot}",
                        computed_at_timestamp_ns=computed_at_timestamp_ns,
                    )

    # 9. Full outer join on pairing_key with Polars
    df_nat_prep = df_nat.select(
        [
            pl.col(k).cast(pl.Utf8).alias("_key"),
            pl.col(pnl_col).cast(pl.Float64).alias("_pnl_nat"),
            (pl.col(mapping.fill_time_field).cast(pl.Int64).alias("_time_nat"))
            if mapping.fill_time_field and mapping.fill_time_field in df_nat.columns
            else pl.lit(None, dtype=pl.Int64).alias("_time_nat"),
            (pl.col(mapping.sequence_field).cast(pl.Int64).alias("_seq_nat"))
            if mapping.sequence_field and mapping.sequence_field in df_nat.columns
            else pl.int_range(0, pl.len()).alias("_seq_nat"),
        ]
    )

    df_ref_prep = df_ref.select(
        [
            pl.col(k).cast(pl.Utf8).alias("_key"),
            pl.col(pnl_col).cast(pl.Float64).alias("_pnl_ref"),
            (pl.col(mapping.fill_time_field).cast(pl.Int64).alias("_time_ref"))
            if mapping.fill_time_field and mapping.fill_time_field in df_ref.columns
            else pl.lit(None, dtype=pl.Int64).alias("_time_ref"),
            (pl.col(mapping.sequence_field).cast(pl.Int64).alias("_seq_ref"))
            if mapping.sequence_field and mapping.sequence_field in df_ref.columns
            else pl.int_range(0, pl.len()).alias("_seq_ref"),
        ]
    )

    joined = df_nat_prep.join(df_ref_prep, on="_key", how="full", coalesce=True)

    native_only_count = joined.filter(pl.col("_pnl_ref").is_null()).height
    ref_only_count = joined.filter(pl.col("_pnl_nat").is_null()).height

    if native_only_count > 0 or ref_only_count > 0:
        return ParityReceipt.create(
            subject=subject,
            mapping=mapping,
            engine=engine,
            native_artifact=native_binding,
            reference_artifact=reference_binding,
            outcome=ParityOutcomeKind.UNPAIRED_RECORDS,
            detail=f"PARITY_UNPAIRED_RECORDS: native_only={native_only_count} reference_only={ref_only_count}",
            computed_at_timestamp_ns=computed_at_timestamp_ns,
        )

    # 10. Bit-level IEEE-754 floating point check
    paired = joined.filter(pl.col("_pnl_nat").is_not_null() & pl.col("_pnl_ref").is_not_null())
    pnl_nat = paired.get_column("_pnl_nat").to_list()
    pnl_ref = paired.get_column("_pnl_ref").to_list()
    n_pairs = len(pnl_nat)

    def to_bits(val: float) -> int:
        return int(struct.unpack(">Q", struct.pack(">d", float(val)))[0])

    bit_matches = [to_bits(a) == to_bits(b) for a, b in zip(pnl_nat, pnl_ref, strict=True)]
    exact_match = all(bit_matches)

    # Check fill time match if both present
    time_nat = paired.get_column("_time_nat").to_list()
    time_ref = paired.get_column("_time_ref").to_list()
    has_times = all(t1 is not None and t2 is not None for t1, t2 in zip(time_nat, time_ref, strict=True))
    time_errors_ms = []
    if has_times:
        time_errors_ms = [abs(t1 - t2) / 1_000_000.0 for t1, t2 in zip(time_nat, time_ref, strict=True)]
        if any(e > 0 for e in time_errors_ms):
            exact_match = False

    # Compute diagnostics
    divergences_bps = [abs(a - b) * 10_000.0 for a, b in zip(pnl_nat, pnl_ref, strict=True)]
    mismatched_records = sum(1 for m in bit_matches if not m)
    if has_times:
        mismatched_records = sum(1 for bm, te in zip(bit_matches, time_errors_ms, strict=True) if not bm or te > 0)

    mean_div = sum(divergences_bps) / n_pairs if n_pairs > 0 else None
    max_div = max(divergences_bps) if n_pairs > 0 else None
    term_nat = sum(pnl_nat)
    term_ref = sum(pnl_ref)
    term_div = abs(term_nat - term_ref) * 10_000.0
    sign_disagree = (term_nat > 0 and term_ref < 0) or (term_nat < 0 and term_ref > 0)
    mae_ms = (sum(time_errors_ms) / len(time_errors_ms)) if has_times and time_errors_ms else None

    # Equity curve drawdown divergence
    def max_drawdown_bps(pnl_series: list[float]) -> float:
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnl_series:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        return max_dd * 10_000.0

    dd_nat = max_drawdown_bps(pnl_nat)
    dd_ref = max_drawdown_bps(pnl_ref)
    dd_div = abs(dd_nat - dd_ref)

    diagnostics = ParityDiagnostics(
        paired_records=n_pairs,
        mismatched_records=mismatched_records,
        mean_abs_divergence_bps=mean_div,
        max_abs_divergence_bps=max_div,
        terminal_divergence_bps=term_div,
        terminal_sign_disagreement=sign_disagree,
        fill_timing_mae_ms=mae_ms,
        max_drawdown_divergence_bps=dd_div,
    )

    outcome = ParityOutcomeKind.EXACT_MATCH if exact_match else ParityOutcomeKind.DIVERGED
    detail = outcome.value

    return ParityReceipt.create(
        subject=subject,
        mapping=mapping,
        engine=engine,
        native_artifact=native_binding,
        reference_artifact=reference_binding,
        outcome=outcome,
        detail=detail,
        diagnostics=diagnostics,
        computed_at_timestamp_ns=computed_at_timestamp_ns,
    )
