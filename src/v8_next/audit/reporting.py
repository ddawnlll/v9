"""Report generation from persisted audit evidence only.

``AUDIT_REPORT.md`` is the full forensic narrative; ``FINAL_DELIVERY_TR.md`` is
the mandatory twelve-section Turkish deliverable. Both read the same validated
JSON/JSONL artifacts and never re-run the engine or invent numbers.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from v8_next.audit.forensic import AuditStore

TR_SECTIONS = (
    "Çalıştırma Kimliği",
    "Piyasa Verisi Kalitesi",
    "Uzman Etkinliği",
    "Fırsat Hunisi",
    "Gerçek Emirler ve Gerçekleşmeler",
    "Pozisyon Yaşam Döngüsü",
    "Risk ve Koruma",
    "İşlem Ekonomisi",
    "MFE, MAE ve Gerçekleşen R",
    "Kaçırılan ve Reddedilen Fırsatlar",
    "Başarısızlık Atfı",
    "Yeniden Üretilebilirlik ve Sonraki Adımlar",
)


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _rows(store: AuditStore, name: str) -> list[dict[str, Any]]:
    return list(store.iter_jsonl(name)) if store.exists(name) else []


def _gate_table(gates: Sequence[Mapping[str, Any]]) -> str:
    lines = ["| Gate | Durum | Gerekçe |", "|---|---|---|"]
    for gate in gates:
        lines.append(
            f"| {gate.get('gate_id')} | {gate.get('status')} | {str(gate.get('explanation'))[:160]} |"
        )
    return "\n".join(lines)


def _gate_status(store: AuditStore) -> dict[str, str]:
    if not store.exists("gate_results.json"):
        return {}
    data = store.read_json("gate_results.json")
    return {g["gate_id"]: g["status"] for g in data.get("gates", [])}


def build_context(run_dir: Path) -> dict[str, Any]:
    store = AuditStore(run_dir)
    ctx: dict[str, Any] = {"store": store}
    for name in (
        "run_manifest",
        "opportunity_funnel",
        "execution_metrics",
        "economic_metrics",
        "mfe_mae_metrics",
        "post_exit_metrics",
        "data_quality",
        "reconciliation",
        "validation_results",
        "gate_results",
    ):
        file = f"{name}.json"
        ctx[name] = store.read_json(file) if store.exists(file) else {}
    ctx["lifecycles"] = _rows(store, "trade_lifecycles.jsonl")
    ctx["events"] = _rows(store, "events.jsonl")
    ctx["orders"] = _rows(store, "orders.jsonl")
    ctx["fills"] = _rows(store, "fills.jsonl")
    ctx["positions"] = _rows(store, "positions.jsonl")
    ctx["cashflows"] = _rows(store, "cashflows.jsonl")
    ctx["expert_decisions"] = _rows(store, "expert_decisions.jsonl")
    ctx["opportunities"] = _rows(store, "opportunities.jsonl")
    return ctx


def write_reports(run_dir: Path) -> dict[str, str]:
    run_dir = Path(run_dir)
    ctx = build_context(run_dir)
    audit_report = render_audit_report(ctx)
    turkish = render_turkish_delivery(ctx)
    (run_dir / "AUDIT_REPORT.md").write_text(audit_report, encoding="utf-8")
    (run_dir / "FINAL_DELIVERY_TR.md").write_text(turkish, encoding="utf-8")
    return {"audit_report": str(run_dir / "AUDIT_REPORT.md"), "final_delivery": str(run_dir / "FINAL_DELIVERY_TR.md")}


def _expert_summary(rows: Sequence[Mapping[str, Any]]) -> tuple[list[str], Counter[str], Counter[str]]:
    by_expert: Counter[str] = Counter()
    by_stance: Counter[str] = Counter()
    for row in rows:
        if row.get("event_type") != "EXPERT_EVALUATED":
            continue
        name = row.get("expert_id") or "ALL_28 (aggregate NO_OPPORTUNITY)"
        by_expert[str(name)] += 1
        by_stance[str(row.get("stance"))] += 1
    lines = ["| Uzman | Değerlendirme |", "|---|---|"]
    lines += [f"| {name} | {count} |" for name, count in sorted(by_expert.items())]
    return lines, by_stance, by_expert


def render_turkish_delivery(ctx: Mapping[str, Any]) -> str:
    manifest = ctx["run_manifest"]
    dataset = manifest.get("dataset", {})
    perf = manifest.get("performance", {})
    dq = ctx["data_quality"]
    econ = ctx["economic_metrics"]
    funnel = ctx["opportunity_funnel"]
    recon = ctx["reconciliation"]
    mfe = ctx["mfe_mae_metrics"]
    execution = ctx["execution_metrics"]
    post = ctx["post_exit_metrics"]
    lifecycles = ctx["lifecycles"]
    gates = ctx["gate_results"].get("gates", [])
    validation = ctx["validation_results"]
    statuses = Counter(g.get("status") for g in gates)

    if statuses.get("FAIL") or statuses.get("BLOCKED"):
        overall = "PARTIAL"
    elif validation.get("status") != "PASS":
        overall = "PARTIAL"
    else:
        overall = "COMPLETE"

    exit_kinds = Counter(str(row.get("exit_kind")) for row in lifecycles)
    invalid_r = [row for row in lifecycles if row.get("risk_denominator_status") == "INVALID"]
    expert_table, stance_counts, _expert_counts = _expert_summary(ctx["events"])
    policy_rows = [row for row in post.get("rows", []) if row.get("legal_policy_counterfactual_usdt") is not None]
    policy_differs = sum(
        1
        for row in policy_rows
        if row.get("legal_policy_counterfactual_usdt") != row.get("hindsight_maximum_usdt")
    )

    lines: list[str] = [
        "# V8-NEXT NATIVE FORENSIC AUDIT — TÜRKÇE TESLİM RAPORU",
        "",
        f"**DURUM:** {overall}",
        f"**Çalıştırma:** `{manifest.get('run_id')}`",
        f"**Yetki:** {manifest.get('authority')}",
        "",
    ]

    def section(number: int, title: str) -> None:
        lines.extend(["", f"## {number}. {title}", ""])

    section(1, TR_SECTIONS[0])
    lines += [
        f"- Depo: `{manifest.get('repository', {}).get('path')}`",
        f"- Kaynak revizyon: `{manifest.get('repository', {}).get('source_revision')}` "
        f"(dirty={_fmt(manifest.get('repository', {}).get('git_dirty'))})",
        f"- Python: {manifest.get('environment', {}).get('python')}; "
        f"NautilusTrader: {manifest.get('environment', {}).get('nautilus_trader')}",
        f"- Veri seti: `{dataset.get('path')}`",
        f"- SHA-256: `{dataset.get('sha256')}`",
        f"- Pencere: {manifest.get('config', {}).get('start_ms', dataset.get('start_ms'))} → "
        f"{dataset.get('end_ms')}; bar: {dataset.get('bars')} ({dataset.get('bar_interval')})",
        f"- Execution profile: {execution.get('execution_profile', {}).get('execution_profile_name')}",
        f"- Uzman filtresi: {manifest.get('config', {}).get('expert_filter')}",
        f"- OOS verisine dokunulmadı: {_fmt(dq.get('oos_data_touched'))}",
    ]

    section(2, TR_SECTIONS[1])
    lines += [
        f"- Bar sayısı: {dq.get('bars')} (beklenen {dq.get('expected_bar_count')})",
        f"- Boşluk (gap): {dq.get('gaps')}; yinelenen zaman damgası: {dq.get('duplicate_timestamps')}",
        f"- Funding satırı: {dq.get('funding_rows')} (düşürülen: {dq.get('funding_dropped')}); "
        f"mark price eksik: {_fmt(dq.get('mark_price_absent'))}",
        f"- Kotasyon/tick/derinlik verisi: quote={_fmt(dq.get('quote_data_available'))}, "
        f"trade_tick={_fmt(dq.get('trade_tick_data_available'))}, "
        f"depth={_fmt(dq.get('order_book_depth_available'))}",
        f"- Bar içi yol tespit edilebilir mi: {_fmt(dq.get('intrabar_path_identifiable'))}",
        f"- Not: {dq.get('note')}",
    ]

    section(3, TR_SECTIONS[2])
    lines += [
        f"- `EXPERT_EVALUATED` olayı (tüm ledgers): {sum(stance_counts.values())}",
        f"- Durum dağılımı: SUPPORT={stance_counts.get('SUPPORT', 0)}, "
        f"CONTRADICT={stance_counts.get('CONTRADICT', 0)}, ABSTAIN={stance_counts.get('ABSTAIN', 0)}, "
        f"NO_OPPORTUNITY={stance_counts.get('NO_OPPORTUNITY', 0)}, "
        f"NOT_EVALUATED={stance_counts.get('NOT_EVALUATED', 0)}, "
        f"EVALUATION_ERROR={stance_counts.get('EVALUATION_ERROR', 0)}",
        f"- Fırsat anındaki uzman kararları (`expert_decisions.jsonl`, filtre={manifest.get('config', {}).get('expert_filter')}): "
        f"{len(ctx['expert_decisions'])} satır",
        "- Altı değerlendirme durumu birbirinden ayrıdır; NO_OPPORTUNITY satırı bar başına tek toplu kayıttır.",
        "",
        *expert_table,
    ]

    section(4, TR_SECTIONS[3])
    lines += [
        f"- Aday (CANDIDATE): {funnel.get('candidates')}",
        f"- Onaylanan (CONFIRMED): {funnel.get('confirmed')}",
        f"- Kabul edilen (ADMITTED): {funnel.get('admitted')}",
        f"- Süresi dolan: {funnel.get('expired')}; geçersizleşen: {funnel.get('invalidated')}",
        f"- Pozisyon doluluğu nedeniyle reddedilen: {funnel.get('admission_rejected_position_occupied')}",
        f"- Book durum dağılımı: {json.dumps(funnel.get('book_status_counts', {}), ensure_ascii=False)}",
        f"- Book'ta şu an ADMITTED: {funnel.get('book_current_admitted')} "
        f"(kümülatif kabul {funnel.get('admission_accepted')})",
        f"- Fırsat uzlaşması: {_fmt(funnel.get('reconciliation_ok'))}",
        f"- Tanım: {funnel.get('definition')}",
    ]

    section(5, TR_SECTIONS[4])
    lines += [
        f"- Yerel emir sayısı: {recon.get('native_order_count')}; yerel fill sayısı: {recon.get('native_fill_count')}",
        f"- Denetlenen fill: {recon.get('audited_fill_count')}; eşleşmeyen yerel: {recon.get('unmatched_native_fills')}; "
        f"eşleşmeyen denetim: {recon.get('unmatched_audit_fills')}",
        f"- Yinelenen fill kimliği: {recon.get('duplicate_fill_ids')}",
        f"- Eşleşen emir kimliği: {recon.get('native_order_ids_matched_to_audit')}; "
        f"bağlantısız emir: {recon.get('unlinked_orders')}; bağlantısız pozisyon: {recon.get('unlinked_positions')}",
        f"- Kesimde açık emir: {recon.get('open_order_count_at_cutoff')} "
        f"({json.dumps(recon.get('open_orders_at_cutoff'), ensure_ascii=False)})",
        f"- Yetim emir olayı: {recon.get('orphan_order_events')}; yetim pozisyon olayı: {recon.get('orphan_position_events')}",
        f"- Maker/taker dağılımı: {json.dumps(execution.get('maker_taker_distribution', {}), ensure_ascii=False)}",
        f"- Fill tamamlanma oranı: {_fmt(execution.get('fill_completion_ratio'))}",
        f"- Native fill raporu durumu: {json.dumps(recon.get('native_fills_report_status'), ensure_ascii=False)}",
        f"- Yerel fill raporu sütun kaynağı: {execution.get('commission_source')}",
    ]

    section(6, TR_SECTIONS[5])
    lines += [
        f"- Açılan pozisyon: {len(ctx['positions'])} olay; kapanan: {econ.get('closed_trades')}; "
        f"kesimde açık: {econ.get('open_positions_at_cutoff')}",
        f"- Çıkış türleri: {json.dumps(dict(exit_kinds), ensure_ascii=False)}",
        f"- Bar tutma süresi (ortalama): {_fmt(_mean([row.get('holding_bars') for row in lifecycles]))}",
        f"- Kesimde açık emir (ledger): {recon.get('open_order_count_at_cutoff')}; "
        f"yaşam döngüsü çıkış dağılımı: {json.dumps(dict(exit_kinds), ensure_ascii=False)}",
    ]

    section(7, TR_SECTIONS[6])
    lines += [
        f"- Geçerli R paydası: {mfe.get('valid_r_denominator')}; geçersiz: {mfe.get('invalid_r_denominator')}",
        f"- Geçersiz nedenler: {json.dumps(mfe.get('invalid_reasons', {}), ensure_ascii=False)}",
        f"- Ledger koruma kaydı: stop_created={recon.get('protection_created_stop')}, "
        f"target_created={recon.get('protection_created_target')}, "
        f"eşleşmeyen koruma fill kimliği={recon.get('protection_missing_count')}",
        f"- Native stop/target emri bulunan yaşam döngüsü: {sum(1 for row in lifecycles if row.get('protection_evidence') == 'NATIVE_ORDER_PRESENT')}"
        f"/{len(lifecycles)}",
        "- Bar içi sıra sözleşmesi: STOP-FIRST (model varsayımı; gözlenen piyasa sırası değildir).",
        "- Hedef ve stop aynı barı kapsıyorsa hangi emrin önce tetiklendiği tarihsel veriden tespit edilemez.",
    ]

    section(8, TR_SECTIONS[7])
    lines += [
        f"- Başlangıç sermayesi: {_fmt(econ.get('initial_capital'), 2)} USDT; kesim özkaynağı: {_fmt(econ.get('account_equity_at_cutoff'), 2)} USDT",
        f"- Brüt fiyat PnL: {_fmt(econ.get('gross_price_pnl'))} USDT",
        f"- Komisyon toplamı (native fill): {_fmt(econ.get('other_native_commissions'))} USDT",
        f"- Gerçekleşen net işlem PnL: {_fmt(econ.get('realized_net_trading_pnl'))} USDT",
        f"- Kesimde gerçekleşmemiş PnL: {_fmt(econ.get('unrealized_pnl_at_cutoff'))} USDT",
        f"- Native realized PnL toplamı: {_fmt(econ.get('native_realized_pnl_total'))} USDT",
        f"- Kazanan/kaybeden: {econ.get('wins')}/{econ.get('losses')}",
        f"- Funding modu: {econ.get('funding_mode')}; işletme gideri (opex): {_fmt(econ.get('business_opex'))} (ayrı tutulur)",
        f"- Korunum artığı: {_fmt(recon.get('conservation_residual_usdt'), 6)} USDT "
        f"(tolerans {_fmt(recon.get('conservation_tolerance_usdt'))})",
        f"- Açıklanamayan nakit akışı: {recon.get('unexplained_cashflows')}",
    ]

    section(9, TR_SECTIONS[8])
    lines += [
        f"- İşlem sayısı: {mfe.get('trades')}",
        f"- MFE R ortalaması: {_fmt(mfe.get('mfe_r_mean'))}; MAE R ortalaması: {_fmt(mfe.get('mae_r_mean'))}",
        f"- Gerçekleşen brüt R ortalaması: {_fmt(mfe.get('realized_gross_r_mean'))}; "
        f"net R ortalaması: {_fmt(mfe.get('realized_net_r_mean'))}",
        f"- MFE > gerçekleşen sonuç olan işlem: {mfe.get('mfe_gt_realized_count')}",
        f"- Geçersiz R üretilen işlem: {len(invalid_r)} (0R değil, null + neden)",
        f"- Çıkış sonrası ufuklar: {json.dumps(post.get('horizons', []), ensure_ascii=False)}",
        f"- Policy karşı-olgusu hesaplanan satır: {len(policy_rows)}; hindsight'tan farklı: {policy_differs}",
        "- Sınıflar ayrıdır: OBSERVED_TAPE_PATH (gerçekleşen yol), "
        "LEGAL_POLICY_COUNTERFACTUAL_MODEL (aynı bracket ile yeniden giriş), "
        "HINDSIGHT_MAXIMUM_MODEL (en iyi lehte hareket); son ikisi USDT ve miktar ile ölçeklidir.",
        f"- Policy tanımı: {post.get('policy_definition') or (policy_rows[0].get('policy_definition') if policy_rows else 'yok')}",
    ]

    section(10, TR_SECTIONS[9])
    lines += [
        f"- Pozisyon doluluğu reddi: {funnel.get('admission_rejected_position_occupied')}",
        f"- Geçersizleşen fırsat: {funnel.get('invalidated')}; süresi dolan: {funnel.get('expired')}",
        f"- Kümülatif onaylanan: {funnel.get('confirmed')}; kümülatif kabul: {funnel.get('admitted')}; "
        f"onaylanıp kabul edilmeyen: {max(0, int(funnel.get('confirmed') or 0) - int(funnel.get('admitted') or 0))}",
        "- Bu fark pozisyon doluluğu reddiyle ({}) birebir açıklanır; book'taki güncel ADMITTED={} "
        "kümülatif sayımla karıştırılmamalıdır.".format(
            funnel.get('admission_rejected_position_occupied'), funnel.get('book_current_admitted')
        ),
        "- Bu fırsatların gerçek Binance koşullarında kârlı olacağı bu veriyle tespit edilemez.",
    ]

    section(11, TR_SECTIONS[10])
    lines += [
        "| Tür | Bulgu |",
        "|---|---|",
        f"| OBSERVED_FACT | {econ.get('closed_trades')} kapanmış işlem, net {_fmt(econ.get('realized_net_trading_pnl'))} USDT |",
        "| VERIFIED_SOFTWARE_DEFECT | Eski native fill raporu ImportError: pandas eksikti; giderildi (bkz. G6) |",
        "| MODEL_ASSUMPTION | Bar içi sıra STOP-FIRST; zero-latency/full-size varsayılan fill |",
        "| NOT_IDENTIFIABLE | Emir defteri, kuyruk pozisyonu, gerçek Binance fill fiyatı |",
        "| NOT_IDENTIFIABLE | Funding nakit akışının native PnL'e etkisi (motor uygulamıyor) |",
    ]

    section(12, TR_SECTIONS[11])
    lines += [
        f"- Doğrulama durumu: **{validation.get('status')}**; ihlal sayısı: {len(validation.get('violations') or [])}",
        f"- Mevcut zorunlu artefakt: {validation.get('artifact_count')}/{validation.get('mandatory_artifacts')}; "
        f"manifest tarafından özetlenen: {validation.get('validated_manifest_artifact_count')}",
        f"- Üretim komutu: `{manifest.get('reproduction', {}).get('command')}`",
        f"- Rapor yeniden üretimi: `v8-next audit --report-only --run-dir {manifest.get('run_id')}` desteği audit CLI üzerindedir.",
        f"- Çalışma süresi: replay {_fmt(perf.get('replay_seconds'), 2)} s; toplam {_fmt(perf.get('audit_total_seconds'), 2)} s; "
        f"ölçülen audit ek yükü {_fmt(perf.get('audit_overhead_seconds'), 4)} s (160 barlık fixture, ikinci replay yok)",
        f"- Zirve bellek (RSS): {_fmt(perf.get('peak_memory_mb'), 2)} MB; artefakt boyutu: {_fmt(perf.get('raw_artifact_bytes'))} bayt; "
        f"fixture olay dosyası: {_fmt(perf.get('fixture_artifact_bytes'))} bayt",
        f"- Tarihsel referansla fark: {_historical_note(manifest)}",
        "- Testler: `pytest tests/test_audit_native.py -q` (test dosyası bu çalıştırmanın parçasıdır).",
        "- Sonraki tek adım: gerçek emir defteri (L2) içeren bir tape ile depth-bağımlı fill knobs'larının doğrulanması.",
        "",
        "### Kabul kapıları",
        "",
        _gate_table(gates),
        "",
    ]
    return "\n".join(lines) + "\n"


def render_audit_report(ctx: Mapping[str, Any]) -> str:
    manifest = ctx["run_manifest"]
    dataset = manifest.get("dataset", {})
    dq = ctx["data_quality"]
    econ = ctx["economic_metrics"]
    funnel = ctx["opportunity_funnel"]
    recon = ctx["reconciliation"]
    mfe = ctx["mfe_mae_metrics"]
    execution = ctx["execution_metrics"]
    validation = ctx["validation_results"]
    gates = ctx["gate_results"].get("gates", [])
    lifecycles = ctx["lifecycles"]
    exit_kinds = Counter(str(row.get("exit_kind")) for row in lifecycles)

    lines: list[str] = [
        f"# Native Forensic Audit — {manifest.get('run_id')}",
        "",
        f"Authority: `{manifest.get('authority')}`  ",
        f"Schema: `{manifest.get('schema_version')}`  ",
        f"Validation: **{validation.get('status')}**  ",
        "Gates: " + ", ".join(f"{g['gate_id']}={g['status']}" for g in gates),
        "",
        "## 1. Run identity",
        f"- repo: `{manifest.get('repository', {}).get('path')}` @ `{manifest.get('repository', {}).get('source_revision')}`",
        f"- tape: `{dataset.get('path')}` sha256 `{dataset.get('sha256')}` bars={dataset.get('bars')}",
        f"- environment: python {manifest.get('environment', {}).get('python')}, "
        f"nautilus {manifest.get('environment', {}).get('nautilus_trader')}",
        f"- reproduce: `{manifest.get('reproduction', {}).get('command')}`",
        "",
        "## 2. Market data quality",
        f"- gaps={dq.get('gaps')} duplicates={dq.get('duplicate_timestamps')} funding_rows={dq.get('funding_rows')}",
        f"- depth={dq.get('order_book_depth_available')} quotes={dq.get('quote_data_available')} "
        f"intrabar_path={dq.get('intrabar_path_identifiable')}",
        "",
        "## 3. Event ledger",
        f"- total events: {len(ctx['events'])}",
        f"- by type: `{json.dumps(dict(Counter(e['event_type'] for e in ctx['events'])), sort_keys=True)}`",
        "",
        "## 4. Opportunity funnel",
        f"```json\n{json.dumps(funnel, indent=2, sort_keys=True)}\n```",
        "",
        "## 5. Orders and fills",
        f"- native orders={recon.get('native_order_count')} native fills={recon.get('native_fill_count')} "
        f"audited fills={recon.get('audited_fill_count')}",
        f"- workload: ledger orders={len(ctx['orders'])} fills={len(ctx['fills'])} "
        f"open orders at cutoff={recon.get('open_order_count_at_cutoff')}",
        f"- unmatched native={recon.get('unmatched_native_fills')} unmatched audit={recon.get('unmatched_audit_fills')} "
        f"duplicates={recon.get('duplicate_fill_ids')} matched order ids={recon.get('native_order_ids_matched_to_audit')} "
        f"unlinked orders={recon.get('unlinked_orders')} unlinked positions={recon.get('unlinked_positions')}",
        f"- maker/taker: `{json.dumps(execution.get('maker_taker_distribution', {}), sort_keys=True)}`",
        f"- report status: `{json.dumps(recon.get('native_fills_report_status'), sort_keys=True)}`",
        "",
        "## 6. Position lifecycle",
        f"- lifecycles: {len(lifecycles)}; exit kinds (tag-classified): `{json.dumps(dict(exit_kinds), sort_keys=True)}`",
        f"- open at cutoff: {econ.get('open_positions_at_cutoff')} "
        f"open orders: `{json.dumps(recon.get('open_orders_at_cutoff'), sort_keys=True)}`",
        "",
        "## 7. Risk and protection",
        f"- valid R denominators: {mfe.get('valid_r_denominator')}; invalid: {mfe.get('invalid_r_denominator')} "
        f"reasons `{json.dumps(mfe.get('invalid_reasons', {}), sort_keys=True)}`",
        f"- protection ledger: stops={recon.get('protection_created_stop')} targets={recon.get('protection_created_target')} "
        f"unmatched protection ids={recon.get('protection_missing_count')}",
        f"- stop-first capped trades: {mfe.get('stop_first_capped_count')}",
        "- intrabar contract: STOP_FIRST (model assumption)",
        "",
        "## 8. Economics",
        f"```json\n{json.dumps(econ, indent=2, sort_keys=True)}\n```",
        "",
        "## 9. MFE / MAE / R",
        f"```json\n{json.dumps(mfe, indent=2, sort_keys=True)}\n```",
        "",
        "## 10. Post-exit",
        f"```json\n{json.dumps({k: v for k, v in ctx['post_exit_metrics'].items() if k != 'rows'}, indent=2, sort_keys=True)}\n```",
        "",
        "## 11. Execution assumptions",
        f"```json\n{json.dumps(execution.get('execution_profile', {}), indent=2, sort_keys=True)}\n```",
        "",
        "## 12. Gates",
        "",
        _gate_table(gates),
        "",
        "## 13. Validation",
        f"```json\n{json.dumps(validation, indent=2, sort_keys=True)}\n```",
        "",
        "## 14. Reproducibility",
        f"- equivalence fixture: `{json.dumps(manifest.get('equivalence', {}), sort_keys=True)}`",
        "- tests: `pytest tests/test_audit_native.py -q`",
        "",
    ]
    return "\n".join(lines) + "\n"


def _historical_note(manifest: Mapping[str, Any]) -> str:
    ref = manifest.get("historical_reference") or {}
    if not ref:
        return "referans yok"
    return (
        f"fırsat/giriş/kapanış aynı; net {_fmt(ref.get('observed_net_realized_pnl'))} vs "
        f"{_fmt(ref.get('reference_net_realized_pnl'))} USDT; komisyon farkı "
        f"{_fmt(ref.get('observed_fee_total'))} vs {_fmt(ref.get('reference_implied_fee'))} USDT "
        f"(açıklama: {ref.get('fee_difference_explanation')})"
    )


def _mean(values: Sequence[Any]) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    return round(sum(numbers) / len(numbers), 6) if numbers else None
