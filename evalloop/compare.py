"""Diff two runs and apply the regression gate.

Comparison is PAIRED on case id: the pass-rate delta, category deltas and regressions are all
computed only over cases present and error-free in both runs, so adding cases to the suite
never looks like a regression. Cases that are new in the candidate are reported (and count
toward the absolute floor) but are not compared.
"""
from __future__ import annotations

from math import comb

import yaml

from .db import ROOT


def mcnemar_exact_p(regressed: int, fixed: int) -> float:
    """Two-sided exact sign test on discordant pairs (H0: a flip is equally likely either way)."""
    n = regressed + fixed
    if n == 0:
        return 1.0
    k = min(regressed, fixed)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def _pct_change(new, old):
    return None if not old else (new - old) / old * 100


def diff_runs(base: dict, cand: dict) -> dict:
    bs, cs = base["summary"], cand["summary"]
    b = {r["case_id"]: r for r in base["results"]}
    c = {r["case_id"]: r for r in cand["results"]}
    shared = [i for i in c if i in b and not b[i].get("error") and not c[i].get("error")]
    regressions = [i for i in shared if b[i]["passed"] and not c[i]["passed"]]
    fixes = [i for i in shared if not b[i]["passed"] and c[i]["passed"]]
    still_failing = [i for i in shared if not b[i]["passed"] and not c[i]["passed"]]
    n = len(shared)
    bp = sum(b[i]["passed"] for i in shared)
    cp = sum(c[i]["passed"] for i in shared)
    cats = {}
    for cat in sorted({c[i]["category"] for i in shared}):
        ids = [i for i in shared if c[i]["category"] == cat]
        br = sum(b[i]["passed"] for i in ids) / len(ids)
        cr = sum(c[i]["passed"] for i in ids) / len(ids)
        cats[cat] = {"n": len(ids), "base_rate": br, "cand_rate": cr, "delta_pts": (cr - br) * 100}
    return {
        "base_run": bs["run_id"], "cand_run": cs["run_id"],
        "same_case_set": bs["cases_sha"] == cs["cases_sha"],
        "same_agent_config": bs["agent_config"]["config_hash"] == cs["agent_config"]["config_hash"],
        "n_paired": n, "base_pass_rate": bp / n if n else None, "cand_pass_rate": cp / n if n else None,
        "delta_pts": ((cp - bp) / n * 100) if n else None,
        "regressions": regressions, "fixes": fixes, "still_failing": still_failing,
        "new_cases": [i for i in c if i not in b], "removed_cases": [i for i in b if i not in c],
        "mcnemar_p": mcnemar_exact_p(len(regressions), len(fixes)),
        "by_category": cats,
        "cost_per_case_pct": _pct_change(cs["cost_usd"]["agent_per_case"], bs["cost_usd"]["agent_per_case"]),
        "p95_latency_pct": _pct_change(cs["latency_s"]["p95"], bs["latency_s"]["p95"]),
        "cand_overall_rate": cs["overall"]["rate"], "cand_error_rate": cs["n_errors"] / max(1, cs["n_cases"]),
        "regression_details": {i: {"category": c[i]["category"], "tags": c[i]["tags"], "question": c[i]["question"],
                                   "failure_type": c[i]["failure_type"], "pred_sql": c[i].get("pred_sql")} for i in regressions},
    }


def load_thresholds(path=None) -> dict:
    return yaml.safe_load((path and open(path) or open(ROOT / "gate" / "thresholds.yaml")).read())


