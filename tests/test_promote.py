import json
import shutil

import pytest

from evalloop import promote
from evalloop.cases import load_cases
from evalloop.config import load_config
from evalloop.db import ROOT

GOLD = "SELECT ROUND(AVG(Milliseconds) / 60000.0, 2) FROM Track"
FAILURE = ROOT / "failures" / "prod-001.json"
JUDGE = load_config("configs/judge_mock.yaml")


@pytest.fixture
def cases_copy(tmp_path):
    p = tmp_path / "cases.jsonl"
    shutil.copy(ROOT / "evals" / "cases.jsonl", p)
    return p


def _drop_promoted(p):
    keep = [l for l in p.read_text().splitlines() if '"promoted"' not in l]
    p.write_text("\n".join(keep) + "\n")


def test_promote_adds_case_when_failure_reproduces(cases_copy):
    _drop_promoted(cases_copy)
    n = len(load_cases(cases_copy))
    case = promote.promote(FAILURE, GOLD, "aggregation", verify_cfg=load_config("configs/mock/v2.yaml"), judge_cfg=JUDGE, cases_path=cases_copy)
    cs = load_cases(cases_copy)
    assert len(cs) == n + 1 and cs[-1].id == case.id and cs[-1].source == "promoted" and cs[-1].origin["bad_agent_output"]


def test_promote_rejects_case_the_fixed_config_already_passes(cases_copy):
    _drop_promoted(cases_copy)
    with pytest.raises(promote.PromoteError, match="does NOT reproduce"):
        promote.promote(FAILURE, GOLD, "aggregation", verify_cfg=load_config("configs/mock/v1.yaml"), judge_cfg=JUDGE, cases_path=cases_copy)


def test_promote_rejects_duplicates_and_bad_gold(cases_copy):
    _drop_promoted(cases_copy)
    promote.promote(FAILURE, GOLD, "aggregation", cases_path=cases_copy)
    with pytest.raises(promote.PromoteError, match="duplicate"):
        promote.promote(FAILURE, GOLD, "aggregation", cases_path=cases_copy)
    _drop_promoted(cases_copy)
    with pytest.raises(promote.PromoteError, match="gold_sql fails"):
        promote.promote(FAILURE, "SELECT nope FROM nowhere", "aggregation", cases_path=cases_copy)
