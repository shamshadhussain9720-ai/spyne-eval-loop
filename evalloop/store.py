"""Run storage: one directory per run.

  runs/<run_id>/summary.json   aggregate metrics + full resolved config (small, always loaded)
  runs/<run_id>/results.jsonl  one line per case with trace (streamable; 10k cases ~ tens of MB)
  runs/BASELINE                pointer (a run id) to the blessed baseline, changed only by a human

At real scale the same layout maps onto object storage + a Postgres index of summaries.
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import ROOT

RUNS_DIR = ROOT / "runs"


def save_run(summary: dict, results: list[dict], system_prompt: str, runs_dir: Path = RUNS_DIR) -> Path:
    d = runs_dir / summary["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (d / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")
    (d / "system_prompt.txt").write_text(system_prompt)
    return d


def list_runs(runs_dir: Path = RUNS_DIR) -> list[str]:
    return sorted(p.name for p in runs_dir.iterdir() if (p / "summary.json").exists()) if runs_dir.exists() else []


def resolve(ref: str, runs_dir: Path = RUNS_DIR) -> str:
    if ref == "baseline":
        ptr = runs_dir / "BASELINE"
        if not ptr.exists():
            raise FileNotFoundError("no runs/BASELINE pointer; run `evalloop baseline <run_id>`")
        return ptr.read_text().strip()
    if ref == "latest":
        runs = list_runs(runs_dir)
        if not runs:
            raise FileNotFoundError("no runs yet")
        return runs[-1]
    return Path(ref).name if (runs_dir / Path(ref).name).exists() else ref


def load_run(ref: str, runs_dir: Path = RUNS_DIR) -> dict:
    rid = resolve(ref, runs_dir)
    d = runs_dir / rid
    if not d.exists():
        raise FileNotFoundError(f"run {rid} not found in {runs_dir}")
    summary = json.loads((d / "summary.json").read_text())
    results = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines() if l.strip()]
    return {"summary": summary, "results": results}


def set_baseline(run_id: str, runs_dir: Path = RUNS_DIR) -> None:
    if not (runs_dir / run_id / "summary.json").exists():
        raise FileNotFoundError(run_id)
    (runs_dir / "BASELINE").write_text(run_id + "\n")
