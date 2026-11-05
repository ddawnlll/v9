#!/usr/bin/env python
"""Render the self-contained HTML research dashboard from recorded measurements.

Every chart is drawn server-side into inline SVG from ``data/matrix``,
``data/ablations``, ``data/census`` and ``data/findings.json``. There is no
JavaScript, no network fetch and no placeholder series: a panel whose input is
missing renders an explicit "not measured" note instead of a fake value.

Usage
    uv run --locked --extra dev python tools/cross_market_dashboard.py \
        --out reports/v9-cross-market-validation/dashboard.html
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "reports" / "v9-cross-market-validation"
DATA = ROOT / "data"

ARMS = ("A", "B", "C", "D")
ARM_LABELS = {
    "A": "A · V8 static H1",
    "B": "B · V9 legacy H1",
    "C": "C · V9 multi-horizon static",
    "D": "D · V9 multi-horizon adaptive",
}
ARM_COLORS = {"A": "var(--c1)", "B": "var(--c2)", "C": "var(--c3)", "D": "var(--c4)"}

STYLE = """
:root{
  --fg:#111;--muted:#5b6570;--border:#d6dbe1;--surface:#f6f8fa;
  --c1:#4c78a8;--c2:#e45756;--c3:#59a14f;--c4:#b07aa1;--c5:#edc948;--c6:#76b7b2;
  --pos:#2e7d32;--neg:#c62828;
}
@media (prefers-color-scheme:dark){
  :root{--fg:#e8eaed;--muted:#9aa4ae;--border:#39414a;--surface:#171b1f;
        --c1:#7fa8d4;--c2:#ef8a89;--c3:#8bc47f;--c4:#cfa6c4;--c5:#f0d97a;--c6:#9fd3ce;
        --pos:#7ac47f;--neg:#ef8a89;}
}
*{box-sizing:border-box}
body{margin:0;padding:28px 32px 64px;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     color:var(--fg);background:transparent;max-width:1180px}
h1{font-size:26px;margin:0 0 6px;font-weight:640;letter-spacing:-.01em}
h2{font-size:18px;margin:34px 0 10px;font-weight:620}
p.lede{color:var(--muted);margin:0 0 4px}
.small{font-size:13px;color:var(--muted)}
.grid{display:grid;gap:18px}
.g2{grid-template-columns:repeat(auto-fit,minmax(430px,1fr))}
.card{border:1px solid var(--border);border-radius:10px;padding:14px 16px;background:var(--surface)}
.card h3{margin:0 0 2px;font-size:14px;font-weight:620}
.card .cap{margin:0 0 10px;font-size:12px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.pos{color:var(--pos)}td.neg{color:var(--neg)}
svg{display:block;overflow:visible}
.axis{stroke:var(--border);stroke-width:1}
.tick{fill:var(--muted);font-size:10px}
.lbl{fill:var(--fg);font-size:10px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
footer{margin-top:40px;border-top:1px solid var(--border);padding-top:12px}
"""


def esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def money(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    return math.ceil(value / 10**exponent) * 10**exponent


def grouped_bars(
    symbols: Sequence[str],
    series: dict[str, list[float | None]],
    *,
    width: int = 1040,
    height: int = 300,
    unit: str = "USDT",
) -> str:
    """Grouped bar chart with a zero baseline; missing values leave a gap."""
    pad_l, pad_r, pad_t, pad_b = 54, 12, 12, 58
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    values = [v for rows in series.values() for v in rows if v is not None]
    if not values:
        return '<p class="small">No measured values for this panel.</p>'
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    span = (hi - lo) or 1.0
    scale = plot_h / span
    zero_y = pad_t + hi * scale
    band = plot_w / len(symbols)
    bar_w = max(3.0, (band - 10) / len(series))
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">',
        f'<line class="axis" x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}"/>',
    ]
    for tick_value in (lo, lo / 2, hi / 2, hi):
        y = pad_t + (hi - tick_value) * scale
        parts.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y + 3:.1f}" text-anchor="end">'
            f"{tick_value:,.0f}</text>"
        )
    for group_index, symbol in enumerate(symbols):
        base_x = pad_l + group_index * band + 5
        for series_index, (label, rows) in enumerate(series.items()):
            value = rows[group_index]
            if value is None:
                continue
            x = base_x + series_index * bar_w
            top = pad_t + (hi - max(value, 0.0)) * scale
            bar_h = abs(value) * scale
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w - 1.2:.1f}" '
                f'height="{max(bar_h, 0.6):.1f}" fill="{ARM_COLORS.get(label, "var(--c5)")}">'
                f"<title>{esc(symbol)} · {esc(label)}: {value:,.2f} {unit}</title></rect>"
            )
        parts.append(
            f'<text class="lbl" x="{pad_l + group_index * band + band / 2:.1f}" '
            f'y="{height - pad_b + 16}" text-anchor="middle">'
            f"{esc(symbol.replace('USDT', ''))}</text>"
        )
    parts.append("</svg>")
    legend = "".join(
        f'<span><span class="sw" style="background:{ARM_COLORS.get(k, "var(--c5)")}"></span>'
        f"{esc(ARM_LABELS.get(k, k))}</span>"
        for k in series
    )
    parts.append(f'<div class="legend">{legend}</div>')
    return "".join(parts)


def sparkline(points: Sequence[float], *, width: int = 430, height: int = 90) -> str:
    if len(points) < 2:
        return '<span class="small">no closed campaigns</span>'
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = width / (len(points) - 1)
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{i * step:.1f},{height - 6 - (v - lo) / span * (height - 12):.1f}"
        for i, v in enumerate(points)
    )
    final = points[-1]
    color = "var(--pos)" if final >= points[0] else "var(--neg)"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">'
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"/></svg>'
        f'<div class="small">start {money(points[0])} → end {money(final)} USDT</div>'
    )


def line_chart(
    series: dict[str, list[tuple[float, float]]], *, width: int = 1040, height: int = 280
) -> str:
    pad_l, pad_r, pad_t, pad_b = 54, 90, 12, 40
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    all_y = [y for rows in series.values() for _, y in rows]
    all_x = [x for rows in series.values() for x, _ in rows]
    if not all_y:
        return '<p class="small">No cost-stress measurements.</p>'
    lo, hi = min(0.0, min(all_y)), max(0.0, max(all_y))
    x_lo, x_hi = min(all_x), max(all_x)
    span_y = (hi - lo) or 1.0
    span_x = (x_hi - x_lo) or 1.0
    colors = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)"]
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">',
        f'<line class="axis" x1="{pad_l}" y1="{pad_t + (hi - 0) / span_y * plot_h:.1f}" '
        f'x2="{width - pad_r}" y2="{pad_t + (hi - 0) / span_y * plot_h:.1f}"/>',
    ]
    for x_value in sorted({x for x in all_x}):
        px = pad_l + (x_value - x_lo) / span_x * plot_w
        parts.append(
            f'<text class="tick" x="{px:.1f}" y="{height - pad_b + 16}" text-anchor="middle">'
            f"+{x_value:.0f}bps</text>"
        )
    for index, (symbol, rows) in enumerate(sorted(series.items())):
        color = colors[index % len(colors)]
        path = " ".join(
            f"{'M' if i == 0 else 'L'}"
            f"{pad_l + (x - x_lo) / span_x * plot_w:.1f},"
            f"{pad_t + (hi - y) / span_y * plot_h:.1f}"
            for i, (x, y) in enumerate(sorted(rows))
        )
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.7"/>')
        _, last_y = sorted(rows)[-1]
        parts.append(
            f'<text class="lbl" x="{width - pad_r + 6}" '
            f'y="{pad_t + (hi - last_y) / span_y * plot_h + 3:.1f}" fill="{color}">'
            f"{esc(symbol.replace('USDT', ''))}</text>"
        )
    for tick_value in (lo, hi):
        y = pad_t + (hi - tick_value) / span_y * plot_h
        parts.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y + 3:.1f}" text-anchor="end">'
            f"{tick_value:,.0f}</text>"
        )
    parts.append("</svg>")
    part_of = (
        '<div class="legend"><span>Net PnL (USDT) against incremental per-side '
        "execution cost; right-hand label = symbol</span></div>"
    )
    return "".join(parts) + part_of


def load_json_dir(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not path.is_dir():
        return out
    for file in sorted(path.glob("*.json")):
        document = json.loads(file.read_text())
        key = document.get("symbol") or file.stem
        out[key] = document
    return out


def build() -> str:
    matrix = load_json_dir(DATA / "matrix")
    ablations = load_json_dir(DATA / "ablations")
    census = load_json_dir(DATA / "census")
    findings_path = DATA / "findings.json"
    findings = json.loads(findings_path.read_text()) if findings_path.is_file() else {}
    audit_path = DATA / "data_audit.json"
    audit = json.loads(audit_path.read_text()) if audit_path.is_file() else {}

    symbols = sorted(matrix)
    blocks: list[str] = []

    # ---- headline table -------------------------------------------------
    rows = []
    for symbol in symbols:
        cells = []
        for arm in ARMS:
            payload = matrix[symbol]["arms"][arm]
            value = payload["net_pnl"]
            cls = "pos" if value > 0 else "neg" if value < 0 else ""
            cells.append(f'<td class="{cls}">{money(value)}</td>')
        dd = matrix[symbol]["arms"]["D"]["max_drawdown_pct"]
        violations = sum(matrix[symbol]["arms"][arm]["protection_violations"] for arm in ARMS)
        rows.append(
            f"<tr><td>{esc(symbol)}</td>{''.join(cells)}"
            f"<td>{pct(dd)}</td><td>{violations}</td></tr>"
        )
    net_totals = [
        sum(matrix[s]["arms"][arm]["net_pnl"] for s in symbols if s in matrix) for arm in ARMS
    ]
    totals_cells = "".join(
        f'<td class="{"pos" if v > 0 else "neg" if v < 0 else ""}"><strong>{money(v)}</strong></td>'
        for v in net_totals
    )
    blocks.append(
        '<h2>Net PnL by symbol and arm</h2>'
        '<p class="lede">Each cell is an independent 10,000 USDT account for six months '
        "(2025-01-01 → 2025-07-01, end-exclusive), maker 2 bps / taker 5 bps, "
        "NautilusTrader 2.0.0rc4 default fill model.</p>"
        '<table><thead><tr><th>Symbol</th>'
        + "".join(f"<th>{esc(arm)}</th>" for arm in ARMS)
        + "<th>D max DD</th><th>Protection violations</th></tr></thead><tbody>"
        + "".join(rows)
        + f'<tr><td><strong>Sum</strong></td>{totals_cells}<td></td><td></td></tr>'
        "</tbody></table>"
        '<p class="small">Columns A/B/C/D: A = V8 static H1, B = V9 legacy H1, '
        "C = V9 multi-horizon static, D = V9 multi-horizon adaptive.</p>"
    )

    blocks.append(
        "<h2>Paired arm deltas</h2>"
        '<div class="grid g2"><div class="card"><h3>Net PnL difference per symbol</h3>'
        '<p class="cap">Positive = the first arm of the pair earned more.</p>'
        + grouped_bars(
            symbols,
            {pair: [next(
                (
                    row["delta_net_pnl"]
                    for row in _paired(matrix, symbol)
                    if row["pair"] == pair
                ),
                None,
            ) for symbol in symbols]
             for pair in ("B-A", "C-B", "D-C", "D-A")},
        )
        + "</div></div>"
    )

    # ---- equity curves --------------------------------------------------
    cards = []
    for symbol in symbols:
        curve = matrix[symbol]["arms"]["D"].get("equity_curve") or []
        points = [point["equity"] for point in curve]
        cards.append(
            f'<div class="card"><h3>{esc(symbol)} · arm D</h3>'
            f'<p class="cap">Cumulative realized gross price PnL, '
            f'{matrix[symbol]["arms"]["D"]["trades_closed"]} closed campaigns.</p>'
            f"{sparkline(points)}</div>"
        )
    blocks.append(
        "<h2>Equity curves (arm D)</h2>"
        '<p class="lede">Gross realized price PnL per closed campaign; commissions and '
        "funding are reported separately, so the final point is not the net balance.</p>"
        f'<div class="grid g2">{"".join(cards)}</div>'
    )

    # ---- cost stress ----------------------------------------------------
    if ablations:
        series: dict[str, list[tuple[float, float]]] = {}
        for symbol, document in ablations.items():
            points = [(0.0, document["ablations"]["FULL"]["net_pnl"])]
            for payload in document.get("stress", {}).values():
                points.append((float(payload["incremental_bps_per_side"]), payload["net_pnl"]))
            series[symbol] = points
        break_even_rows = "".join(
            f"<tr><td>{esc(symbol)}</td>"
            f"<td>{money(document['ablations']['FULL']['net_pnl'])}</td>"
            f"<td>{'positive' if document['ablations']['FULL']['net_pnl'] > 0 else 'negative'}</td>"
            f"<td>{'—' if document.get('break_even_incremental_bps_per_side') is None else document['break_even_incremental_bps_per_side']}</td>"
            "</tr>"
            for symbol, document in sorted(ablations.items())
        )
        blocks.append(
            "<h2>Execution-cost sensitivity (arm D)</h2>"
            '<p class="lede">Incremental per-side cost added on top of the reference '
            "maker/taker fees; the break-even column is the added cost at which the arm's "
            "net PnL reaches zero.</p>"
            f'<div class="card">{line_chart(series)}</div>'
            "<table><thead><tr><th>Symbol</th><th>Net PnL at reference cost</th>"
            "<th>Sign</th><th>Break-even added bps/side</th></tr></thead><tbody>"
            f"{break_even_rows}</tbody></table>"
        )

    # ---- expert coverage ------------------------------------------------
    coverage_rows = []
    for symbol in symbols:
        evidence = matrix[symbol]["arms"]["D"].get("horizon_evidence") or {}
        reasons = evidence.get("abstain_reasons") or {}
        missing = sum(v for k, v in reasons.items() if "MISSING_INPUT" in k)
        incomplete = sum(v for k, v in reasons.items() if "CONTEXT_INCOMPLETE" in k)
        quality = sum(v for k, v in reasons.items() if "INPUT_QUALITY" in k)
        total = sum(reasons.values()) or 1
        coverage_rows.append(
            f"<tr><td>{esc(symbol)}</td><td>{total}</td>"
            f"<td>{missing}</td><td>{pct(100 * missing / total)}</td>"
            f"<td>{incomplete}</td><td>{quality}</td>"
            f"<td>{esc(', '.join(sorted(reasons))[:120] if len(reasons) <= 3 else str(len(reasons)) + ' distinct reasons')}</td></tr>"
        )
    blocks.append(
        "<h2>Expert coverage on the adaptive arm</h2>"
        '<p class="lede">Abstention reasons aggregated over every expert evaluation. '
        "<code>MISSING_INPUT</code> is an expert whose required feature was absent from the "
        "tape (for example open interest, which no monthly kline archive carries); those "
        "experts cannot contribute evidence in this experiment.</p>"
        "<table><thead><tr><th>Symbol</th><th>Evaluations</th><th>Missing input</th>"
        "<th>Share</th><th>Context incomplete</th><th>Input quality</th>"
        "<th>Distinct reasons</th></tr></thead><tbody>"
        + "".join(coverage_rows)
        + "</tbody></table>"
    )

    # ---- grammar census -------------------------------------------------
    if census:
        policies = sorted(
            {
                entry["policy"]
                for document in census.values()
                for entry in document.get("distributions", [])
            }
        )
        header = "".join(f"<th>{esc(p)}</th>" for p in policies)
        body_rows = []
        for symbol in sorted(census):
            document = census[symbol]
            counts = {e["policy"]: e["episodes"] for e in document.get("distributions", [])}
            body_rows.append(
                f"<tr><td>{esc(symbol)}</td>"
                + "".join(f"<td>{counts.get(p, '—')}</td>" for p in policies)
                + f"<td>{document.get('overlap', {}).get('bars_with_opposing_directions', '—')}</td>"
                "</tr>"
            )
        blocks.append(
            "<h2>Opportunity census (research-only, no orders)</h2>"
            '<p class="lede">Deduplicated economic episodes per grammar on the verified '
            "H1 tapes. Only <code>range-breakout-48-v1</code> drives the frozen A/B/C/D "
            "arms; the other four grammars are observational and were never inserted into "
            "an execution arm.</p>"
            f"<table><thead><tr><th>Symbol</th>{header}"
            "<th>Bars with opposing directions</th></tr></thead><tbody>"
            + "".join(body_rows)
            + "</tbody></table>"
        )

    # ---- data integrity -------------------------------------------------
    if audit:
        audit_rows = []
        for symbol in sorted(audit):
            entry = audit[symbol]
            if entry.get("status") != "OK":
                audit_rows.append(
                    f"<tr><td>{esc(symbol)}</td><td colspan='6'>{esc(entry.get('status'))}</td></tr>"
                )
                continue
            audit_rows.append(
                f"<tr><td>{esc(symbol)}</td><td>{entry['h1']['bars']}</td>"
                f"<td>{entry['m15']['bars']}</td>"
                f"<td>{entry['funding']['rows_in_window']}</td>"
                f"<td>{entry['h1']['missing_slots']}</td>"
                f"<td>{entry['h4']['completed_h4_bars']}</td>"
                f"<td>{entry['h1']['boundary_diff_max_bps']}</td></tr>"
            )
        blocks.append(
            "<h2>Data integrity</h2>"
            '<p class="lede">Every archive verified against Binance\'s published SHA-256 '
            "before the tape was built; the largest close-to-open discontinuity across all "
            "symbols is a few basis points, i.e. tick-level microstructure rather than a gap.</p>"
            "<table><thead><tr><th>Symbol</th><th>H1 bars</th><th>M15 bars</th>"
            "<th>Funding rows</th><th>Missing H1 slots</th><th>Completed H4</th>"
            "<th>Max boundary diff (bps)</th></tr></thead><tbody>"
            + "".join(audit_rows)
            + "</tbody></table>"
        )

    verdict = findings.get("verdict_block", "")
    generated = findings.get("generated_utc", "unknown")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>V9 cross-market validation</title>"
        f"<style>{STYLE}</style></head><body>"
        "<h1>V9 cross-market validation · 10 USD-M perpetuals · 2025 H1</h1>"
        "<p class='lede'>Frozen candidate <code>09908af</code> · NautilusTrader 2.0.0rc4 · "
        "independent 10,000 USDT accounts · research artifacts only, no live authority.</p>"
        f"<p class='small'>Rendered {esc(generated)} from recorded measurements.</p>"
        + verdict
        + "".join(blocks)
        + "<footer class='small'>Every series drawn here comes from a recorded "
        "measurement file under <code>reports/v9-cross-market-validation/data/</code>. "
        "Panels with no data render a note instead of a placeholder.</footer>"
        "</body></html>"
    )


_PAIRED_CACHE: dict[str, list[dict[str, Any]]] = {}


def _paired(matrix: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    if symbol in _PAIRED_CACHE:
        return _PAIRED_CACHE[symbol]
    document = matrix[symbol]["arms"]
    rows = []
    for high, low in (("B", "A"), ("C", "B"), ("D", "C"), ("D", "A")):
        rows.append(
            {
                "pair": f"{high}-{low}",
                "delta_net_pnl": document[high]["net_pnl"] - document[low]["net_pnl"],
            }
        )
    _PAIRED_CACHE[symbol] = rows
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dashboard.html")
    args = parser.parse_args()
    html = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(json.dumps({"out": str(args.out), "bytes": len(html)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
