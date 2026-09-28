"""The system under test: a small natural-language-to-SQL agent.

Deliberately simple (one model call). The prompt, model and settings all live in a
versioned config; nothing about behaviour is hard-coded here.
Output contract: either a single SQL statement, or `REFUSE: <reason>`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .providers import Completion


@dataclass
class AgentOutput:
    kind: str            # "sql" | "refuse"
    sql: str | None
    raw: str
    completion: Completion
    messages: list


def build_messages(cfg: dict, schema: str, question: str) -> list:
    system = cfg["prompt"].replace("{schema}", schema)
    return [{"role": "system", "content": system}, {"role": "user", "content": f"Question: {question}"}]


def parse_output(text: str) -> tuple[str, str | None]:
    t = text.strip()
    fence = re.search(r"```(?:sql)?\s*(.*?)```", t, re.S | re.I)
    if fence:
        t = fence.group(1).strip()
    if re.match(r"^REFUSE\b", t, re.I):
        return "refuse", None
    return "sql", t.rstrip(";").strip()


def run_agent(cfg: dict, provider, schema: str, question: str) -> AgentOutput:
    messages = build_messages(cfg, schema, question)
    comp = provider.complete(messages, model=cfg["model"], temperature=cfg["temperature"],
                             seed=cfg["seed"], max_tokens=cfg["max_tokens"])
    kind, sql = parse_output(comp.text)
    return AgentOutput(kind, sql, comp.text, comp, messages)
