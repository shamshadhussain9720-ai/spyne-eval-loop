"""Failure -> permanent test.

A production failure (thumbs-down, support ticket, trace sampled from logs) is logged as a small
JSON file (see failures/prod-001.json). A human supplies the CORRECT SQL. `promote` then:

  1. validates the gold SQL: it must execute and return rows (a case that cannot discriminate is rejected);
  2. rejects duplicates (same normalised question already in the suite);
  3. assigns a stable id (prom-NNN), records provenance (`source: promoted`, `origin`);
  4. VERIFIES THE CASE REPRODUCES THE FAILURE against the failing config: a promoted case that the
     failing config already passes proves nothing, so it is refused unless --force;
  5. appends it to evals/cases.jsonl. From then on every CI run guards against that failure.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .cases import Case, DEFAULT_CASES, append_case, load_cases, validate_case


def _norm_q(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()


class PromoteError(Exception):
    pass


def promote(failure_path, gold_sql, category, *, difficulty="medium", expect="answer", grading="exec",
            tags=None, order_matters=False, verify_cfg=None, judge_cfg=None, cases_path=DEFAULT_CASES,
            force=False, dry_run=False) -> Case:
    failure = json.loads(Path(failure_path).read_text())
    question = failure["question"].strip()
    existing = load_cases(cases_path)
    if any(_norm_q(c.question) == _norm_q(question) for c in existing):
        raise PromoteError("duplicate: this question is already covered by the eval set")
    nums = [int(m.group(1)) for c in existing if (m := re.fullmatch(r"prom-(\d+)", c.id))]
    case = Case(id=f"prom-{max(nums, default=0) + 1:03d}", category=category, difficulty=difficulty, question=question,
                gold_sql=gold_sql if expect == "answer" else None, expect=expect, grading=grading,
                order_matters=order_matters, tags=list(tags or []) + ([] if expect == "answer" else ["refusal", "critical"]),
                notes=failure.get("note", ""), source="promoted",
                origin={"failure_file": str(failure_path), "bad_agent_output": failure.get("agent_output"),
                        "reported_by": failure.get("reported_by"), "promoted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    errs = validate_case(case)
    if errs:
        raise PromoteError("; ".join(errs))
    if verify_cfg is not None and not force:
        from .runner import run_suite
        r = run_suite(verify_cfg, judge_cfg, [case], use_cache=False, save=False)["results"][0]
        if r.get("error"):
            raise PromoteError(f"verification run errored: {r['error']}")
        if r["passed"]:
            raise PromoteError(f"case does NOT reproduce: config '{verify_cfg['name']}' already passes it "
                               f"(output: {r['pred_sql'] or r['raw_output']!r}). Not adding a test that cannot fail. Use --force to override.")
    if not dry_run:
        append_case(case, cases_path)
    return case
