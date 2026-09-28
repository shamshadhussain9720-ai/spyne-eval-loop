"""Measure the model judge against HUMAN labels.

evals/judge_labels.jsonl holds (case, candidate agent output, human_label) triples written by hand,
deliberately including tricky pairs: equivalent SQL with different columns/order, right-looking answers
with the wrong metric, over-refusals, and alternative readings of an ambiguous question.

Reported: agreement, Cohen's kappa, false-accept rate (judge says correct, human says incorrect; the
dangerous direction for a gate) and false-reject rate. A judge should only decide gated cases if kappa and
false-accept rate clear the bar (defaults: kappa >= 0.70, false-accept <= 10%).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, graders
from .agent import parse_output
from .cases import load_cases
from .db import ROOT
from .providers import get_provider

LABELS = ROOT / "evals" / "judge_labels.jsonl"
OUT_DIR = ROOT / "evals" / "calibration"


def cohen_kappa(pairs):
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    pe = sum((sum(a == v for a, _ in pairs) / n) * (sum(b == v for _, b in pairs) / n) for v in ("correct", "incorrect"))
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def calibrate(judge_cfg, labels_path=LABELS, min_kappa=0.70, max_false_accept=0.10, save=True) -> dict:
    cases = {c.id: c for c in load_cases()}
    provider = get_provider(judge_cfg)
    rows = []
    for line in Path(labels_path).read_text().splitlines():
        if not line.strip():
            continue
        lab = json.loads(line)
        case = cases[lab["case_id"]]
        kind, sql = parse_output(lab["candidate_output"])
        pred_prev = err = None
        if kind == "sql":
            r = db.execute(sql)
            err = r.error
            pred_prev = None if err else graders.preview(r.rows)
        gold_prev = graders.preview(db.execute(case.gold_sql).rows) if case.gold_sql else None
        msgs = graders.build_judge_messages(case.__dict__, lab["candidate_output"], pred_prev, err, gold_prev)
        c = provider.complete(msgs, model=judge_cfg["model"], temperature=judge_cfg["temperature"],
                              seed=judge_cfg["seed"], max_tokens=judge_cfg["max_tokens"])
        v = graders.parse_verdict(c.text)["verdict"]
        rows.append({"id": lab["id"], "case_id": lab["case_id"], "human": lab["human_label"], "judge": v, "note": lab.get("note", "")})
    valid = [r for r in rows if r["judge"] != "invalid"]
    pairs = [(r["human"], r["judge"]) for r in valid]
    neg = [r for r in valid if r["human"] == "incorrect"]
    pos = [r for r in valid if r["human"] == "correct"]
    res = {
        "judge_config": {k: judge_cfg[k] for k in ("name", "model", "config_hash")},
        "n_labels": len(rows), "n_invalid": len(rows) - len(valid),
        "agreement": sum(a == b for a, b in pairs) / len(pairs), "kappa": cohen_kappa(pairs),
        "false_accept_rate": sum(r["judge"] == "correct" for r in neg) / len(neg) if neg else 0.0,
        "false_reject_rate": sum(r["judge"] == "incorrect" for r in pos) / len(pos) if pos else 0.0,
        "disagreements": [r for r in valid if r["human"] != r["judge"]],
        "thresholds": {"min_kappa": min_kappa, "max_false_accept": max_false_accept},
    }
    res["trusted_for_gating"] = res["kappa"] >= min_kappa and res["false_accept_rate"] <= max_false_accept
    if save:
        OUT_DIR.mkdir(exist_ok=True, parents=True)
        (OUT_DIR / f"{judge_cfg['name']}.json").write_text(json.dumps(res, indent=2) + "\n")
    return res
