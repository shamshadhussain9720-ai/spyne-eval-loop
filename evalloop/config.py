"""Versioned agent/judge configs. A config = a YAML file + the prompt file it points to.

`config_hash` covers the *resolved* config including prompt text, so editing a prompt
file changes the hash even if the YAML is untouched. Runs record this hash, so any
result can be traced to the exact prompt/model/settings that produced it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from .db import ROOT

REQUIRED = ["name", "provider", "model", "temperature", "seed", "max_tokens"]


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_absolute() and not path.exists():
        path = ROOT / path
    cfg = yaml.safe_load(path.read_text())
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"{path}: missing config keys {missing}")
    if "prompt_file" in cfg:
        cfg["prompt"] = (ROOT / cfg["prompt_file"]).read_text()
    cfg.setdefault("pricing", {"input_per_mtok": 0.0, "output_per_mtok": 0.0})
    cfg["config_path"] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    cfg["config_hash"] = config_hash(cfg)
    return cfg


def config_hash(cfg: dict) -> str:
    material = {k: v for k, v in cfg.items() if k not in ("config_path", "config_hash", "pricing")}
    blob = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def cost_usd(cfg: dict, prompt_tokens: int, completion_tokens: int) -> float:
    p = cfg.get("pricing", {})
    return (prompt_tokens * p.get("input_per_mtok", 0) + completion_tokens * p.get("output_per_mtok", 0)) / 1e6
