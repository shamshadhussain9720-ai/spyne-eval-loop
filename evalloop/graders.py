"""Graders.

1. Execution accuracy (deterministic, primary): run gold and predicted SQL, compare RESULTS.
   - floats rounded to 2 dp; None preserved
   - row order ignored unless the case says `order_matters`
   - column-tolerant: extra columns and different column order are fine as long as every gold
     column can be matched to a distinct predicted column and the projected rows are identical
2. Refusal check (deterministic): for `expect: refuse` cases the agent must emit REFUSE.
3. Model judge (LLM, secondary): decides `grading: judge` cases (ambiguous questions) and is
   compared with the execution grader everywhere else. Its agreement with HUMAN labels is
   measured by `evalloop calibrate`; a judge that has not been calibrated should not gate.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from itertools import permutations

from . import db

PREVIEW_ROWS = 8


def _norm(v):
    if isinstance(v, float):
        return round(v, 2)
    return v


def norm_rows(rows) -> list[tuple]:
    return [tuple(_norm(v) for v in r) for r in rows]


def rows_match(gold, pred, ordered: bool) -> bool:
    """Column-tolerant result comparison. `gold`/`pred` are lists of tuples (already normalised)."""
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    gcols = list(zip(*gold))
    pcols = list(zip(*pred))
    key = (lambda col: list(col)) if ordered else (lambda col: sorted(map(repr, col)))
    cand = [[j for j, pc in enumerate(pcols) if key(gc) == key(pc)] for gc in gcols]
    if any(not c for c in cand):
        return False
    for combo in permutations(range(len(pcols)), len(gcols)):
        if all(combo[i] in cand[i] for i in range(len(gcols))):
            projected = [tuple(row[j] for j in combo) for row in pred]
            if ordered:
                if projected == gold:
                    return True
            elif Counter(map(repr, projected)) == Counter(map(repr, gold)):
                return True
    return False


def preview(rows, n=PREVIEW_ROWS) -> str:
    rows = norm_rows(rows)
    s = json.dumps(rows[:n], default=str, ensure_ascii=False)
    return s + (f" ... (+{len(rows) - n} more rows, {len(rows)} total)" if len(rows) > n else "")


def exec_grade(case, pred_sql: str | None, gold_res: db.ExecResult) -> dict:
    """Returns {exec_pass, reason, pred_rows_n, pred_preview, error}."""
    if pred_sql is None:
        return {"exec_pass": False, "reason": "no_sql", "pred_rows_n": None, "pred_preview": None, "error": None}
    res = db.execute(pred_sql)
    if not res.ok:
        return {"exec_pass": False, "reason": "sql_error", "pred_rows_n": None, "pred_preview": None, "error": res.error}
    ok = rows_match(norm_rows(gold_res.rows), norm_rows(res.rows), case.order_matters)
    return {"exec_pass": ok, "reason": "match" if ok else "wrong_result", "pred_rows_n": len(res.rows),
            "pred_preview": preview(res.rows), "error": None}


# ---------------------------------------------------------------- model judge
JUDGE_SYSTEM = """You are a strict JUDGE for a text-to-SQL agent on the Chinook music-store SQLite database.
You get the QUESTION, the EXPECTED_BEHAVIOR (answer or refuse), a REFERENCE_SQL with its result, and what the
agent produced (AGENT_OUTPUT and the result of running it).
Rules:
- EXPECTED_BEHAVIOR=refuse: correct only if the agent refused (output starts with REFUSE). Any SQL is incorrect.
- EXPECTED_BEHAVIOR=answer: refusing is incorrect. The agent is correct if its result answers the question
  in a reasonable reading. Different aliases, column order, extra helpful columns, or equivalent SQL are fine.
  A different set of rows, wrong aggregation, wrong units, wrong ordering when order was requested, or a SQL error is incorrect.
- If the question is ambiguous, accept any reasonable interpretation, not only the reference.
Reply with JSON only: {"verdict": "correct" | "incorrect", "reason": "<one short sentence>"}"""


def build_judge_messages(case_like: dict, agent_output: str, pred_preview: str | None, error: str | None,
                         gold_preview: str | None) -> list:
    user = (
        f"QUESTION: {case_like['question']}\n"
        f"EXPECTED_BEHAVIOR: {case_like['expect']}\n"
        f"REFERENCE_SQL: {case_like.get('gold_sql') or '(none, should refuse)'}\n"
        f"REFERENCE_RESULT: {gold_preview or '(none)'}\n"
        f"AGENT_OUTPUT: {agent_output.strip()}\n"
        f"AGENT_RESULT: {('ERROR ' + error) if error else (pred_preview or '(none)')}\n"
    )
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_verdict(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0))
        v = str(d.get("verdict", "")).lower()
        if v in ("correct", "incorrect"):
            return {"verdict": v, "reason": str(d.get("reason", ""))[:200]}
    except (AttributeError, json.JSONDecodeError):
        pass
    return {"verdict": "invalid", "reason": f"unparseable judge output: {text[:80]!r}"}
