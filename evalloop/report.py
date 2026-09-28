"""Static HTML report: pass rate over time, category breakdown, cost/latency, gate status, drill-down.

Deliberately dependency-free (inline SVG, <details> for drill-down). At 10k cases/run the drill-down
would be served from the run store with pagination instead of inlined; see ARCHITECTURE.md.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import compare, store
from .db import ROOT

E = html.escape
CSS = """body{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1150px;color:#1a1a1a}
table{border-collapse:collapse;margin:8px 0 20px}td,th{border:1px solid #ccc;padding:4px 9px;text-align:left;vertical-align:top}
th{background:#f3f3f3}.pass{color:#0a7a2f;font-weight:600}.fail{color:#b3261e;font-weight:600}.warn{color:#a15c00}
.bar{display:inline-block;height:10px;background:#3b82f6;vertical-align:middle}pre{background:#f6f6f6;padding:8px;overflow:auto;white-space:pre-wrap}
details.case{margin:2px 0}details.case>summary{cursor:pointer}h2{margin-top:32px;border-bottom:1px solid #ddd}.mono{font-family:monospace;font-size:12px}
.note{background:#fff8e1;border:1px solid #f0d98c;padding:8px 12px}"""


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def _chart(runs):
    if not runs:
        return ""
    W, H, P = 900, 220, 34
    xs = [P + i * (W - 2 * P) / max(1, len(runs) - 1) for i in range(len(runs))]
    ys = [H - P - (r["summary"]["overall"]["rate"] or 0) * (H - 2 * P) for r in runs]
    pts = " ".join(f"{x:.0f},{y:.0f}" for x, y in zip(xs, ys))
    g = "".join(f'<line x1="{P}" x2="{W-P}" y1="{H-P-v*(H-2*P):.0f}" y2="{H-P-v*(H-2*P):.0f}" stroke="#e5e5e5"/>'
                f'<text x="2" y="{H-P-v*(H-2*P)+4:.0f}" font-size="11">{int(v*100)}%</text>' for v in (0, .25, .5, .75, 1))
    dots = "".join(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="5" fill="#3b82f6"><title>{E(r["summary"]["run_id"])}: {_pct(r["summary"]["overall"]["rate"])}</title></circle>'
                   f'<text x="{x:.0f}" y="{H-8}" font-size="10" text-anchor="middle">{E(r["summary"]["agent_config"]["name"][:14])}</text>'
                   for x, y, r in zip(xs, ys, runs))
    return f'<svg width="{W}" height="{H}">{g}<polyline points="{pts}" fill="none" stroke="#3b82f6" stroke-width="2"/>{dots}</svg>'


def build(runs_dir=store.RUNS_DIR, out=ROOT / "report" / "index.html") -> Path:
    ids = store.list_runs(runs_dir)
    runs = [store.load_run(i, runs_dir) for i in ids]
    base = None
    try:
        base = store.load_run("baseline", runs_dir)
    except FileNotFoundError:
        pass
    thr = compare.load_thresholds()
    P = ["<!doctype html><meta charset=utf-8><title>Eval report</title>", f"<style>{CSS}</style>", "<h1>NL-to-SQL eval report</h1>"]

    # ---- runs table with gate status vs baseline
    P.append("<h2>Runs over time</h2>" + _chart(runs))
    P.append("<table><tr><th>run</th><th>config (hash)</th><th>git</th><th>cases</th><th>pass rate</th><th>errors</th>"
             "<th>agent $/case</th><th>p50 / p95 latency</th><th>tokens in/out</th><th>gate vs baseline</th></tr>")
    for r in runs:
        s = r["summary"]
        if base and s["run_id"] != base["summary"]["run_id"]:
            g = compare.gate(compare.diff_runs(base, r), r, thr)
            gate_cell = f'<span class="{g["status"]}">{g["status"].upper()}</span>' + "".join(f'<div class="mono">{E(x)}</div>' for x in g["failures"])
        else:
            gate_cell = "baseline" if base and s["run_id"] == base["summary"]["run_id"] else "-"
        c = s["cost_usd"]["agent_per_case"]
        P.append(f"<tr><td class=mono>{E(s['run_id'])}</td><td>{E(s['agent_config']['name'])} ({s['agent_config']['config_hash']})"
                 f"<div class=mono>{E(s['agent_config']['provider']['type'])}/{E(s['agent_config']['model'])}</div></td><td class=mono>{s['git_sha']}</td>"
                 f"<td>{s['n_cases']}</td><td>{s['overall']['passed']}/{s['overall']['n']} = {_pct(s['overall']['rate'])}</td><td>{s['n_errors']}</td>"
                 f"<td>{'n/a' if c is None else f'${c:.5f}'}</td><td>{s['latency_s']['p50']:.2f}s / {s['latency_s']['p95']:.2f}s</td>"
                 f"<td>{s['tokens']['prompt']:,} / {s['tokens']['completion']:,}</td><td>{gate_cell}</td></tr>")
    P.append("</table>")
    if any(r["summary"]["agent_config"]["provider"]["type"] == "mock" for r in runs):
        P.append('<p class="note"><b>Note:</b> runs using the <code>mock</code> provider are deterministic offline stand-ins used to exercise the loop. '
                 "Their latency is synthetic and their token counts are estimated (chars/4); they say nothing about a real model.</p>")

    # ---- category breakdown, latest vs baseline
    if runs:
        latest = runs[-1]
        P.append(f"<h2>Breakdown by category: {E(latest['summary']['run_id'])}</h2><table><tr><th>category</th><th>n</th><th>latest</th><th></th><th>baseline (paired)</th></tr>")
        d = compare.diff_runs(base, latest) if base and latest is not base else None
        for cat, v in latest["summary"]["by_category"].items():
            b = f"{d['by_category'][cat]['base_rate']:.0%}" if d and cat in d["by_category"] else "-"
            P.append(f"<tr><td>{cat}</td><td>{v['n']}</td><td>{v['passed']}/{v['n']} ({v['rate']:.0%})</td>"
                     f"<td><span class=bar style='width:{int(v['rate']*120)}px'></span></td><td>{b}</td></tr>")
        P.append("</table><table><tr><th>difficulty</th><th>n</th><th>pass</th></tr>" + "".join(
            f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['passed']}/{v['n']} ({v['rate']:.0%})</td></tr>" for k, v in latest["summary"]["by_difficulty"].items()) + "</table>")
        if d:
            P.append("<h2>Diff vs baseline</h2><pre>" + E(compare.render_text(d, compare.gate(d, latest, thr), latest)) + "</pre>")
        ja = latest["summary"]["judge_exec_agreement"]
        P.append(f"<p>Judge vs execution agreement in this run: {ja['n']} cases, {_pct(ja['rate'])}.</p>")

    # ---- judge calibration
    cal = ROOT / "evals" / "calibration"
    for f in sorted(cal.glob("*.json")) if cal.exists() else []:
        c = json.loads(f.read_text())
        P.append(f"<h2>Judge calibration: {E(c['judge_config']['name'])}</h2><p>{c['n_labels']} human labels. agreement {c['agreement']:.0%}, kappa {c['kappa']:.2f}, "
                 f"false-accept {c['false_accept_rate']:.0%}, false-reject {c['false_reject_rate']:.0%}. "
                 f"<b class={'pass' if c['trusted_for_gating'] else 'fail'}>{'TRUSTED' if c['trusted_for_gating'] else 'NOT trusted'} for gating</b> "
                 f"(bar: kappa &ge; {c['thresholds']['min_kappa']}, false-accept &le; {c['thresholds']['max_false_accept']:.0%}).</p>")

    # ---- drill-down
    P.append("<h2>Drill-down (failures first)</h2>")
    for r in reversed(runs):
        s = r["summary"]
        P.append(f"<details><summary><b>{E(s['run_id'])}</b>: {s['overall']['passed']}/{s['overall']['n']} passed</summary>")
        for x in sorted(r["results"], key=lambda x: (x["passed"], x["case_id"])):
            mark = '<span class=pass>PASS</span>' if x["passed"] else f'<span class=fail>FAIL</span> {E(str(x.get("failure_type")))}'
            body = {k: x.get(k) for k in ("question", "gold_sql", "pred_sql", "raw_output", "exec", "judge", "latency_s", "prompt_tokens",
                                          "completion_tokens", "cost_usd", "cached", "tags", "source", "error")}
            P.append(f"<details class=case><summary>{mark} <b>{E(x['case_id'])}</b> [{E(x['category'])}] {E(x['question'])}</summary>"
                     f"<pre>{E(json.dumps(body, indent=1, ensure_ascii=False))}</pre></details>")
        P.append("</details>")
    out.parent.mkdir(exist_ok=True, parents=True)
    out.write_text("\n".join(P))
    return out