def gate(d: dict | None, cand: dict, thr: dict) -> dict:
    """Returns {status: pass|fail, failures: [...], warnings: [...]}. `d` is None when no baseline exists."""
    fails, warns = [], []
    cs = cand["summary"]
    err_rate = cs["n_errors"] / max(1, cs["n_cases"])
    if err_rate > thr["errors"]["max_error_rate"]:
        fails.append(f"error rate {err_rate:.0%} > {thr['errors']['max_error_rate']:.0%}: run is not trustworthy")
    rate = cs["overall"]["rate"]
    if rate is None or rate < thr["min_pass_rate"]:
        fails.append(f"pass rate {0 if rate is None else rate:.1%} below absolute floor {thr['min_pass_rate']:.0%}")
    if d is None:
        warns.append("no baseline: only absolute rules were applied")
        return {"status": "fail" if fails else "pass", "failures": fails, "warnings": warns}

    if not d["same_case_set"]:
        warns.append(f"case set differs (+{len(d['new_cases'])} new, -{len(d['removed_cases'])} removed); comparing shared cases only")
    if d["delta_pts"] is not None and d["delta_pts"] < -thr["pass_rate"]["max_drop_pts"]:
        fails.append(f"pass rate on {d['n_paired']} paired cases fell {abs(d['delta_pts']):.1f} pts "
                     f"({d['base_pass_rate']:.1%} -> {d['cand_pass_rate']:.1%}), limit {thr['pass_rate']['max_drop_pts']} pts")
    crit = set(thr["critical"]["tags"])
    for i in d["regressions"]:
        if crit & set(d["regression_details"][i]["tags"]):
            fails.append(f"critical case regressed: {i} ({d['regression_details'][i]['failure_type']})")
    for cat, v in d["by_category"].items():
        if v["n"] >= thr["category"]["min_cases"] and v["delta_pts"] < -thr["category"]["max_drop_pts"]:
            fails.append(f"category '{cat}' fell {abs(v['delta_pts']):.0f} pts ({v['base_rate']:.0%} -> {v['cand_rate']:.0%}, n={v['n']})")
    if len(d["regressions"]) > thr["max_regressed_cases"]:
        fails.append(f"{len(d['regressions'])} cases regressed (limit {thr['max_regressed_cases']}): {', '.join(d['regressions'])}")
    if d["cost_per_case_pct"] is not None and d["cost_per_case_pct"] > thr["cost"]["max_increase_pct"]:
        warns.append(f"cost/case up {d['cost_per_case_pct']:.0f}% (limit {thr['cost']['max_increase_pct']}%)")
    if d["p95_latency_pct"] is not None and d["p95_latency_pct"] > thr["latency"]["p95_max_increase_pct"]:
        warns.append(f"p95 latency up {d['p95_latency_pct']:.0f}% (limit {thr['latency']['p95_max_increase_pct']}%)")
    if d["regressions"] and d["mcnemar_p"] > 0.05:
        warns.append(f"regressions are not statistically distinguishable from noise on this small suite (exact McNemar p={d['mcnemar_p']:.2f}); "
                     "gate is deliberately stricter than significance testing because the suite is small")
    return {"status": "fail" if fails else "pass", "failures": fails, "warnings": warns}


def render_text(d: dict | None, g: dict, cand: dict) -> str:
    cs = cand["summary"]
    L = [f"candidate: {cs['run_id']}  config={cs['agent_config']['name']}#{cs['agent_config']['config_hash']}  "
         f"pass={cs['overall']['passed']}/{cs['overall']['n']}"]
    if d:
        L.append(f"baseline : {d['base_run']}")
        L.append(f"paired cases: {d['n_paired']}   pass rate {d['base_pass_rate']:.1%} -> {d['cand_pass_rate']:.1%} ({d['delta_pts']:+.1f} pts)")
        L.append(f"regressions ({len(d['regressions'])}): " + (", ".join(d["regressions"]) or "none"))
        for i in d["regressions"]:
            x = d["regression_details"][i]
            L.append(f"    - {i} [{x['category']}] {x['failure_type']}: {x['question']}")
            L.append(f"        agent output: {x['pred_sql']}")
        L.append(f"fixes ({len(d['fixes'])}): " + (", ".join(d["fixes"]) or "none") +
                 f"    still failing ({len(d['still_failing'])}): " + (", ".join(d["still_failing"]) or "none"))
        if d["new_cases"]:
            L.append(f"new cases (not compared): {', '.join(d['new_cases'])}")
        L.append("by category (paired): " + "  ".join(f"{k}:{v['base_rate']:.0%}->{v['cand_rate']:.0%}" for k, v in d["by_category"].items()))
        f = lambda x: "n/a" if x is None else f"{x:+.0f}%"
        L.append(f"cost/case {f(d['cost_per_case_pct'])}   p95 latency {f(d['p95_latency_pct'])}")
    L += [f"WARN: {w}" for w in g["warnings"]]
    L += [f"FAIL: {x}" for x in g["failures"]]
    L.append(f"GATE: {g['status'].upper()}")
    return "\n".join(L)
