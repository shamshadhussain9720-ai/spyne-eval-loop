from __future__ import annotations

import argparse
import json
import sys

from . import calibrate as cal, compare as cmp, promote as prom, report as rep, store
from .cases import DEFAULT_CASES, load_cases, validate_all
from .config import load_config
from .runner import run_suite


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="evalloop")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run an agent config over the eval set and store the run")
    r.add_argument("--config", required=True)
    r.add_argument("--judge", default="configs/judge_mock.yaml")
    r.add_argument("--cases", default=str(DEFAULT_CASES))
    r.add_argument("--concurrency", type=int, default=8)
    r.add_argument("--no-cache", action="store_true", help="force fresh model calls (scheduled runs)")
    r.add_argument("--label")
    r.add_argument("--judge-sample-rate", type=float, default=1.0, help="fraction of exec-graded cases also sent to the judge")
    r.add_argument("--category", help="only run this category (quick local iteration; never gate on a partial run)")

    c = sub.add_parser("compare", help="diff a candidate run against a baseline and apply the gate (exit 1 on fail)")
    c.add_argument("--base", default="baseline")
    c.add_argument("--cand", default="latest")
    c.add_argument("--thresholds")
    c.add_argument("--json", help="write diff+gate JSON here")

    sub.add_parser("report").add_argument("--out", default=None)
    b = sub.add_parser("baseline", help="bless a run as the baseline")
    b.add_argument("run_id")
    sub.add_parser("validate-cases")

    p = sub.add_parser("promote", help="turn a logged failure into a permanent eval case")
    p.add_argument("--from-file", required=True)
    p.add_argument("--gold", required=True, help="the CORRECT SQL (human supplied)")
    p.add_argument("--category", required=True)
    p.add_argument("--difficulty", default="medium")
    p.add_argument("--expect", default="answer", choices=["answer", "refuse"])
    p.add_argument("--grading", default="exec", choices=["exec", "judge"])
    p.add_argument("--ordered", action="store_true")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--verify-config", help="config that failed in production; the new case must fail on it")
    p.add_argument("--judge", default="configs/judge_mock.yaml")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    k = sub.add_parser("calibrate", help="measure the judge against human labels")
    k.add_argument("--judge", default="configs/judge_mock.yaml")
    k.add_argument("--min-kappa", type=float, default=0.70)

    a = ap.parse_args(argv)

    if a.cmd == "run":
        cases = load_cases(a.cases)
        if a.category:
            cases = [x for x in cases if x.category == a.category]
        errs = validate_all(cases)
        if errs:
            print("invalid cases:\n  " + "\n  ".join(errs)); return 2
        res = run_suite(load_config(a.config), load_config(a.judge), cases, concurrency=a.concurrency,
                        use_cache=not a.no_cache, label=a.label, judge_sample_rate=a.judge_sample_rate)
        s = res["summary"]
        print(f"{s['agent_config']['name']}#{s['agent_config']['config_hash']}: {s['overall']['passed']}/{s['overall']['n']} passed "
              f"({s['overall']['rate']:.1%}), errors={s['n_errors']}, agent cost ${s['cost_usd']['agent']:.4f}, "
              f"p95 latency {s['latency_s']['p95']:.2f}s, wall {s['wall_time_s']}s")
        for x in res["results"]:
            if not x["passed"]:
                print(f"  FAIL {x['case_id']:<11} {x.get('failure_type')}")
        print(f"RUN_ID={s['run_id']}")
        return 0

    if a.cmd == "compare":
        thr = cmp.load_thresholds(a.thresholds)
        cand = store.load_run(a.cand)
        try:
            base = store.load_run(a.base)
        except FileNotFoundError:
            base = None
        d = cmp.diff_runs(base, cand) if base else None
        g = cmp.gate(d, cand, thr)
        print(cmp.render_text(d, g, cand))
        if a.json:
            json.dump({"diff": d, "gate": g}, open(a.json, "w"), indent=2)
        return 0 if g["status"] == "pass" else 1

    if a.cmd == "report":
        print(rep.build(**({"out": __import__("pathlib").Path(a.out)} if a.out else {}))); return 0
    if a.cmd == "baseline":
        store.set_baseline(a.run_id); print(f"baseline -> {a.run_id}"); return 0
    if a.cmd == "validate-cases":
        cs = load_cases(); errs = validate_all(cs)
        print(f"{len(cs)} cases", "OK" if not errs else "\n" + "\n".join(errs)); return 1 if errs else 0

    if a.cmd == "promote":
        try:
            case = prom.promote(a.from_file, a.gold, a.category, difficulty=a.difficulty, expect=a.expect, grading=a.grading,
                                tags=a.tag, order_matters=a.ordered, force=a.force, dry_run=a.dry_run,
                                verify_cfg=load_config(a.verify_config) if a.verify_config else None,
                                judge_cfg=load_config(a.judge))
        except prom.PromoteError as e:
            print(f"REJECTED: {e}"); return 1
        print(f"{'(dry run) would add' if a.dry_run else 'added'} {case.id} [{case.category}] {case.question!r}"
              + (f"\n  verified: fails on {a.verify_config}" if a.verify_config and not a.force else ""))
        return 0

    if a.cmd == "calibrate":
        res = cal.calibrate(load_config(a.judge), min_kappa=a.min_kappa)
        print(f"judge={res['judge_config']['name']} labels={res['n_labels']} agreement={res['agreement']:.0%} kappa={res['kappa']:.2f} "
              f"false_accept={res['false_accept_rate']:.0%} false_reject={res['false_reject_rate']:.0%}")
        for d in res["disagreements"]:
            print(f"  disagree {d['id']} ({d['case_id']}): human={d['human']} judge={d['judge']}  {d['note']}")
        print("TRUSTED for gating" if res["trusted_for_gating"] else "NOT trusted for gating"); return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
