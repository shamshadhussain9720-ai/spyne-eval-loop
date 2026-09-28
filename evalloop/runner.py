"""Concurrent eval runner.

Design points
  * asyncio + semaphore for bounded concurrency; provider calls run in worker threads and
    retry 429/5xx with exponential backoff inside the provider.
  * Disk cache keyed by hash(config_hash, case, schema): re-running an unchanged config on an
    unchanged case costs nothing. Latency/tokens of the original call are replayed from cache.
    `--no-cache` (used by the scheduled CI job) forces fresh calls to catch provider-side drift.
  * Every case records: agent output, execution result preview, grades, latency, tokens, cost, trace.
  * Provider/infrastructure errors are recorded as `error` and excluded from pass-rate comparisons;
    they are gated separately so a flaky API cannot masquerade as a regression.
  * Judge calls can be sampled (`judge_sample_rate`) so 10k-case runs do not pay for 10k judge calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, graders
from .agent import run_agent
from .cases import Case, cases_hash
from .config import cost_usd
from .db import ROOT
from .providers import ProviderError, get_provider
from .store import save_run

CACHE_DIR = ROOT / ".cache"


class Cache:
    def __init__(self, enabled=True, root=CACHE_DIR):
        self.enabled, self.root = enabled, Path(root)
        if enabled:
            self.root.mkdir(exist_ok=True)

    def get(self, key):
        p = self.root / f"{key}.json"
        return json.loads(p.read_text()) if self.enabled and p.exists() else None

    def put(self, key, value):
        if self.enabled:
            tmp = self.root / f"{key}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(value))
            tmp.replace(self.root / f"{key}.json")  # atomic


def _h(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


def _judge_selected(case: Case, rate: float) -> bool:
    if case.grading == "judge" or rate >= 1.0:
        return True
    return int(hashlib.sha256(case.id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def process_case(case: Case, ctx: dict) -> dict:
    a_cfg, j_cfg, cache = ctx["agent_cfg"], ctx["judge_cfg"], ctx["cache"]
    rec = {"case_id": case.id, "category": case.category, "difficulty": case.difficulty, "expect": case.expect,
           "grading": case.grading, "tags": case.tags, "question": case.question, "gold_sql": case.gold_sql,
           "source": case.source}
    # ---- agent call (cached)
    key = _h("agent", a_cfg["config_hash"], case.id, case.question, ctx["schema_sha"])
    out = cache.get(key)
    rec["cached"] = out is not None
    if out is None:
        try:
            ao = run_agent(a_cfg, ctx["agent_provider"], ctx["schema"], case.question)
        except ProviderError as e:
            rec.update(error=f"agent: {e}", passed=False, failure_type="infra_error")
            return rec
        out = {"kind": ao.kind, "sql": ao.sql, "raw": ao.raw, "prompt_tokens": ao.completion.prompt_tokens,
               "completion_tokens": ao.completion.completion_tokens, "latency_s": ao.completion.latency_s}
        cache.put(key, out)
    rec.update(output_kind=out["kind"], pred_sql=out["sql"], raw_output=out["raw"], latency_s=out["latency_s"],
               prompt_tokens=out["prompt_tokens"], completion_tokens=out["completion_tokens"])
    rec["cost_usd"] = cost_usd(a_cfg, out["prompt_tokens"], out["completion_tokens"])

    # ---- deterministic grading
    gold_res = ctx["gold"].get(case.id)
    ex = {"exec_pass": None, "reason": None, "pred_rows_n": None, "pred_preview": None, "error": None}
    if case.expect == "answer":
        ex = graders.exec_grade(case, out["sql"], gold_res) if out["kind"] == "sql" else \
            {**ex, "exec_pass": False, "reason": "over_refusal"}
    rec["exec"] = ex

    # ---- judge (only when needed / sampled)
    rec["judge"] = None
    rec["judge_cost_usd"] = 0.0
    if _judge_selected(case, ctx["judge_sample_rate"]):
        gold_prev = graders.preview(gold_res.rows) if gold_res else None
        msgs = graders.build_judge_messages(case.__dict__, out["raw"], ex.get("pred_preview"), ex.get("error"), gold_prev)
        jkey = _h("judge", j_cfg["config_hash"], json.dumps(msgs))
        jo = cache.get(jkey)
        if jo is None:
            try:
                c = ctx["judge_provider"].complete(msgs, model=j_cfg["model"], temperature=j_cfg["temperature"],
                                                   seed=j_cfg["seed"], max_tokens=j_cfg["max_tokens"])
                jo = {"text": c.text, "pt": c.prompt_tokens, "ct": c.completion_tokens}
                cache.put(jkey, jo)
            except ProviderError as e:
                jo = {"text": "", "pt": 0, "ct": 0, "error": str(e)}
        rec["judge"] = {**graders.parse_verdict(jo["text"]), **({"error": jo["error"]} if "error" in jo else {})}
        rec["judge_cost_usd"] = cost_usd(j_cfg, jo["pt"], jo["ct"])

    # ---- final verdict
    if case.expect == "refuse":
        rec["passed"] = out["kind"] == "refuse"
        rec["failure_type"] = None if rec["passed"] else "should_have_refused"
    elif case.grading == "judge":
        v = (rec["judge"] or {}).get("verdict")
        if v not in ("correct", "incorrect"):
            rec.update(error="judge produced no valid verdict", passed=False, failure_type="infra_error")
            return rec
        rec["passed"] = v == "correct"
        rec["failure_type"] = None if rec["passed"] else ("over_refusal" if out["kind"] == "refuse" else "judge_incorrect")
    else:
        rec["passed"] = bool(ex["exec_pass"])
        rec["failure_type"] = None if rec["passed"] else ex["reason"]
    # judge-vs-exec disagreement is a review signal, not a verdict
    j = rec["judge"]
    rec["judge_agrees_with_exec"] = (None if not j or j["verdict"] == "invalid" or case.grading != "exec"
                                     or case.expect != "answer" else (j["verdict"] == "correct") == rec["passed"])
    return rec


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(-(-p / 100 * len(xs) // 1)) - 1))]


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if not r.get("error")]
    def rate(rs):
        return {"n": len(rs), "passed": sum(r["passed"] for r in rs),
                "rate": (sum(r["passed"] for r in rs) / len(rs)) if rs else None}
    groups = {}
    for key in ("category", "difficulty"):
        groups[key] = {v: rate([r for r in ok if r[key] == v]) for v in sorted({r[key] for r in ok})}
    lat = [r["latency_s"] for r in ok]
    agree = [r["judge_agrees_with_exec"] for r in ok if r.get("judge_agrees_with_exec") is not None]
    agent_cost = sum(r.get("cost_usd", 0) for r in ok)
    judge_cost = sum(r.get("judge_cost_usd", 0) for r in ok)
    return {
        "n_cases": len(results), "n_errors": len(results) - len(ok), "overall": rate(ok),
        "by_category": groups["category"], "by_difficulty": groups["difficulty"],
        "latency_s": {"mean": statistics.fmean(lat) if lat else None, "p50": _pct(lat, 50), "p95": _pct(lat, 95)},
        "tokens": {"prompt": sum(r.get("prompt_tokens", 0) for r in ok), "completion": sum(r.get("completion_tokens", 0) for r in ok)},
        "cost_usd": {"agent": agent_cost, "judge": judge_cost, "total": agent_cost + judge_cost,
                     "agent_per_case": agent_cost / len(ok) if ok else None},
        "judge_exec_agreement": {"n": len(agree), "rate": (sum(agree) / len(agree)) if agree else None},
    }


def _git_sha() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:8]
    try:
        return subprocess.check_output(["git", "rev-parse", "--short=8", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return "nogit"


async def _run_async(cases, ctx, concurrency):
    sem = asyncio.Semaphore(concurrency)
    async def one(c):
        async with sem:
            return await asyncio.to_thread(process_case, c, ctx)
    return await asyncio.gather(*(one(c) for c in cases))


def run_suite(agent_cfg, judge_cfg, cases: list[Case], *, concurrency=8, use_cache=True, label=None,
              judge_sample_rate=1.0, runs_dir=None, save=True, cache_dir=None) -> dict:
    t0 = time.perf_counter()
    schema = db.schema_ddl()
    ctx = {
        "agent_cfg": agent_cfg, "judge_cfg": judge_cfg, "cache": Cache(use_cache, cache_dir or CACHE_DIR),
        "agent_provider": get_provider(agent_cfg), "judge_provider": get_provider(judge_cfg),
        "schema": schema, "schema_sha": hashlib.sha256(schema.encode()).hexdigest()[:12],
        "judge_sample_rate": judge_sample_rate,
        "gold": {c.id: db.execute(c.gold_sql) for c in cases if c.gold_sql},
    }
    results = list(asyncio.run(_run_async(cases, ctx, concurrency)))
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y%m%dT%H%M%SZ}_{label or agent_cfg['name']}"
    summary = {
        "run_id": run_id, "label": label, "created_utc": now.isoformat(timespec="seconds"), "git_sha": _git_sha(),
        "agent_config": {k: v for k, v in agent_cfg.items() if k != "prompt"},
        "judge_config": {k: v for k, v in judge_cfg.items() if k != "prompt"},
        "cases_sha": cases_hash(cases), "cache_used": use_cache, "concurrency": concurrency,
        "judge_sample_rate": judge_sample_rate, "wall_time_s": round(time.perf_counter() - t0, 2),
        **summarize(results),
    }
    if save:
        from .store import RUNS_DIR
        save_run(summary, results, agent_cfg.get("prompt", ""), runs_dir or RUNS_DIR)
    return {"summary": summary, "results": results}
