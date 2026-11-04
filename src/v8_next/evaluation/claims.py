"""Central Claim Registry & Statutory Claim Records (Rule 12, Rule 30-31, D-132, D-153 §3).

Enforces:
1. Closed algebra of statutory claim classes.
2. Cryptographic ledger chain verification prior to issuance.
3. Content-addressed StatutoryClaimRecord minting without scalar collapse.
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    GateState,
    GateVector,
)


class StatutoryClaimClass(StrEnum):
    """Closed Algebra of Statutory Claim Classes (Rule 30)."""

    DiagnosticSignal = "DIAGNOSTIC_SIGNAL"
    CounterfactualPotential = "COUNTERFACTUAL_POTENTIAL"
    RecoverableRegret = "RECOVERABLE_REGRET"
    SimulatedCashflow = "SIMULATED_CASHFLOW"
    RealizedCashflow = "REALIZED_CASHFLOW"
    ReadyNotClaimed = "READY_NOT_CLAIMED"
    SupportedEdge = "SUPPORTED_EDGE"

    def canonical_header(self) -> str:
        """Canonical legally authorized rendering header (Rule 31)."""
        headers = {
            self.DiagnosticSignal: "Diagnostic Signal (Zero Economic Authority)",
            self.CounterfactualPotential: "Counterfactual Ex-Post Potential (Diagnostic Upper Bound)",
            self.RecoverableRegret: "Recoverable Regret (Friction & Overlap Adjusted)",
            self.SimulatedCashflow: "Simulated Cashflow Delta (ExecutionBackend Physics)",
            self.RealizedCashflow: "Realized Cashflow Settlement (Physical Venue Fills)",
            self.ReadyNotClaimed: "Research Candidate Ready For Review (NOT Production Approved)",
            self.SupportedEdge: "Statistically Certified Edge (Multiple-Testing Adjusted)",
        }
        return headers.get(self, "Unknown Claim Class")


class StatutoryClaimRecord(BaseModel):
    """Immutable statutory claim record registered in the central ClaimRegistry."""

    model_config = ConfigDict(frozen=True)

    claim_id: str
    claim_class: StatutoryClaimClass
    numeric_value: float
    units: str
    parent_receipt_hashes: tuple[str, ...]
    timestamp_utc: int
    signature: str
    allowed_rendering_header: str

    @classmethod
    def create(
        cls,
        claim_class: StatutoryClaimClass,
        numeric_value: float,
        units: str,
        parent_receipt_hashes: Sequence[str],
        signing_key: str = "v8.5-claim-authority",
    ) -> StatutoryClaimRecord:
        now = int(time.time())
        parents = tuple(parent_receipt_hashes)
        canon_str = (
            f"StatutoryClaimRecord|{claim_class.value}|{numeric_value:.6f}|"
            f"{units}|{','.join(parents)}|{now}|{signing_key}"
        )
        claim_id = hashlib.sha256(canon_str.encode("utf-8")).hexdigest()
        signature = hashlib.sha256(f"SIG:{claim_id}:{signing_key}".encode("utf-8")).hexdigest()
        return cls(
            claim_id=claim_id,
            claim_class=claim_class,
            numeric_value=numeric_value,
            units=units,
            parent_receipt_hashes=parents,
            timestamp_utc=now,
            signature=signature,
            allowed_rendering_header=claim_class.canonical_header(),
        )

    def verify_signature(self, signing_key: str = "v8.5-claim-authority") -> bool:
        expected = hashlib.sha256(f"SIG:{self.claim_id}:{signing_key}".encode("utf-8")).hexdigest()
        return self.signature == expected


class ClaimRegistry:
    """Central registry validating ledger hash chains and issuing statutory claim records."""

    def __init__(self, registry_dir: Path | str = "artifacts/benchmarks") -> None:
        self.registry_dir = Path(registry_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.registry_dir / "claims_registry.jsonl"
        self._claims: list[StatutoryClaimRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.registry_file.exists():
            return
        with open(self.registry_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    self._claims.append(StatutoryClaimRecord(**data))

    def verify_ledger_and_issue_claim(
        self,
        ledger: BenchmarkLedger,
        receipt_digest: str,
        gates: GateVector,
        capability_score: float | None,
        live_realization_verified: bool = False,
    ) -> tuple[bool, str, StatutoryClaimRecord | None]:
        """Verify benchmark ledger chain, gate conditions, and issue statutory claim record.

        Non-Scalar Collapse Invariant:
        A claim cannot be issued solely on a scalar score; all required gates G0-G7
        must be *established* (see ``GateEvaluation.holds``), and ledger chain must be
        cryptographically intact. An unevaluated gate (``NOT_APPLICABLE``) satisfies
        the precondition only where its own descriptor admits that state — never on a
        ``RequiredBlocking`` gate, which must be established before anything is minted
        (#435).
        """
        # 1. Verify ledger hash chain
        chain_valid, chain_msg = ledger.verify_chain()
        if not chain_valid:
            return False, f"Ledger chain verification failed: {chain_msg}", None

        # 2. Verify target receipt exists in ledger
        receipt_entry = next((e for e in ledger.entries if e.receipt.receipt_digest == receipt_digest), None)
        if receipt_entry is None:
            return False, f"Receipt digest {receipt_digest} not found in ledger", None

        # 3. Verify gate conditions: G0-G7 must be established. A gate that was
        #    never evaluated (NOT_APPLICABLE) is refused here with a named reason
        #    naming the gate and its state unless its own descriptor admits that
        #    state (#435).
        evals = gates.evaluated_gates()
        for idx in range(8):  # G0 through G7
            ev = evals[idx]
            if not ev.holds():
                return False, f"Gate G{idx} not established: {ev.refusal_reason()}", None

        # 4. G8 check — same rule, decided by the descriptor's requirement
        g8_ev = evals[8]
        if not g8_ev.holds():
            return False, f"Gate G8 not established: {g8_ev.refusal_reason()}", None

        # 5. Determine claim class
        if live_realization_verified and g8_ev.state == GateState.PASS:
            claim_class = StatutoryClaimClass.SupportedEdge
        else:
            claim_class = StatutoryClaimClass.ReadyNotClaimed

        # 6. Mint StatutoryClaimRecord. A claim is a measured statement: without a
        # measured capability score there is nothing to mint (NX08.R6 keeps the
        # runtime economic-claim authority check intact).
        if capability_score is None:
            return False, "NO_MEASURED_CAPABILITY_SCORE", None
        parent_hashes = [receipt_digest, receipt_entry.entry_hash]
        claim_record = StatutoryClaimRecord.create(
            claim_class=claim_class,
            numeric_value=capability_score,
            units="CapabilityScore[0-100]",
            parent_receipt_hashes=parent_hashes,
        )

        # 7. Append to append-only registry file
        self._claims.append(claim_record)
        with open(self.registry_file, "a", encoding="utf-8") as f:
            f.write(claim_record.model_dump_json() + "\n")

        return True, "Statutory claim record verified and minted successfully", claim_record
