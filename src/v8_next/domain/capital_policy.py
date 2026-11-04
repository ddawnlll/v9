"""Capital policy config/validation/decision path (Task 4).

Real capital stays unauthorized when no policy is supplied.
Test policies verify accept/reject deterministically.
No live orders, no private clients, no interactive approval.

Sends no orders, opens no private clients, allocates no capital until
an explicit authorized policy is present.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_UNAUTHORIZED_REASON = "NO_POLICY_FILE_OR_UNAUTHORIZED"


class CapitalPolicy(BaseModel):
    """Validated capital authorization gate.

    - max_notional: absolute notional cap per order / per campaign.
    - max_exposure_frac: fraction of equity allowed as exposure (0,1].
    - authorized: explicit human/policy authorization flag. False = safe default.
    Missing file or absent authorization never mints capital permission.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_notional: Decimal = Field(gt=0, description="Absolute notional cap (>0).")
    max_exposure_frac: Decimal = Field(gt=0, le=1, description="Exposure fraction (0,1].")
    authorized: bool = Field(default=False, description="Explicit capital authorization.")
    # Optional risk budget retained for Task 4 spec completeness; non-authorizing by itself.
    risk_budget: Decimal | None = Field(default=None, gt=0)
    # Empty allowlist = no instrument restriction (legacy); non-empty denies others.
    allowed_instruments: tuple[str, ...] = Field(default=())
    # Human provenance for authorized files (who signed); informational only.
    approval_ref: str = Field(default="")

    @field_validator("max_notional", "max_exposure_frac", "risk_budget", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError) as e:
            raise ValueError(f"invalid decimal: {v!r}") from e
        if not d.is_finite():
            raise ValueError("non-finite decimal rejected")
        return d

    @model_validator(mode="after")
    def _check(self) -> "CapitalPolicy":
        # No additional cross-field rule: authorization is independent of sizing.
        # Sizing is still validated (>0, <=1) even when unauthorized.
        return self

    # --- constructors ---

    @classmethod
    def unauthorized(cls) -> "CapitalPolicy":
        """Safe default when no real policy file is configured."""
        return cls(max_notional=Decimal("10000"), max_exposure_frac=Decimal("1.0"), authorized=False)

    @classmethod
    def test_policy(cls, *, max_notional: str | Decimal = "10000", max_exposure_frac: str | Decimal = "1.0", authorized: bool) -> "CapitalPolicy":
        """Deterministic test factory: caller chooses accept/reject explicitly."""
        return cls(max_notional=Decimal(str(max_notional)), max_exposure_frac=Decimal(str(max_exposure_frac)), authorized=authorized)

    @classmethod
    def from_file(cls, path: str | Path | None) -> "CapitalPolicy":
        """Load JSON/YAML policy file; missing/None -> unauthorized safe default.

        File format (JSON):
          {"max_notional": "10000", "max_exposure_frac": "0.1", "authorized": true}

        String-encoded decimals preferred to avoid float drift.
        Never prompts, never blocks.
        """
        if path is None:
            return cls.unauthorized()
        p = Path(path)
        if not p.is_file():
            return cls.unauthorized()
        raw = p.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid capital policy JSON at {p}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"capital policy at {p} must be a JSON object")
        # allow snake/camel alias tolerance only for explicit keys
        mapped: dict[str, Any] = {}
        if "max_notional" in data:
            mapped["max_notional"] = data["max_notional"]
        if "max_exposure_frac" in data:
            mapped["max_exposure_frac"] = data["max_exposure_frac"]
        # legacy alias from PaperConfig compatibility
        if "max_exposure_frac" not in mapped and "max_exposure_fraction" in data:
            mapped["max_exposure_frac"] = data["max_exposure_fraction"]
        if "max_notional" not in mapped or "max_exposure_frac" not in mapped:
            raise ValueError(f"capital policy at {p} missing required fields max_notional/max_exposure_frac")
        mapped["authorized"] = bool(data.get("authorized", False))
        if "risk_budget" in data and data["risk_budget"] is not None:
            mapped["risk_budget"] = data["risk_budget"]
        if "allowed_instruments" in data and data["allowed_instruments"] is not None:
            mapped["allowed_instruments"] = tuple(data["allowed_instruments"])
        if "approval_ref" in data and data["approval_ref"] is not None:
            mapped["approval_ref"] = str(data["approval_ref"])
        return cls(**mapped)

    # --- decision helpers (pure, no IO, no orders) ---

    def decision(
        self,
        requested_notional: Decimal | float | str | None = None,
        *,
        live: bool = False,
        instrument_id: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate a hypothetical notional request against this policy.

        Returns a pure decision dict: no orders emitted, no approval prompt.
        live=True never authorizes in this scope (no live execution path
        exists); ACCEPT covers test/paper simulation only.
        """
        if live:
            return {"decision": "REJECT", "reason": "LIVE_EXECUTION_DISABLED_IN_SCOPE", "authorized": False}
        if not self.authorized:
            return {"decision": "REJECT", "reason": _UNAUTHORIZED_REASON, "authorized": False}
        if instrument_id is not None and self.allowed_instruments and instrument_id not in self.allowed_instruments:
            return {"decision": "REJECT", "reason": "INSTRUMENT_NOT_ALLOWED", "authorized": True}
        if requested_notional is not None:
            try:
                req = Decimal(str(requested_notional))
            except (InvalidOperation, ValueError, TypeError):
                return {"decision": "REJECT", "reason": "INVALID_NOTIONAL", "authorized": True}
            if not req.is_finite() or req <= 0:
                return {"decision": "REJECT", "reason": "INVALID_NOTIONAL", "authorized": True}
            if req > self.max_notional:
                return {"decision": "REJECT", "reason": "EXCEEDS_MAX_NOTIONAL", "authorized": True, "max_notional": str(self.max_notional)}
            # exposure frac check requires equity context; caller supplies equity separately.
            # Here we only gate notional; frac gate is exposure-aware at admission time.
        return {"decision": "ACCEPT", "reason": "AUTHORIZED_WITHIN_LIMITS", "authorized": True}

    def exposure_cap(self, equity: Decimal | float | str) -> Decimal:
        """Maximum exposure notional for given equity under this policy."""
        eq = Decimal(str(equity))
        if not eq.is_finite() or eq < 0:
            raise ValueError("invalid equity")
        return (eq * self.max_exposure_frac).quantize(Decimal("0.00000001"))

    def as_limits(self, *, max_snapshot_age_ns: int = 60_000_000_000) -> dict[str, Any]:
        """Export to risk/admission compatible dict (no live orders)."""
        return {
            "max_order_notional": self.max_notional,
            "max_exposure_fraction": self.max_exposure_frac,
            "max_gross_fraction": self.max_exposure_frac,  # conservative: gross == exposure
            "max_snapshot_age_ns": max_snapshot_age_ns,
            "authorized": self.authorized,
        }

    def to_receipt_fields(self) -> dict[str, Any]:
        return {
            "max_notional": str(self.max_notional),
            "max_exposure_frac": str(self.max_exposure_frac),
            "authorized": self.authorized,
            "risk_budget": str(self.risk_budget) if self.risk_budget is not None else None,
            "allowed_instruments": list(self.allowed_instruments),
            "approval_ref": self.approval_ref,
        }
