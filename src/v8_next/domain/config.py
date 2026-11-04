"""Validated external simulation assumptions; no economic defaults."""

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from v8_next.economics.grammar import POLICIES
from v8_next.economics.observer_policy import validate_observer_policy
from v8_next.economics.protection import PROTECTION_POLICIES
from v8_next.risk.sizing import StopBudget


class PositioningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    funding_max_age_ns: int | None = Field(default=None, gt=0, strict=True)
    open_interest_max_age_ns: int | None = Field(default=None, gt=0, strict=True)

    account_ratio_period: (
        Literal["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"] | None
    ) = None
    account_ratio_max_age_ns: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def ratio_pair(self) -> Self:
        if (self.account_ratio_period is None) != (self.account_ratio_max_age_ns is None):
            raise ValueError("account ratio requires both period and freshness")
        return self


class PaperConfig(PositioningPolicy):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    maker_fee: Decimal = Field(ge=0)
    taker_fee: Decimal = Field(ge=0)
    initial_balance: Decimal = Field(gt=0)
    max_notional: Decimal = Field(gt=0)
    max_exposure_fraction: Decimal = Field(gt=0, le=1)

    stop_budget: StopBudget | None = None

    @model_validator(mode="after")
    def protected_sizing(self) -> Self:
        if self.stop_budget is not None and self.campaign_policy == "timeout-only-v1":
            raise ValueError("stop budget requires a protected campaign policy")
        return self

    observer_policy: str = "squeeze"

    @field_validator("observer_policy")
    @classmethod
    def valid_observer_policy(cls, value: str) -> str:
        return validate_observer_policy(value)

    grammar_policy: str = "range-breakout-48-v1"

    @field_validator("grammar_policy")
    @classmethod
    def valid_grammar(cls, value: str) -> str:
        if value not in POLICIES:
            raise ValueError("unknown opportunity grammar")
        return value

    campaign_policy: str = "timeout-only-v1"

    @field_validator("campaign_policy")
    @classmethod
    def valid_campaign_policy(cls, value: str) -> str:
        if value not in PROTECTION_POLICIES:
            raise ValueError("unknown campaign policy")
        return value

    symbols: tuple[Literal["BTCUSDT", "ETHUSDT"], ...] = ("BTCUSDT",)

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("nonempty unique paper symbols required")
        return value

    experiment_window: tuple[StrictInt, StrictInt] | None = None

    @model_validator(mode="after")
    def valid_experiment(self) -> Self:
        if self.experiment_window is not None:
            start, end = self.experiment_window
            if not 0 < start < end or self.campaign_policy == "timeout-only-v1":
                raise ValueError("ordered experiment window and protected campaign required")
            if self.calibration_source_run is not None:
                raise ValueError("rule experiment cannot claim calibrated source admission")
        return self

    calibration_source_run: str | None = None
