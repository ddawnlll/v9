"""Freeze a future observation experiment, not a claim or execution permit."""

import hashlib
import json
import time
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from v8_next.domain.config import PaperConfig
from v8_next.domain.market import Candle
from v8_next.evaluation.store import ResearchStore, canonical


class ForwardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instrument_id: Literal["BTCUSDT-PERP.BINANCE"]
    start_ns: StrictInt = Field(gt=0)
    end_ns: StrictInt = Field(gt=0)
    policies: dict[str, PaperConfig] = Field(min_length=2)
    baseline: str
    block_size: StrictInt = Field(gt=0)
    reps: StrictInt = Field(ge=2)
    seed: StrictInt = Field(ge=0)
    warmup_bars: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid(self) -> Self:
        hour = 3600 * 10**9
        if self.start_ns >= self.end_ns or self.start_ns % hour or self.end_ns % hour:
            raise ValueError("explicit ordered hourly forward boundaries required")
        if self.start_ns - self.warmup_bars * hour < 0:
            raise ValueError("warmup extends before source epoch")
        if self.baseline not in self.policies or any(not k.strip() for k in self.policies):
            raise ValueError("named policies and a member baseline required")
        if self.block_size >= (self.end_ns - self.start_ns) // hour - 1:
            raise ValueError("bootstrap block must be shorter than planned sample")
        values = list(self.policies.values())
        if len({p.model_dump_json() for p in values}) != len(values):
            raise ValueError("duplicate policies cannot inflate planned family")
        if len({(p.initial_balance, p.maker_fee, p.taker_fee) for p in values}) != 1:
            raise ValueError("planned capital and fee assumptions must match")
        return self


def freeze_forward_plan(
    store: ResearchStore, plan_id: str, plan: ForwardPlan, code_and_lock_hash: str
) -> str:
    """Record current wall clock internally; caller cannot backdate registration.

    Local clock/database trust and external prior access remain unverified.
    Identical retries preserve the first record even after the window begins.
    """
    if not plan_id.strip() or not code_and_lock_hash.strip():
        raise ValueError("explicit plan identity and runtime hash required")
    plan = ForwardPlan.model_validate(plan.model_dump())
    payload = canonical(
        {
            "plan": plan.model_dump(mode="json"),
            "code_and_lock_hash": code_and_lock_hash,
            "version": "FORWARD_NATIVE_BAR_EXPERIMENT_V1",
        }
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    store.db.execute("BEGIN IMMEDIATE")
    try:
        existing = store.db.execute(
            "SELECT payload,digest FROM forward_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None:
            if existing != (payload, digest):
                raise ValueError("frozen forward plan cannot be rewritten")
        else:
            now = time.time_ns()
            if now >= plan.start_ns:
                raise ValueError("forward plan must be frozen before its observation window")
            store.db.execute(
                "INSERT INTO forward_plans VALUES (?,?,?,?)", (plan_id, payload, digest, now)
            )
        store.db.execute("COMMIT")
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    return digest


def bind_forward_data(
    store: ResearchStore,
    plan_id: str,
    dataset_hash: str,
    candles: tuple[Candle, ...],
    code_and_lock_hash: str,
) -> ForwardPlan:
    """Bind one complete future source window; no result or OOS certification.

    Caller must hash/verify the manifest and source bytes. A persisted binding
    survives later evaluation failure; a different dataset cannot replace it.
    """
    store.db.execute("BEGIN IMMEDIATE")
    try:
        row = store.db.execute(
            "SELECT payload,digest,registered_ns FROM forward_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("missing or corrupted forward plan")
        payload = json.loads(row[0])
        plan = ForwardPlan.model_validate(payload["plan"])
        if payload["code_and_lock_hash"] != code_and_lock_hash:
            raise ValueError("forward runtime differs from frozen plan")
        now = time.time_ns()
        if not row[2] < plan.start_ns < plan.end_ns <= now:
            raise ValueError("forward observation window not completed or not preregistered")
        hour = 3600 * 10**9
        source_start = plan.start_ns - plan.warmup_bars * hour
        if len(candles) != (plan.end_ns - source_start) // hour:
            raise ValueError("incomplete forward source window")
        for i, candle in enumerate(candles):
            if (
                candle.instrument_id != plan.instrument_id
                or candle.start_ns != source_start + i * hour
                or candle.end_ns != candle.start_ns + hour
            ):
                raise ValueError("forward source identity or chronology mismatch")
        existing = store.db.execute(
            "SELECT dataset_hash FROM forward_bindings WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None and existing[0] != dataset_hash:
            raise ValueError("forward plan already bound to another dataset")
        if existing is None:
            store.db.execute(
                "INSERT INTO forward_bindings VALUES (?,?,?)", (plan_id, dataset_hash, now)
            )
        store.db.execute("COMMIT")
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    return plan
