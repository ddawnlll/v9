"""D-153 Canonical Forensic Report Generator (Rule 12, Rule 31 Renderer Firewall).

Generates self-verifying, tamper-evident HTML and JSON forensic reports.
Constitutional Invariant:
    Accepts ONLY verified receipts. If receipt digest does not match its contents,
    or if bound physical artifacts on disk are missing/tampered, rendering fails closed
    or marks the status as BLOCKED / UNVERIFIED.

Render identity (#442): ``receipt_digest`` alone does NOT identify a render, because
the same receipt renders differently under different contracts. Every artifact this
module writes therefore advertises the render contract revision it was produced
under, next to the digest, so a consumer can recompute the identity at HEAD and
reject a superseded render instead of republishing its numbers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from v8_next.evaluation.benchmark_receipt import BenchmarkReceipt, GateEvaluation, GateState
from v8_next.evaluation.certificate import PolicyCertificate

#: The render contract this module implements. Bump the revision whenever the
#: rendered semantics change, so renders of the same receipt are distinguishable.
#:   v1 — pre-NX08.R2: a missing robustness/economic factor rendered as the
#:        fabricated defaults (robustness 50.0, economic 60.0).
#:   v2 — missing-aware: an absent factor renders MISSING and the readiness index
#:        is MISSING rather than a number built on retired defaults (#429, #442).
RENDER_CONTRACT_ID = "d153-forensic-html"
RENDER_CONTRACT_REVISION = 2

#: Stands in when the certificate reports no contract of its own. Never a guess at
#: a version: an unknown contract identity must not look like a known one.
CERTIFICATE_CONTRACT_UNSPECIFIED = "UNSPECIFIED"


def render_identity(
    receipt: BenchmarkReceipt,
    certificate: PolicyCertificate | None = None,
) -> dict[str, Any]:
    """Render contract/revision identity for ``receipt``, beside its digest (#442).

    ``render_identity_digest`` is recomputable: it hashes the render contract
    revision, the certificate contract the readiness was derived under, and the
    measurements this contract renders. Two renders of the same ``receipt_digest``
    under different contracts therefore carry different identities, and the
    superseded one is rejectable by name.
    """
    cert = certificate if certificate is not None else PolicyCertificate.generate(receipt)
    derivation = cert.derivation or {}
    certificate_contract = str(
        derivation.get("transform_version", CERTIFICATE_CONTRACT_UNSPECIFIED)
    )
    payload = {
        "render_contract": RENDER_CONTRACT_ID,
        "render_revision": RENDER_CONTRACT_REVISION,
        "certificate_contract": certificate_contract,
        "receipt_digest": receipt.receipt_digest,
        "receipt_digest_version": receipt.digest_version,
        "readiness_index": cert.readiness_index,
        "research_capability_score": cert.research_capability_score,
        "evidence_multiplier": cert.evidence_multiplier,
        "minerva_robustness_score": cert.minerva_robustness_score,
        "economic_score": cert.economic_score,
        "robustness_seal_status": cert.robustness_seal_status,
        "missing_measurements": sorted(cert.missing_measurements),
    }
    identity_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "render_contract": f"{RENDER_CONTRACT_ID}/v{RENDER_CONTRACT_REVISION}",
        "render_revision": RENDER_CONTRACT_REVISION,
        "certificate_contract": certificate_contract,
        "receipt_digest": receipt.receipt_digest,
        "receipt_digest_version": receipt.digest_version,
        "render_identity_digest": identity_digest,
    }


def render_identity_is_current(
    declared: dict[str, Any],
    receipt: BenchmarkReceipt,
    certificate: PolicyCertificate | None = None,
) -> bool:
    """True when ``declared`` is the identity of the current render of ``receipt``.

    A consumer holding a published artifact compares the identity that artifact
    advertises against this predicate; ``False`` means the artifact is a
    superseded render whose readiness must not be republished.
    """
    current = render_identity(receipt, certificate)
    return bool(declared.get("render_identity_digest") == current["render_identity_digest"])


def _v(value: float | None, digits: int = 1) -> str:
    """Render a certificate measurement; a missing one says MISSING, not 0."""
    if value is None:
        return "MISSING"
    return f"{value:.{digits}f}"


def _component_badge(value: float | None, measured: str, absent: str = "NOT_MEASURED") -> str:
    """Status badge for one readiness component row.

    An absent component declares the absence; it never reads as an evaluated value
    or as a default that was applied in place of one (#442).
    """
    if value is None:
        return f'<span class="badge badge-miss">{absent}</span>'
    return f'<span class="badge badge-pass">{measured}</span>'


def _gate_decision(ev: GateEvaluation) -> str:
    """Rendered decision for one gate row (#435).

    The descriptor's ``requirement`` is what decides whether a state is admitted,
    so the report shows the decision explicitly: an established PASS, a
    NOT_APPLICABLE that the gate's own clause admits, or the named reason the gate
    is not established (e.g. ``REQUIRED_BLOCKING_GATE_UNEVALUATED``).
    """
    if ev.holds():
        if ev.state == GateState.PASS:
            return "ESTABLISHED"
        return "ADMITTED_NOT_APPLICABLE"
    return ev.refusal_reason() or ""


def generate_forensic_html_report(
    receipt: BenchmarkReceipt,
    output_path: Path | str,
    certificate: PolicyCertificate | None = None,
) -> bool:
    """Render self-contained forensic HTML report adhering to Rule 31 Renderer Firewall."""
    # 1. Self-verification check (Renderer Firewall)
    is_valid, verify_msg = receipt.verify()
    # #476: a caller holding the run's certificate must hand it in; regenerating
    # here from the receipt alone would discard the projection/Minerva inputs.
    cert = certificate if certificate is not None else PolicyCertificate.generate(receipt)
    # The identity this render advertises, recomputable at HEAD from the receipt
    # and the certificate contract (#442).
    identity = render_identity(receipt, cert)

    is_approved = "Ready For Review" in cert.status
    verdict_badge = "badge-pass" if is_approved else "badge-fail"
    verdict_text = cert.status if is_valid else f"BLOCKED ({verify_msg})"
    derivation = cert.derivation or {}
    projection_refusal = str(derivation.get("projection_refusal_reason") or "")
    minerva_refusal = str(derivation.get("minerva_refusal_reason") or "")
    economic_note = (
        "No economic projection in this diagnostic cell: absent under the current "
        "certificate contract (never a diagnostic default)"
        if cert.economic_score is None
        else "Diagnostic cell projection (no forward claims)"
    )
    if projection_refusal:
        economic_note = f"Named projection refusal: {projection_refusal}"
    minerva_note = "arXiv:2608.23808 prudex evaluation"
    if minerva_refusal:
        minerva_note = f"Named Minerva refusal: {minerva_refusal}"

    # 2. HTML template (matching D-153 Forensic standard)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>V8.5 Benchmark Fabric — Forensic Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }}
.container {{ max-width: 1040px; margin: 0 auto; background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 28px; }}
h1, h2 {{ color: #f8fafc; border-bottom: 1px solid #334155; padding-bottom: 8px; }}
.callout {{ background: #0f172a; border-left: 4px solid #38bdf8; padding: 12px 16px; margin: 16px 0; font-size: 13px; }}
.callout-warn {{ border-left-color: #f59e0b; }}
.badge {{ display: inline-block; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 13px; }}
.badge-pass {{ background: #14532d; color: #4ade80; border: 1px solid #22c55e; }}
.badge-fail {{ background: #7f1d1d; color: #f87171; border: 1px solid #ef4444; }}
.badge-warn {{ background: #78350f; color: #fbbf24; border: 1px solid #d97706; }}
.badge-miss {{ background: #374151; color: #9ca3af; border: 1px solid #4b5563; }}
.hero {{ display: flex; justify-content: space-between; align-items: center; background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 20px; margin: 20px 0; }}
.hero-score {{ font-size: 42px; font-weight: bold; color: #38bdf8; }}
table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }}
th, td {{ border: 1px solid #334155; padding: 10px; text-align: left; }}
th {{ background: #0f172a; color: #94a3b8; }}
tr:nth-child(even) {{ background: #1e293b; }}
code {{ font-family: ui-monospace, monospace; background: #0f172a; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<div class="container">
<h1>V8.5 Benchmark Fabric — Evidence Dashboard & Policy Certificate</h1>

<div class="callout callout-warn">
<strong>CONSTITUTIONAL NOTICE (Rule 12 & Rule 57):</strong> Benchmark Fabric is an evidence-bound diagnostic instrument. CapabilityScore does NOT confer deployment readiness or mint economic edge (<code>SUPPORTED_EDGE</code>). All outputs carry <code>NO_ECONOMIC_CLAIM</code>.
</div>

<div class="callout">
<strong>SELF-VERIFICATION (#328):</strong> Rendered from a receipt whose digest was <em>recomputed from its own contents</em> at render time.<br>
<code>verified_digest = {receipt.receipt_digest}</code><br>
<code>render_contract = {identity["render_contract"]}</code><br>
<code>render_identity = {identity["render_identity_digest"]}</code><br>
<code>certificate_contract = {identity["certificate_contract"]}</code><br>
<code>integrity_status = {verify_msg}</code> | <code>digest_version = {receipt.digest_version}</code> | <code>artifact_bindings = {len(receipt.artifact_bindings)}</code>
</div>

<div class="callout callout-warn">
<strong>RENDER IDENTITY (#442):</strong> A render is bound to the contract revision that produced it; <code>verified_digest</code> alone does not identify a render.<br>
Recompute <code>render_identity(receipt)</code> at HEAD for this <code>verified_digest</code>: if its <code>render_identity</code> differs from the value above, this artifact is a <em>superseded render</em> and the readiness it shows must not be republished.
</div>

<div class="hero">
  <div>
    <div style="color: #94a3b8; font-size: 13px; text-transform: uppercase;">Policy Verdict & Status</div>
    <div style="margin: 8px 0;"><span class="badge {verdict_badge}">{verdict_text}</span></div>
    <div style="font-size: 13px;">Target Policy: <strong>{receipt.policy_id}</strong> | Case: <code>{receipt.case_id}</code></div>
    <div style="font-size: 12px; color: #94a3b8; margin-top: 4px;">{cert.authority_verdict}</div>
  </div>
  <div style="text-align: right;">
    <div style="color: #94a3b8; font-size: 13px; text-transform: uppercase;">Readiness Index</div>
    <div class="hero-score">{_v(cert.readiness_index)} <span style="font-size: 16px; color: #94a3b8;">/ 100</span></div>
    <div style="font-size: 12px; color: #94a3b8;">Capability: {_v(cert.research_capability_score)} | Robustness: {_v(cert.minerva_robustness_score)}</div>
  </div>
</div>

<h2>Hard-Gate Verification Matrix (G0-G9 Non-Compensable)</h2>
<table>
<thead><tr><th>Gate ID</th><th>Canonical Gate Descriptor</th><th>Requirement</th><th>State</th><th>Decision</th></tr></thead>
<tbody>
"""
    for ev in receipt.gates.evaluated_gates():
        st = ev.state.value
        badge_cls = (
            "badge-pass"
            if st == "PASS"
            else ("badge-warn" if st == "UNKNOWN" else ("badge-miss" if st == "MISSING" else "badge-fail"))
        )
        gate_name = f"{ev.descriptor.canonical_id}::{ev.descriptor.vector_field}"
        html_content += f"""  <tr>
    <td><code>G{ev.descriptor.index}</code></td>
    <td><strong>{gate_name}</strong><br><small style="color: #94a3b8;">{ev.descriptor.source_clause}</small></td>
    <td><code>{ev.descriptor.requirement}</code></td>
    <td><span class="badge {badge_cls}">{st}</span></td>
    <td><code>{_gate_decision(ev)}</code></td>
  </tr>
"""

    html_content += f"""</tbody>
</table>

<h2>D-153 Multiplicative Readiness Components</h2>
<table>
<thead><tr><th>Component</th><th>Score / Value</th><th>Status</th><th>Notes</th></tr></thead>
<tbody>
  <tr>
    <td>Research Capability Score</td>
    <td><strong>{_v(cert.research_capability_score)} / 100</strong></td>
    <td>{_component_badge(cert.research_capability_score, "EVALUATED")}</td>
    <td>Harmonic mean with uncertainty penalty (D-153 §76)</td>
  </tr>
  <tr>
    <td>Evidence Multiplier</td>
    <td><strong>{_v(cert.evidence_multiplier, 2)}</strong></td>
    <td>{_component_badge(cert.evidence_multiplier, "PENALIZED")}</td>
    <td>Single diagnostic cell coverage penalty</td>
  </tr>
  <tr>
    <td>Minerva Robustness Score</td>
    <td><strong>{_v(cert.minerva_robustness_score)} / 100</strong></td>
    <td>{_component_badge(cert.minerva_robustness_score, cert.robustness_seal_status, cert.robustness_seal_status)}</td>
    <td>{minerva_note}</td>
  </tr>
  <tr>
    <td>Economic Projection Score</td>
    <td><strong>{_v(cert.economic_score)} / 100</strong></td>
    <td>{_component_badge(cert.economic_score, "DIAGNOSTIC_CELL", "PROJECTION_REFUSED")}</td>
    <td>{economic_note}</td>
  </tr>
</tbody>
</table>

<h2>Cryptographically Bound Physical Artifacts</h2>
<table>
<thead><tr><th>Role</th><th>Path</th><th>SHA-256 Digest</th><th>Bytes</th></tr></thead>
<tbody>
"""
    for b in receipt.artifact_bindings:
        html_content += f"""  <tr>
    <td><code>{b.role}</code></td>
    <td><code>{b.path}</code></td>
    <td><code>{b.sha256_hex[:16]}...{b.sha256_hex[-8:]}</code></td>
    <td>{b.bytes}</td>
  </tr>
"""
    html_content += """</tbody>
</table>

<h2>Economic Evidence Attachment</h2>
<table>
<thead><tr><th>Field</th><th>Value</th></tr></thead>
<tbody>
"""
    econ_digest = receipt.economic_evidence_digest or "(none attached)"
    econ_path = receipt.economic_receipt_path or "(none)"
    html_content += f"""  <tr>
    <td><code>economic_evidence_digest</code></td>
    <td><code>{econ_digest[:32]}...</code></td>
  </tr>
  <tr>
    <td><code>economic_receipt_path</code></td>
    <td><code>{econ_path}</code></td>
  </tr>
"""
    html_content += """</tbody>
</table>

<div style="margin-top: 32px; font-size: 11px; color: #64748b; border-top: 1px solid #334155; padding-top: 12px;">
V8.5 Benchmark Fabric Forensic Ledger & Certificate Firewall - Built in accordance with D-153 & D-152.
</div>
</div>
</body>
</html>"""

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html_content, encoding="utf-8")
    return True
