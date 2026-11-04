"""Transactional research metadata, experiment lineage, and evidence reconciliation.

Not an execution ledger or authority issuer.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

try:
    duckdb: Any = importlib.import_module("duckdb")
except ImportError:  # pragma: no cover
    duckdb = None


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


DataRole = Literal["DEVELOPMENT", "HOLDOUT", "PROSPECTIVE"]


class TrialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    family: str
    policy_hash: str
    dataset_hash: str
    role: DataRole
    registered_ns: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_non_empty(self) -> Self:
        if not self.trial_id.strip():
            raise ValueError("trial_id cannot be empty")
        if not self.family.strip():
            raise ValueError("family cannot be empty")
        if not self.policy_hash.strip():
            raise ValueError("policy_hash cannot be empty")
        if not self.dataset_hash.strip():
            raise ValueError("dataset_hash cannot be empty")
        return self


class DatasetWindowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_hash: str
    instrument_id: str
    start_ns: StrictInt = Field(ge=0)
    end_ns: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if not self.dataset_hash.strip():
            raise ValueError("dataset_hash cannot be empty")
        if not self.instrument_id.strip():
            raise ValueError("invalid dataset coverage")
        if not (0 <= self.start_ns < self.end_ns):
            raise ValueError("invalid dataset coverage")
        return self


class BurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lineage: str
    dataset_hash: str
    burned_ns: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_non_empty(self) -> Self:
        if not self.lineage.strip():
            raise ValueError("lineage cannot be empty")
        if not self.dataset_hash.strip():
            raise ValueError("dataset_hash cannot be empty")
        return self


class HoldoutRecord(BaseModel):
    """Enriched view of a protected holdout trial, its dataset coverage, and burn status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    family: str
    dataset_hash: str
    instrument_id: str | None = None
    start_ns: StrictInt | None = None
    end_ns: StrictInt | None = None
    registered_ns: StrictInt = Field(ge=0)
    is_burned: bool = False
    burned_ns: StrictInt | None = None


