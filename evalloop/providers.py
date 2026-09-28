"""Model providers, swappable through config.

  provider: {type: openai_compat, base_url: ..., api_key_env: ...}   # Groq, OpenAI, OpenRouter, Ollama, vLLM...
  provider: {type: mock, profile: strong|weak}                        # offline, deterministic (tests/CI demo)

Adding a provider = one class with `complete(messages, model, temperature, seed, max_tokens)`.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .db import ROOT


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float


class ProviderError(RuntimeError):
    pass


class OpenAICompat:
    RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

    def __init__(self, base_url: str, api_key_env: str, max_retries: int = 6, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = os.environ.get(api_key_env)
        if not self.api_key:
            raise ProviderError(f"env var {api_key_env} is not set")
        self.max_retries, self.timeout_s = max_retries, timeout_s

    def complete(self, messages, *, model, temperature, seed, max_tokens) -> Completion:
        body = {"model": model, "messages": messages, "temperature": temperature,
                "seed": seed, "max_tokens": max_tokens}
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "evalloop/0.1"}
        last = None
        for attempt in range(self.max_retries):
            t0 = time.perf_counter()
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout_s)
            except httpx.TransportError as e:
                last = f"transport: {e}"
            else:
                if r.status_code == 200:
                    d = r.json()
                    u = d.get("usage", {})
                    return Completion(d["choices"][0]["message"]["content"] or "",
                                      u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                                      time.perf_counter() - t0)
                last = f"http {r.status_code}: {r.text[:200]}"
                if r.status_code not in self.RETRY_STATUS:
                    raise ProviderError(last)
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.replace(".", "").isdigit():
                    time.sleep(min(float(retry_after), 30))
                    continue
            time.sleep(min(2 ** attempt, 30) * (0.5 + random.random() / 2))  # exponential backoff + jitter
        raise ProviderError(f"gave up after {self.max_retries} attempts: {last}")


class Mock:
    """Deterministic offline stand-in for an LLM. Used ONLY to test the loop itself.

    Agent calls: looks up a canned answer for the question (`strong` or `weak` profile,
    see evals/mock_answers.json). Judge calls: a deliberately naive heuristic judge
    (exact result-preview match) so judge calibration has something real to measure.
    Token counts are estimated (chars/4) and latency is synthetic. Neither says anything
    about a real model.
    """

    def __init__(self, profile: str = "strong", answers_path: str = "evals/mock_answers.json"):
        self.profile = profile
        self.answers = json.loads((ROOT / answers_path).read_text())

    def complete(self, messages, *, model, temperature, seed, max_tokens) -> Completion:
        system, user = messages[0]["content"], messages[-1]["content"]
        if "JUDGE" in system:
            text = self._judge(user)
        else:
            q = re.search(r"Question:\s*(.*)", user).group(1).strip()
            if q not in self.answers:
                text = "REFUSE: I cannot answer this question."
            else:
                text = self.answers[q][self.profile]
        pt = sum(len(m["content"]) for m in messages) // 4
        ct = max(1, len(text) // 4)
        jitter = int(hashlib.sha256((user + str(seed)).encode()).hexdigest()[:4], 16) / 65535 * 0.15
        return Completion(text, pt, ct, 0.25 + 0.0003 * pt + 0.004 * ct + jitter)

    @staticmethod
    def _judge(user: str) -> str:
        def field(name):
            m = re.search(rf"^{name}:\s*(.*?)(?=^\w+:|\Z)", user, re.S | re.M)
            return m.group(1).strip() if m else ""
        expected, out = field("EXPECTED_BEHAVIOR"), field("AGENT_OUTPUT")
        refused = out.upper().startswith("REFUSE")
        if expected == "refuse":
            ok = refused
        elif refused:
            ok = False
        else:
            ok = field("AGENT_RESULT") == field("REFERENCE_RESULT")
        return json.dumps({"verdict": "correct" if ok else "incorrect", "reason": "mock heuristic judge"})


def get_provider(cfg: dict):
    p = cfg["provider"]
    if p["type"] == "openai_compat":
        return OpenAICompat(p["base_url"], p["api_key_env"])
    if p["type"] == "mock":
        return Mock(p.get("profile", "strong"))
    raise ValueError(f"unknown provider type {p['type']}")
