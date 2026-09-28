"""End-to-end with the offline mock provider. The key test: the suite DISCRIMINATES.
A degraded config must fail in the places a real regression would."""
import pytest

from evalloop import compare, runner, store
from evalloop.cases import load_cases
from evalloop.config import load_config

JUDGE = "configs/judge_mock.yaml"


def run(cfg, tmp_path, **kw):
    return runner.run_suite(load_config(cfg), load_config(JUDGE), load_cases(), runs_dir=tmp_path, cache_dir=tmp_path / "c", **kw)


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    d = tmp_path_factory.mktemp("runs")
    return run("configs/mock/v1.yaml", d, save=False), run("configs/mock/v2.yaml", d, save=False)


def test_suite_discriminates_good_vs_degraded(pair):
    good, bad = pair
    failed = {r["case_id"] for r in bad["results"] if not r["passed"]}
    assert {"refuse-01", "refuse-02"} <= failed                  # both refusals caught
    assert len({r["category"] for r in bad["results"] if not r["passed"]}) >= 3
    assert bad["summary"]["overall"]["rate"] < good["summary"]["overall"]["rate"] - 0.15
    assert all(r["passed"] for r in good["results"] if "critical" in r["tags"])


def test_gate_fails_the_degraded_config_and_passes_identical_rerun(pair):
    good, bad = pair
    thr = compare.load_thresholds()
    d = compare.diff_runs(good, bad)
    g = compare.gate(d, bad, thr)
    assert g["status"] == "fail" and any("critical" in f for f in g["failures"])
    assert compare.gate(compare.diff_runs(good, good), good, thr)["status"] == "pass"


def test_cost_is_lower_for_lean_prompt_even_though_quality_regressed(pair):
    good, bad = pair
    assert bad["summary"]["cost_usd"]["agent"] < good["summary"]["cost_usd"]["agent"]


def test_cache_replays_identically_and_marks_cached(tmp_path):
    a = run("configs/mock/v1.yaml", tmp_path, save=False)
    b = run("configs/mock/v1.yaml", tmp_path, save=False)
    assert not any(r["cached"] for r in a["results"]) and all(r["cached"] for r in b["results"])
    assert [r["passed"] for r in a["results"]] == [r["passed"] for r in b["results"]]


def test_run_is_saved_and_reloadable(tmp_path):
    r = run("configs/mock/v1.yaml", tmp_path)
    loaded = store.load_run(r["summary"]["run_id"], tmp_path)
    assert loaded["summary"]["overall"] == r["summary"]["overall"] and len(loaded["results"]) == len(r["results"])
