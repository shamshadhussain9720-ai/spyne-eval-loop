"""Eval case store: JSONL, one case per line, append-only in practice.

Fields: id, category, difficulty, question, gold_sql (null for refusals), expect (answer|refuse),
grading (exec|judge), order_matters, tags, notes, source (hand|promoted), origin (for promoted).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .db import ROOT

DEFAULT_CASES = ROOT / "evals" / "cases.jsonl"


@dataclass
class Case:
    id: str
    category: str
    difficulty: str
    question: str
    gold_sql: str | None
    expect: str = "answer"
    grading: str = "exec"
    order_matters: bool = False
    tags: list = field(default_factory=list)
    notes: str = ""
    source: str = "hand"
    origin: dict | None = None


def load_cases(path=DEFAULT_CASES) -> list[Case]:
    path = Path(path)
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(Case(**{k: v for k, v in d.items() if k in Case.__dataclass_fields__}))
    return out


def cases_hash(cases: list[Case]) -> str:
    blob = json.dumps([c.__dict__ for c in cases], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def validate_case(c: Case) -> list[str]:
    errs = []
    if c.expect not in ("answer", "refuse"):
        errs.append(f"{c.id}: expect must be answer|refuse")
    if c.grading not in ("exec", "judge"):
        errs.append(f"{c.id}: grading must be exec|judge")
    if c.expect == "answer":
        if not c.gold_sql:
            errs.append(f"{c.id}: answer case needs gold_sql")
        else:
            r = db.execute(c.gold_sql)
            if not r.ok:
                errs.append(f"{c.id}: gold_sql fails: {r.error}")
            elif not r.rows:
                errs.append(f"{c.id}: gold_sql returns no rows (a case that cannot discriminate)")
    return errs


def validate_all(cases: list[Case]) -> list[str]:
    errs = [e for c in cases for e in validate_case(c)]
    ids = [c.id for c in cases]
    errs += [f"duplicate id {i}" for i in set(ids) if ids.count(i) > 1]
    return errs


def append_case(case: Case, path=DEFAULT_CASES) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(case.__dict__) + "\n")