class DependencyGroupProof(BaseModel):
    """Collapse proof for witnesses within a single dependency group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dependency_group: str
    raw_evidence_count: StrictInt = Field(ge=0)
    kinds: tuple[str, ...]
    effective_stance: str


class ReconciliationReceipt(BaseModel):
    """Immutable evidence authority token for stance reconciliation (Mutabakat Fişi, D-132)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str
    opportunity_id: str
    reconciler_algorithm_id: str = "v8-reconciler-independent-groups"
    reconciler_algorithm_version: str = "1.0.0-v8next"
    reconciliation_ns: StrictInt = Field(ge=0)
    participating_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    dependency_group_proofs: tuple[DependencyGroupProof, ...] = Field(default_factory=tuple)
    aggregate_stance: str
    digest: str = ""

    @model_validator(mode="after")
    def compute_and_check_digest(self) -> Self:
        if not self.receipt_id.strip():
            raise ValueError("receipt_id cannot be empty")
        if not self.opportunity_id.strip():
            raise ValueError("opportunity_id cannot be empty")
        payload = {
            "receipt_id": self.receipt_id,
            "opportunity_id": self.opportunity_id,
            "reconciler_algorithm_id": self.reconciler_algorithm_id,
            "reconciler_algorithm_version": self.reconciler_algorithm_version,
            "reconciliation_ns": self.reconciliation_ns,
            "participating_evidence_ids": list(self.participating_evidence_ids),
            "dependency_group_proofs": [p.model_dump() for p in self.dependency_group_proofs],
            "aggregate_stance": self.aggregate_stance,
        }
        expected_digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        if self.digest and self.digest != expected_digest:
            raise ValueError("reconciliation receipt digest mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        return self


class ReplayReconciliationRecord(BaseModel):
    """Verification receipt comparing native execution state against replayed state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reconciliation_id: str
    run_id: str
    reconciled_ns: StrictInt = Field(ge=0)
    status: Literal["MATCHED", "DIVERGED"]
    expected_hash: str
    recovered_hash: str
    mismatches: tuple[str, ...] = Field(default_factory=tuple)
    digest: str = ""

    @model_validator(mode="after")
    def compute_and_check_digest(self) -> Self:
        if not self.reconciliation_id.strip():
            raise ValueError("reconciliation_id cannot be empty")
        if not self.run_id.strip():
            raise ValueError("run_id cannot be empty")
        payload = {
            "reconciliation_id": self.reconciliation_id,
            "run_id": self.run_id,
            "reconciled_ns": self.reconciled_ns,
            "status": self.status,
            "expected_hash": self.expected_hash,
            "recovered_hash": self.recovered_hash,
            "mismatches": list(self.mismatches),
        }
        expected_digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        if self.digest and self.digest != expected_digest:
            raise ValueError("replay reconciliation digest mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        return self


class CampaignObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str
    observed_ns: StrictInt = Field(ge=0)
    payload: dict[str, Any]
    digest: str = ""

    @model_validator(mode="after")
    def compute_and_check_digest(self) -> Self:
        text = canonical(self.payload)
        expected_digest = hashlib.sha256(text.encode()).hexdigest()
        if self.digest and self.digest != expected_digest:
            raise ValueError("campaign observation hash mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        return self


class DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    payload: dict[str, Any]
    digest: str = ""

    @model_validator(mode="after")
    def compute_and_check_digest(self) -> Self:
        text = canonical(self.payload)
        expected_digest = hashlib.sha256(text.encode()).hexdigest()
        if self.digest and self.digest != expected_digest:
            raise ValueError("decision hash mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        return self


class LifecycleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opportunity_id: str
    sequence: StrictInt = Field(ge=0)
    state: str
    decision_ns: StrictInt = Field(ge=0)
    reason: str


class ResearchStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS campaign_observations (
                campaign_id TEXT NOT NULL, observed_ns INTEGER NOT NULL,
                payload TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(campaign_id, observed_ns));
            CREATE TABLE IF NOT EXISTS trials (
                trial_id TEXT PRIMARY KEY, family TEXT NOT NULL,
                policy_hash TEXT NOT NULL, dataset_hash TEXT NOT NULL,
                role TEXT NOT NULL, registered_ns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS forward_plans (
                plan_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                digest TEXT NOT NULL, registered_ns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS forward_bindings (
                plan_id TEXT PRIMARY KEY REFERENCES forward_plans(plan_id),
                dataset_hash TEXT NOT NULL, bound_ns INTEGER NOT NULL);
            -- NX03: historical walk-forward plans are a different instrument from
            -- the prospective freeze above and get their own table. Nothing here
            -- can be read or written by the forward path (and vice versa).
            CREATE TABLE IF NOT EXISTS historical_plans (
                plan_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                digest TEXT NOT NULL, registered_ns INTEGER NOT NULL);
            -- NX05: one row per bound run identity (window/profile/code/config).
            -- A consumer looks a run key up here instead of trusting a directory.
            CREATE TABLE IF NOT EXISTS statistics_plans (
                family TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                pinned_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                digest TEXT NOT NULL, registered_ns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS dataset_windows (
                dataset_hash TEXT NOT NULL, instrument_id TEXT NOT NULL,
                start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL,
                PRIMARY KEY(dataset_hash, instrument_id));
            CREATE TABLE IF NOT EXISTS burns (
                lineage TEXT NOT NULL, dataset_hash TEXT NOT NULL,
                burned_ns INTEGER NOT NULL,
                PRIMARY KEY(lineage, dataset_hash));
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lifecycle (
                opportunity_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                state TEXT NOT NULL, decision_ns INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY(opportunity_id, sequence));
            CREATE TABLE IF NOT EXISTS reconciliation_receipts (
                receipt_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                reconciled_ns INTEGER NOT NULL,
                aggregate_stance TEXT NOT NULL,
                payload TEXT NOT NULL,
                digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS replay_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                reconciled_ns INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                digest TEXT NOT NULL);

            CREATE INDEX IF NOT EXISTS idx_trials_family ON trials(family);
            CREATE INDEX IF NOT EXISTS idx_trials_dataset ON trials(dataset_hash);
            CREATE INDEX IF NOT EXISTS idx_dataset_windows_inst ON dataset_windows(instrument_id);
            CREATE INDEX IF NOT EXISTS idx_burns_lineage ON burns(lineage, dataset_hash);
            CREATE INDEX IF NOT EXISTS idx_reconciliation_receipts_opp ON reconciliation_receipts(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_reconciliation_receipts_time ON reconciliation_receipts(reconciled_ns);
            CREATE INDEX IF NOT EXISTS idx_replay_reconciliations_run ON replay_reconciliations(run_id);
        """)

    def record_historical_plan(
        self,
        *,
        plan_id: str,
        payload: str,
        digest: str,
        registered_ns: int,
    ) -> tuple[str, str]:
        """Insert a historical walk-forward plan once; identical retries are idempotent.

        A different payload for an already-registered ``plan_id`` is refused: a
        historical plan is immutable evidence of what was planned, not a mutable
        configuration row.
        """
        if not plan_id.strip() or not payload.strip() or not digest.strip():
            raise ValueError("historical plan requires plan_id, payload and digest")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT payload,digest FROM historical_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if existing is not None and existing != (payload, digest):
                raise ValueError("registered historical plan cannot be rewritten")
            if existing is None:
                self.db.execute(
                    "INSERT INTO historical_plans VALUES (?,?,?,?)",
                    (plan_id, payload, digest, registered_ns),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return plan_id, digest

    def record_run(
        self,
        *,
        run_key: str,
        payload: str,
        digest: str,
        registered_ns: int,
    ) -> tuple[str, str]:
        """Register one run identity once; identical retries are idempotent.

        A different payload for an existing ``run_key`` is refused: the run key is
        content-addressed, so a mismatch means a caller is trying to bind different
        inputs to an identity that already means something else.
        """
        if not run_key.strip() or not payload.strip() or not digest.strip():
            raise ValueError("a run record requires run_key, payload and digest")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT payload,digest FROM runs WHERE run_key=?", (run_key,)
            ).fetchone()
            if existing is not None and existing != (payload, digest):
                raise ValueError("registered run identity cannot be rewritten")
            if existing is None:
                self.db.execute(
                    "INSERT INTO runs VALUES (?,?,?,?)", (run_key, payload, digest, registered_ns)
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return run_key, digest

    def get_run(self, run_key: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT payload,digest FROM runs WHERE run_key=?", (run_key,)
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def get_historical_plan(self, plan_id: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT payload,digest FROM historical_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def close(self) -> None:
        self.db.close()

    def record_campaign_observation(
        self,
        campaign_id: str | CampaignObservationRecord,
        observed_ns: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> CampaignObservationRecord:
        """Persist native observations, not an independent order state machine."""
        if isinstance(campaign_id, CampaignObservationRecord):
            record = campaign_id
        else:
            if observed_ns is None or payload is None:
                raise ValueError("campaign observation requires observed_ns and payload")
            record = CampaignObservationRecord(
                campaign_id=campaign_id, observed_ns=observed_ns, payload=payload
            )

        text = canonical(record.payload)
        digest = record.digest
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT payload,digest FROM campaign_observations WHERE campaign_id=? AND observed_ns=?",
                (record.campaign_id, record.observed_ns),
            ).fetchone()
            if existing is not None:
                if existing != (text, digest):
                    raise ValueError("campaign observation replay diverged")
            else:
                latest = self.db.execute(
                    "SELECT MAX(observed_ns) FROM campaign_observations WHERE campaign_id=?",
                    (record.campaign_id,),
                ).fetchone()[0]
                if latest is not None and record.observed_ns < latest:
                    raise ValueError("campaign observation moved backwards")
                self.db.execute(
                    "INSERT INTO campaign_observations VALUES (?,?,?,?)",
                    (record.campaign_id, record.observed_ns, text, digest),
                )
            self.db.execute("COMMIT")
            return record
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def list_campaign_observations(
        self, campaign_id: str | None = None
    ) -> list[CampaignObservationRecord]:
        query = (
            "SELECT campaign_id, observed_ns, payload, digest FROM campaign_observations "
            + ("WHERE campaign_id=? " if campaign_id else "")
            + "ORDER BY observed_ns, campaign_id"
        )
        params = (campaign_id,) if campaign_id else ()
        rows = self.db.execute(query, params).fetchall()
        return [
            CampaignObservationRecord(
                campaign_id=row[0],
                observed_ns=row[1],
                payload=json.loads(row[2]),
                digest=row[3],
            )
            for row in rows
        ]

    def register_trial(
        self,
        trial_id: str | TrialRecord,
        family: str | None = None,
        policy_hash: str | None = None,
        dataset_hash: str | None = None,
        role: str | None = None,
        registered_ns: int | None = None,
    ) -> TrialRecord:
        if isinstance(trial_id, TrialRecord):
            record = trial_id
        else:
            if role not in {"DEVELOPMENT", "HOLDOUT", "PROSPECTIVE"}:
                raise ValueError("unknown data role")
            if (
                family is None
                or policy_hash is None
                or dataset_hash is None
                or registered_ns is None
            ):
                raise ValueError("missing required trial fields")
            record = TrialRecord(
                trial_id=trial_id,
                family=family,
                policy_hash=policy_hash,
                dataset_hash=dataset_hash,
                role=role,  # type: ignore[arg-type]
                registered_ns=registered_ns,
            )

        values = (
            record.trial_id,
            record.family,
            record.policy_hash,
            record.dataset_hash,
            record.role,
            record.registered_ns,
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM trials WHERE trial_id=?", (record.trial_id,)
            ).fetchone()
            if existing is not None and existing != values:
                raise ValueError("trial identity cannot be rewritten")
            roles = {
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT role FROM trials WHERE dataset_hash=?", (record.dataset_hash,)
                )
            }
            if (record.role == "HOLDOUT" and roles - {"HOLDOUT"}) or (
                record.role != "HOLDOUT" and "HOLDOUT" in roles
            ):
                raise ValueError("dataset cannot mix protected holdout and observed research roles")
            if existing is None:
                self.db.execute("INSERT INTO trials VALUES (?,?,?,?,?,?)", values)
            self.db.execute("COMMIT")
            return record
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get_trial(self, trial_id: str) -> TrialRecord | None:
        row = self.db.execute("SELECT * FROM trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            return None
        return TrialRecord(
            trial_id=row[0],
            family=row[1],
            policy_hash=row[2],
            dataset_hash=row[3],
            role=row[4],
            registered_ns=row[5],
        )

    def list_trials(
        self, family: str | None = None, role: str | None = None
    ) -> list[TrialRecord]:
        conditions = []
        params: list[Any] = []
        if family is not None:
            conditions.append("family=?")
            params.append(family)
        if role is not None:
            conditions.append("role=?")
            params.append(role)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.execute(
            f"SELECT * FROM trials {where} ORDER BY family, registered_ns, trial_id", params
        ).fetchall()
        return [
            TrialRecord(
                trial_id=r[0],
                family=r[1],
                policy_hash=r[2],
                dataset_hash=r[3],
                role=r[4],
                registered_ns=r[5],
            )
            for r in rows
        ]

    def register_dataset_window(
        self,
        dataset_hash: str | DatasetWindowRecord,
        instrument_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> DatasetWindowRecord:
        """Conservative half-open source coverage, including feature warmup.

        This guards locally declared overlap, not unseen datasets or cross-asset
        information leakage. The caller derives coverage from verified input.
        """
        if isinstance(dataset_hash, DatasetWindowRecord):
            record = dataset_hash
        else:
            if not instrument_id or start_ns is None or end_ns is None or not 0 <= start_ns < end_ns:
                raise ValueError("invalid dataset coverage")
            record = DatasetWindowRecord(
                dataset_hash=dataset_hash,
                instrument_id=instrument_id,
                start_ns=start_ns,
                end_ns=end_ns,
            )

        self.db.execute("BEGIN IMMEDIATE")
        try:
            roles = {
                r[0]
                for r in self.db.execute(
                    "SELECT DISTINCT role FROM trials WHERE dataset_hash=?", (record.dataset_hash,)
                )
            }
            if not roles:
                raise ValueError("register trial before dataset coverage")
            previous = self.db.execute(
                "SELECT start_ns,end_ns FROM dataset_windows WHERE dataset_hash=? AND instrument_id=?",
                (record.dataset_hash, record.instrument_id),
            ).fetchone()
            if previous is not None and previous != (record.start_ns, record.end_ns):
                raise ValueError("dataset coverage cannot be rewritten")
            overlapping = {
                r[0]
                for r in self.db.execute(
                    "SELECT DISTINCT t.role FROM dataset_windows w JOIN trials t "
                    "ON t.dataset_hash=w.dataset_hash WHERE w.instrument_id=? "
                    "AND w.start_ns < ? AND w.end_ns > ?",
                    (record.instrument_id, record.end_ns, record.start_ns),
                )
            }
            if ("HOLDOUT" in roles and overlapping - {"HOLDOUT"}) or (
                roles - {"HOLDOUT"} and "HOLDOUT" in overlapping
            ):
                raise ValueError("overlapping observed and holdout dataset windows")
            if previous is None:
                self.db.execute(
                    "INSERT INTO dataset_windows VALUES (?,?,?,?)",
                    (record.dataset_hash, record.instrument_id, record.start_ns, record.end_ns),
                )
            self.db.execute("COMMIT")
            return record
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get_dataset_windows(
        self, dataset_hash: str | None = None, instrument_id: str | None = None
    ) -> list[DatasetWindowRecord]:
        conditions = []
        params: list[Any] = []
        if dataset_hash is not None:
            conditions.append("dataset_hash=?")
            params.append(dataset_hash)
        if instrument_id is not None:
            conditions.append("instrument_id=?")
            params.append(instrument_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.execute(
            f"SELECT dataset_hash, instrument_id, start_ns, end_ns FROM dataset_windows {where} ORDER BY dataset_hash, instrument_id",
            params,
        ).fetchall()
        return [
            DatasetWindowRecord(
                dataset_hash=r[0],
                instrument_id=r[1],
                start_ns=r[2],
                end_ns=r[3],
            )
            for r in rows
        ]

    def assert_no_holdout_overlap(self, instrument_id: str, start_ns: int, end_ns: int) -> None:
        """Reject declared protected coverage before consuming training evidence.

        Full source coverage includes warmup. This read-only check neither burns
        holdouts nor proves that undeclared datasets are clean.
        """
        if not instrument_id or not 0 <= start_ns < end_ns:
            raise ValueError("invalid training source coverage")
        overlap = self.db.execute(
            "SELECT 1 FROM dataset_windows w JOIN trials t ON t.dataset_hash=w.dataset_hash "
            "WHERE t.role='HOLDOUT' AND w.instrument_id=? AND w.start_ns < ? AND w.end_ns > ? LIMIT 1",
            (instrument_id, end_ns, start_ns),
        ).fetchone()
        if overlap is not None:
            raise ValueError("training source overlaps protected holdout")

    def family_size(self, family: str) -> int:
        result = self.db.execute("SELECT COUNT(*) FROM trials WHERE family=?", (family,)).fetchone()
        return int(result[0])

    def burn_holdout(
        self,
        lineage: str | BurnRecord,
        dataset_hash: str | None = None,
        decision_ns: int | None = None,
    ) -> BurnRecord:
        if isinstance(lineage, BurnRecord):
            record = lineage
        else:
            if dataset_hash is None or decision_ns is None:
                raise ValueError("burn_holdout requires dataset_hash and decision_ns")
            record = BurnRecord(lineage=lineage, dataset_hash=dataset_hash, burned_ns=decision_ns)

        self.db.execute(
            "INSERT OR IGNORE INTO burns VALUES (?,?,?)",
            (record.lineage, record.dataset_hash, record.burned_ns),
        )
        return record

    def holdout_pristine(self, lineage: str, dataset_hash: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM burns WHERE lineage=? AND dataset_hash=?", (lineage, dataset_hash)
            ).fetchone()
            is None
        )

    def list_burns(self, lineage: str | None = None) -> list[BurnRecord]:
        query = (
            "SELECT lineage, dataset_hash, burned_ns FROM burns "
            + ("WHERE lineage=? " if lineage else "")
            + "ORDER BY lineage, dataset_hash"
        )
        params = (lineage,) if lineage else ()
        rows = self.db.execute(query, params).fetchall()
        return [
            BurnRecord(lineage=r[0], dataset_hash=r[1], burned_ns=r[2])
            for r in rows
        ]

    def get_holdouts(self, lineage: str | None = None) -> list[HoldoutRecord]:
        """Query protected holdouts with coverage windows and burn records."""
        query = """
            SELECT t.trial_id, t.family, t.dataset_hash, w.instrument_id,
                   w.start_ns, w.end_ns, t.registered_ns, b.burned_ns
            FROM trials t
            LEFT JOIN dataset_windows w ON w.dataset_hash = t.dataset_hash
            LEFT JOIN burns b ON b.lineage = t.family AND b.dataset_hash = t.dataset_hash
            WHERE t.role = 'HOLDOUT'
        """
        params: list[Any] = []
        if lineage is not None:
            query += " AND t.family = ?"
            params.append(lineage)
        query += " ORDER BY t.family, t.registered_ns, t.trial_id"
        rows = self.db.execute(query, params).fetchall()
        return [
            HoldoutRecord(
                trial_id=r[0],
                family=r[1],
                dataset_hash=r[2],
                instrument_id=r[3],
                start_ns=r[4],
                end_ns=r[5],
                registered_ns=r[6],
                is_burned=r[7] is not None,
                burned_ns=r[7],
            )
            for r in rows
        ]

    def record_reconciliation_receipt(
        self, receipt: ReconciliationReceipt | dict[str, Any]
    ) -> bool:
        """Record an immutable reconciliation receipt (Mutabakat Fişi).

        Idempotent replay accepts identical receipts; divergent replay fails.
        """
        if isinstance(receipt, dict):
            record = ReconciliationReceipt.model_validate(receipt)
        else:
            record = receipt

        payload_dict = {
            "receipt_id": record.receipt_id,
            "opportunity_id": record.opportunity_id,
            "reconciler_algorithm_id": record.reconciler_algorithm_id,
            "reconciler_algorithm_version": record.reconciler_algorithm_version,
            "reconciliation_ns": record.reconciliation_ns,
            "participating_evidence_ids": list(record.participating_evidence_ids),
            "dependency_group_proofs": [p.model_dump() for p in record.dependency_group_proofs],
            "aggregate_stance": record.aggregate_stance,
        }
        text = canonical(payload_dict)
        digest = record.digest

        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT payload, digest FROM reconciliation_receipts WHERE receipt_id=?",
                (record.receipt_id,),
            ).fetchone()
            if row is not None:
                if row != (text, digest):
                    raise ValueError("reconciliation receipt replay diverged")
                self.db.execute("COMMIT")
                return False

            self.db.execute(
                "INSERT INTO reconciliation_receipts VALUES (?,?,?,?,?,?)",
                (
                    record.receipt_id,
                    record.opportunity_id,
                    record.reconciliation_ns,
                    record.aggregate_stance,
                    text,
                    digest,
                ),
            )
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get_reconciliation_receipt(self, receipt_id: str) -> ReconciliationReceipt | None:
        row = self.db.execute(
            "SELECT payload, digest FROM reconciliation_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data["digest"] = row[1]
        return ReconciliationReceipt.model_validate(data)

    def list_reconciliation_receipts(
        self, opportunity_id: str | None = None
    ) -> list[ReconciliationReceipt]:
        query = (
            "SELECT payload, digest FROM reconciliation_receipts "
            + ("WHERE opportunity_id=? " if opportunity_id else "")
            + "ORDER BY reconciled_ns, receipt_id"
        )
        params = (opportunity_id,) if opportunity_id else ()
        rows = self.db.execute(query, params).fetchall()
        results = []
        for text, digest in rows:
            data = json.loads(text)
            data["digest"] = digest
            results.append(ReconciliationReceipt.model_validate(data))
        return results

    def record_replay_reconciliation(
        self, record: ReplayReconciliationRecord | dict[str, Any]
    ) -> bool:
        """Record an accounting replay reconciliation slip."""
        if isinstance(record, dict):
            rec = ReplayReconciliationRecord.model_validate(record)
        else:
            rec = record

        payload_dict = {
            "reconciliation_id": rec.reconciliation_id,
            "run_id": rec.run_id,
            "reconciled_ns": rec.reconciled_ns,
            "status": rec.status,
            "expected_hash": rec.expected_hash,
            "recovered_hash": rec.recovered_hash,
            "mismatches": list(rec.mismatches),
        }
        text = canonical(payload_dict)
        digest = rec.digest

        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT payload, digest FROM replay_reconciliations WHERE reconciliation_id=?",
                (rec.reconciliation_id,),
            ).fetchone()
            if row is not None:
                if row != (text, digest):
                    raise ValueError("replay reconciliation diverged")
                self.db.execute("COMMIT")
                return False

            self.db.execute(
                "INSERT INTO replay_reconciliations VALUES (?,?,?,?,?,?)",
                (
                    rec.reconciliation_id,
                    rec.run_id,
                    rec.reconciled_ns,
                    rec.status,
                    text,
                    digest,
                ),
            )
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get_replay_reconciliation(
        self, reconciliation_id: str
    ) -> ReplayReconciliationRecord | None:
        row = self.db.execute(
            "SELECT payload, digest FROM replay_reconciliations WHERE reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data["digest"] = row[1]
        return ReplayReconciliationRecord.model_validate(data)

    def list_replay_reconciliations(
        self, run_id: str | None = None
    ) -> list[ReplayReconciliationRecord]:
        query = (
            "SELECT payload, digest FROM replay_reconciliations "
            + ("WHERE run_id=? " if run_id else "")
            + "ORDER BY reconciled_ns, reconciliation_id"
        )
        params = (run_id,) if run_id else ()
        rows = self.db.execute(query, params).fetchall()
        results = []
        for text, digest in rows:
            data = json.loads(text)
            data["digest"] = digest
            results.append(ReplayReconciliationRecord.model_validate(data))
        return results

    def record_decision(
        self, decision_id: str | DecisionRecord, payload: dict[str, Any] | None = None
    ) -> bool:
        """Idempotent replay accepts identical output only; divergent replay fails."""
        if isinstance(decision_id, DecisionRecord):
            record = decision_id
        else:
            if payload is None:
                raise ValueError("record_decision requires payload")
            record = DecisionRecord(decision_id=decision_id, payload=payload)

        text = canonical(record.payload)
        digest = record.digest
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT payload,digest FROM decisions WHERE decision_id=?", (record.decision_id,)
            ).fetchone()
            if row is not None:
                if row != (text, digest):
                    raise ValueError("non-deterministic or corrupted decision replay")
                self.db.execute("COMMIT")
                return False
            self.db.execute("INSERT INTO decisions VALUES (?,?,?)", (record.decision_id, text, digest))
            if "decision_ns" in record.payload:
                self._expire_observed_opportunities(int(record.payload["decision_ns"]))
            opportunity = record.payload.get("opportunity")
            if opportunity is not None:
                opportunity_id = opportunity["opportunity_id"]
                decision_ns = int(record.payload["decision_ns"])
                previous = self.db.execute(
                    "SELECT sequence,state,decision_ns FROM lifecycle WHERE opportunity_id=? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (opportunity_id,),
                ).fetchone()
                if previous is not None and decision_ns < previous[2]:
                    raise ValueError("opportunity observation moved backwards")
                if previous is None:
                    self.db.execute(
                        "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
                        (opportunity_id, 0, "DETECTED", decision_ns, "GRAMMAR_OBSERVATION"),
                    )
                    self.db.execute(
                        "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
                        (opportunity_id, 1, "PENDING", decision_ns, "AWAITING_ADMISSION"),
                    )
                    previous = (1, "PENDING", decision_ns)
                if previous[1] in {"DETECTED", "PENDING", "TRIGGERED"}:
                    terminal = None
                    if decision_ns >= opportunity["expires_ns"]:
                        terminal = "EXPIRED"
                    elif str(record.payload.get("reason", "")).startswith("REJECTED_"):
                        terminal = "REJECTED"
                    if terminal:
                        self.db.execute(
                            "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
                            (
                                opportunity_id,
                                previous[0] + 1,
                                terminal,
                                decision_ns,
                                record.payload["reason"],
                            ),
                        )
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        row = self.db.execute(
            "SELECT payload, digest FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        return DecisionRecord(
            decision_id=decision_id, payload=json.loads(row[0]), digest=row[1]
        )

    def list_decisions(self) -> list[DecisionRecord]:
        rows = self.db.execute("SELECT decision_id, payload, digest FROM decisions ORDER BY decision_id").fetchall()
        return [
            DecisionRecord(decision_id=r[0], payload=json.loads(r[1]), digest=r[2])
            for r in rows
        ]

    def _expire_observed_opportunities(self, decision_ns: int) -> None:
        """Advance observed deadlines inside the decision transaction, even without a signal."""
        due = self.db.execute(
            "SELECT DISTINCT json_extract(d.payload,'$.opportunity.opportunity_id'), "
            "l.sequence FROM decisions d JOIN lifecycle l ON l.opportunity_id = "
            "json_extract(d.payload,'$.opportunity.opportunity_id') "
            "WHERE json_extract(d.payload,'$.opportunity.expires_ns') <= ? "
            "AND l.state IN ('DETECTED','PENDING','TRIGGERED','ACCEPTED') "
            "AND l.decision_ns <= ? AND l.sequence = "
            "(SELECT MAX(sequence) FROM lifecycle WHERE opportunity_id=l.opportunity_id)",
            (decision_ns, decision_ns),
        ).fetchall()
        self.db.executemany(
            "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
            [
                (identity, sequence + 1, "EXPIRED", decision_ns, "DEADLINE_OBSERVED")
                for identity, sequence in due
            ],
        )

    def transition(
        self,
        opportunity_id: str | LifecycleRecord,
        state: str | None = None,
        decision_ns: int | None = None,
        reason: str | None = None,
    ) -> LifecycleRecord:
        """Economic lifecycle only. Engine owns order/fill state separately."""
        if isinstance(opportunity_id, LifecycleRecord):
            rec = opportunity_id
            target_opp = rec.opportunity_id
            target_state = rec.state
            target_ns = rec.decision_ns
            target_reason = rec.reason
        else:
            if state is None or decision_ns is None or reason is None:
                raise ValueError("transition requires state, decision_ns, and reason")
            target_opp = opportunity_id
            target_state = state
            target_ns = decision_ns
            target_reason = reason

        allowed = {
            None: {"DETECTED"},
            "DETECTED": {"PENDING", "REJECTED", "EXPIRED", "INVALIDATED"},
            "PENDING": {"TRIGGERED", "REJECTED", "EXPIRED", "INVALIDATED"},
            "TRIGGERED": {"ACCEPTED", "REJECTED", "EXPIRED", "INVALIDATED"},
            "ACCEPTED": {"CAMPAIGN_SUBMITTED", "REJECTED", "EXPIRED", "INVALIDATED"},
            "CAMPAIGN_SUBMITTED": {"COMPLETED", "CANCELLED"},
        }
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute(
                "SELECT sequence,state,decision_ns FROM lifecycle WHERE opportunity_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (target_opp,),
            ).fetchone()
            prior = previous[1] if previous else None
            if target_state not in allowed.get(prior, set()):
                raise ValueError("invalid or terminal lifecycle transition")
            if previous and target_ns < previous[2]:
                raise ValueError("lifecycle time moved backwards")
            sequence = previous[0] + 1 if previous else 0
            self.db.execute(
                "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
                (target_opp, sequence, target_state, target_ns, target_reason),
            )
            self.db.execute("COMMIT")
            return LifecycleRecord(
                opportunity_id=target_opp,
                sequence=sequence,
                state=target_state,
                decision_ns=target_ns,
                reason=target_reason,
            )
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def list_lifecycle_events(
        self, opportunity_id: str | None = None
    ) -> list[LifecycleRecord]:
        query = (
            "SELECT opportunity_id, sequence, state, decision_ns, reason FROM lifecycle "
            + ("WHERE opportunity_id=? " if opportunity_id else "")
            + "ORDER BY opportunity_id, sequence"
        )
        params = (opportunity_id,) if opportunity_id else ()
        rows = self.db.execute(query, params).fetchall()
        return [
            LifecycleRecord(
                opportunity_id=r[0],
                sequence=r[1],
                state=r[2],
                decision_ns=r[3],
                reason=r[4],
            )
            for r in rows
        ]

    # -----------------------------------------------------------------------
    # DuckDB OLAP Integration
    # -----------------------------------------------------------------------

    def duckdb_connection(self, read_only: bool = True) -> Any:
        """Attach SQLite store into an in-memory DuckDB session for OLAP queries."""
        if duckdb is None:
            raise ImportError(
                "duckdb is not installed. Install with: pip install 'duckdb>=1.0'"
            )
        conn = duckdb.connect()
        flag = ", READ_ONLY" if read_only else ""
        resolved = str(self.path.resolve()).replace("'", "''")
        conn.execute(f"ATTACH '{resolved}' AS research (TYPE SQLITE{flag})")
        return conn

    def query_duckdb(
        self, query: str, read_only: bool = True
    ) -> list[tuple[Any, ...]]:
        """Execute a DuckDB SQL query against the attached store (prefixed with research.<table_name>)."""
        conn = self.duckdb_connection(read_only=read_only)
        try:
            return cast(list[tuple[Any, ...]], conn.execute(query).fetchall())
        finally:
            conn.close()

    def to_polars(self, table_name: str) -> Any:
        """Export any store table as a Polars DataFrame using DuckDB or direct fetch."""
        import polars as pl

        if duckdb is not None:
            conn = self.duckdb_connection(read_only=True)
            try:
                cursor = conn.execute(f"SELECT * FROM research.{table_name}")
                cols = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                return pl.DataFrame(rows, schema=cols, orient="row")
            finally:
                conn.close()

        # Fallback to direct SQLite read if DuckDB is absent
        cursor = self.db.execute(f"SELECT * FROM {table_name}")
        cols = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return pl.DataFrame(rows, schema=cols, orient="row")


    def export_duckdb(self, destination: Path | str) -> None:
        """Materialize all SQLite tables into a persistent DuckDB database file."""
        if duckdb is None:
            raise ImportError(
                "duckdb is not installed. Install with: pip install 'duckdb>=1.0'"
            )
        dest_path = Path(destination)
        if dest_path.resolve() == self.path.resolve() or (
            dest_path.exists() and os.path.samefile(dest_path, self.path)
        ):
            raise ValueError("export destination must not be the source SQLite store")

        tables = [
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]

        fd, temporary = tempfile.mkstemp(prefix=f".{dest_path.name}.", suffix=".duckdb", dir=dest_path.parent)
        os.close(fd)
        temp_path = Path(temporary)
        temp_path.unlink()  # DuckDB expects a new database, not an empty file.
        try:
            conn = duckdb.connect(str(temp_path))
            try:
                try:
                    resolved = str(self.path.resolve()).replace("'", "''")
                    conn.execute(f"ATTACH '{resolved}' AS src (TYPE SQLITE, READ_ONLY true)")
                    for table in tables:
                        conn.execute(f"CREATE TABLE {table} AS SELECT * FROM src.{table}")
                except Exception:
                    # Discard partial scanner output before the direct SQLite fallback.
                    for table in tables:
                        quoted = '"' + table.replace('"', '""') + '"'
                        conn.execute(f"DROP TABLE IF EXISTS {quoted}")
                        columns = self.db.execute(f"PRAGMA table_info({quoted})").fetchall()
                        types = {"INTEGER": "BIGINT", "TEXT": "VARCHAR", "REAL": "DOUBLE", "BLOB": "BLOB"}
                        definitions = []
                        for column in columns:
                            name, declared = column[1], column[2].upper()
                            if declared not in types:
                                raise ValueError(f"unsupported SQLite export type: {declared!r}") from None
                            name_sql = '"' + name.replace('"', '""') + '"'
                            definitions.append(f"{name_sql} {types[declared]}")
                        conn.execute(f"CREATE TABLE {quoted} ({', '.join(definitions)})")
                        cursor = self.db.execute(f"SELECT * FROM {quoted}")
                        placeholders = ", ".join("?" for _ in columns)
                        while batch := cursor.fetchmany(1000):
                            conn.executemany(f"INSERT INTO {quoted} VALUES ({placeholders})", batch)
            finally:
                conn.close()
            os.replace(temp_path, dest_path)
        finally:
            temp_path.unlink(missing_ok=True)



