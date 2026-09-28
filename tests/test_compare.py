import json
from pathlib import Path

import pytest

from evalloop import compare, store

THR = compare.load_thresholds()


def mk(passes: dict, *, tags=None, cats=None, errors=(), cases_sha="x", cost=0.001, p95=1.0):
    tags, cats = tags or {}, cats or {}
    results = [{"case_id": i, "category": cats.get(i, "c"), "difficulty": "easy", "tags": tags.get(i, []), "question": i,
                "passed": p, "failure_type": None if p else "wrong_result", "pred_sql": "-", "error": "boom" if i in errors else None}
               for i, p in passes.items()]
    n_err = len(errors)
    ok = [r for r in results if not r["error"]]
    return {"summary": {"run_id": "r", "cases_sha": cases_sha, "agent_config": {"config_hash": "h", "name": "n"}, "n_cases": len(results), "n_errors": n_err,
                        "overall": {"n": len(ok), "passed": sum(r["passed"] for r in ok), "rate": sum(r["passed"] for r in ok) / len(ok)},
                        "cost_usd": {"agent_per_case": cost}, "latency_s": {"p95": p95}}, "results": results}


def ids(n, fail=()):
    return {f"c{i}": (f"c{i}" not in fail) for i in range(n)}


def test_identical_runs_pass():
    r = mk(ids(20))
    assert compare.gate(compare.diff_runs(r, r), r, THR)["status"] == "pass"


def test_pass_rate_drop_fails():
    b, c = mk(ids(20)), mk(ids(20, fail={"c1", "c2"}))
    g = compare.gate(compare.diff_runs(b, c), c, THR)
    assert g["status"] == "fail" and any("pass rate" in f for f in g["failures"])


def test_single_flake_within_tolerance_on_large_suite_passes():
    b, c = mk(ids(100)), mk(ids(100, fail={"c1"}))
    assert compare.gate(compare.diff_runs(b, c), c, THR)["status"] == "pass"   # 1 pt drop < 5


def test_critical_case_regression_fails_even_when_pass_rate_is_fine():
    tags = {"c0": ["critical"]}
    b, c = mk(ids(100), tags=tags), mk(ids(100, fail={"c0"}), tags=tags)
    g = compare.gate(compare.diff_runs(b, c), c, THR)
    assert g["status"] == "fail" and any("critical" in f for f in g["failures"])


def test_new_cases_do_not_count_as_regressions():
    b = mk(ids(10))
    c = mk({**ids(10), "new1": False, "new2": False}, cases_sha="y")
    d = compare.diff_runs(b, c)
    assert d["regressions"] == [] and d["new_cases"] == ["new1", "new2"]


def test_errored_cases_are_excluded_and_high_error_rate_fails():
    b = mk(ids(20))
    c = mk(ids(20, fail={"c0", "c1", "c2"}), errors={"c0", "c1", "c2"})
    d = compare.diff_runs(b, c)
    assert d["regressions"] == []                      # provider errors are not regressions...
    assert compare.gate(d, c, THR)["status"] == "fail" # ...but 15% errors invalidates the run


def test_category_drop_fails():
    cats = {f"c{i}": ("join" if i < 4 else "other") for i in range(40)}
    b, c = mk(ids(40), cats=cats), mk(ids(40, fail={"c0", "c1"}), cats=cats)   # join 100% -> 50%
    g = compare.gate(compare.diff_runs(b, c), c, THR)
    assert any("category 'join'" in f for f in g["failures"])


def test_cost_increase_only_warns():
    b, c = mk(ids(20), cost=0.001), mk(ids(20), cost=0.002)
    g = compare.gate(compare.diff_runs(b, c), c, THR)
    assert g["status"] == "pass" and any("cost" in w for w in g["warnings"])


def test_mcnemar():
    assert compare.mcnemar_exact_p(0, 0) == 1.0
    assert compare.mcnemar_exact_p(5, 0) == pytest.approx(0.0625)
    assert compare.mcnemar_exact_p(10, 0) < 0.01


def test_committed_runs_show_the_regression():
    if not (store.RUNS_DIR / "BASELINE").exists():
        pytest.skip("no committed runs")
    base = store.load_run("baseline")
    change = [i for i in store.list_runs() if i.endswith("_change")]
    assert change, "expected a committed run labelled 'change'"
    cand = store.load_run(change[-1])
    g = compare.gate(compare.diff_runs(base, cand), cand, THR)
    assert g["status"] == "fail"
