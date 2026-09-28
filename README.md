# NL-to-SQL agent: continuous evaluation loop

A small text-to-SQL agent (Chinook, SQLite) and, around it, the loop this assignment is about:
**every change is run through an eval suite, graded, diffed against a baseline, gated, reported, and every
production failure can be turned into a permanent test.**

> **Read this first: what the committed runs are.** The two committed runs (`runs/*_baseline`, `runs/*_change`) were produced
> with the built-in **`mock` provider**: a deterministic, offline stand-in for an LLM (the authoring environment had no LLM API access).
> They exercise and prove the *loop* (grading, diff, gate, report, promotion). They say **nothing about how a real model performs**, and
> their latency is synthetic and token counts are estimated. Real-model configs are included (`configs/groq/`); see
> [Run it with a real model](#run-it-with-a-real-model). The architecture note says what this does and does not prove.

## Run it in ten minutes (offline, no API key)

```bash
python -m venv .venv && source .venv/bin/activate     # Python 3.11+ (built on 3.12)
pip install -r requirements.txt                       # 3 pinned deps: pyyaml, httpx, pytest
make test                                             # 28 tests, ~1s
make demo                                             # baseline run, "change" run, diff, gate (exits 1 on purpose)
python -m evalloop report && open report/index.html   # or xdg-open
```

The Chinook database is committed (`data/Chinook.sqlite`, v1.4.5, sha256 `bdf635be…a61a`), so there is nothing to download.

What you will see from `make demo` (committed copy: `docs/compare_baseline_vs_change.txt`): a "cost-cutting" prompt change
(`v2-lean-prompt`) is **93% cheaper and faster, but fails the gate**: it loses the schema and the refusal rule, so it executes a
`DELETE`, invents weather data, and breaks multi-join and subquery cases. The gate fails on critical (refusal) regressions, pass-rate
drop, category drops and regression count, and lists the offending outputs.

## The loop, mapped to the brief

| Brief | Where |
|---|---|
| Agent with versioned config | `evalloop/agent.py`; `configs/*/v*.yaml` + `prompts/*.txt`. A config hash covers YAML *and* prompt text |
| 10-15 cases incl. 2 refusals | `evals/cases.jsonl` (14 hand-written, easy→hard, 8 categories, 2 refusals + 1 promoted = 15) |
| Execution accuracy grader | `evalloop/graders.py`: compares **result sets**, not SQL text; column-tolerant; order only when asked |
| Model-graded check measured against labels | `graders.py` (judge) + `calibrate.py` + `evals/judge_labels.jsonl` (22 hand labels) |
| Concurrent runner: latency, tokens, cost, traces | `evalloop/runner.py` (asyncio + semaphore, retries/backoff, disk cache) |
| Run storage, diff, regression flags, thresholds | `store.py`, `compare.py`, `gate/thresholds.yaml` |
| CI on push + schedule, regression fails build | `.github/workflows/eval.yml` |
| Report: pass rate over time, categories, cost/latency, drill-down | `report/index.html` (`evalloop report`) |
| Failure → eval set, implemented | `evalloop promote` + `failures/prod-001.json`; transcript in `docs/promote_demo.txt` |

## Commands

```bash
python -m evalloop run --config configs/mock/v1.yaml --label baseline   # prints RUN_ID=...
python -m evalloop baseline <run_id>                                     # bless a run (runs/BASELINE)
python -m evalloop compare                                               # latest vs baseline; exit 1 on regression
python -m evalloop compare --base <id> --cand <id> --json out.json
python -m evalloop report
python -m evalloop calibrate --judge configs/judge_mock.yaml             # judge vs human labels
python -m evalloop validate-cases
python -m evalloop promote --from-file failures/prod-001.json --category aggregation \
   --gold "SELECT ROUND(AVG(Milliseconds)/60000.0,2) FROM Track" --verify-config configs/mock/v2.yaml
```

### Failure → permanent test (`promote`)
A production failure is logged as JSON (`failures/prod-001.json`: question, bad output, source). A human supplies the **correct SQL**. `promote`:
(1) checks the gold SQL executes and returns rows, (2) rejects duplicate questions, (3) assigns `prom-NNN` with provenance,
(4) **verifies the case actually fails on the config that failed**, refusing to add a test that cannot fail
(`docs/promote_demo.txt` shows it rejecting the case against the fixed config, then accepting it against the failing one), (5) appends it to `cases.jsonl`.
`prom-001` in the committed suite was created this way; it is the case that catches the ms-vs-minutes bug the original 14 cases missed.

### Gate (`gate/thresholds.yaml`)
Paired by case id (new cases never look like regressions; provider-errored cases are excluded and gated separately). Fails on: absolute pass-rate floor;
overall drop > 5 pts; **any regression on a `critical` case** (refusals); a category drop > 25 pts (n≥2); > 2 individual regressions; error rate > 5%.
Warns on cost/case +30% and p95 latency +50% (latency is noisy on real APIs). The exact McNemar p-value is printed; with 15 cases it is rarely significant,
so the gate is deliberately stricter than significance testing and says so.

## Run it with a real model

```bash
export GROQ_API_KEY=...                                  # free key from console.groq.com; any OpenAI-compatible API works
make groq-demo                                           # calibrate judge, run v1 and v2 with llama-3.3-70b-versatile
python -m evalloop baseline <groq-baseline run id>       # bless it
python -m evalloop compare                               # latest vs baseline
```
Swapping provider/model = edit `provider.base_url`, `api_key_env`, `model` in a config (OpenAI, OpenRouter, Together, Ollama, vLLM all speak this API).
Set `pricing` per your provider's price list (values in the repo are placeholders). **CI baseline and CI candidate must use the same provider**:
CI defaults to the offline mock; to evaluate a real model in CI set repo variables `EVAL_CONFIG` / `EVAL_JUDGE` and secret `GROQ_API_KEY`, and commit a real baseline.

## Reproducibility and what stays non-deterministic
* Pinned dependencies; `temperature: 0` and `seed: 42` in every config; Chinook is committed and hashed; cases are hashed (`cases_sha`); config hash covers prompt text; run records carry the git SHA.
* **Not deterministic, by nature:** hosted models are not bit-reproducible even at temperature 0 with a seed (batching, hardware, silent model updates); the LLM judge inherits this; latency varies with load; provider token counts/prices change.
  Mitigations: disk cache (a re-run of an unchanged config+case replays the identical answer), nightly `--no-cache` run to surface provider drift, tolerance bands in the gate.
  Not implemented: N-repeat majority voting (listed in the architecture note).
* Mock provider is fully deterministic, which is why the committed diff is stable.

## Calls I made where the brief was ambiguous
* **Gold answers are results, not SQL.** Column order/aliases/extra columns are ignored, floats rounded to 2 dp, row order ignored unless the question asks for ordering. I avoided questions with tied answers (checked against the data).
* **"Ambiguous" case (`ambig-01`) is graded by the judge**, not execution, because several readings are legitimate. The judge is only as good as its calibration: the mock judge is intentionally naive (kappa 0.64, flagged *not trusted for gating*, `docs/calibrate_mock_judge.txt`). Calibrate a real judge before letting it gate.
* **Refusal = emits `REFUSE:`**. Destructive and off-topic requests are the two refusal cases; both are `critical`.
* **Safety:** the DB is opened read-only, single-statement, with a query timeout, so even a wrongly generated `DELETE` cannot do damage while being graded.
* **Mock provider for the committed runs**: see the note at the top.

## Layout
```
evalloop/   config, db, providers, agent, cases, graders, runner, store, compare, report, promote, calibrate, cli
configs/    agent + judge configs (mock/ and groq/), candidate.yaml (what CI evaluates)
prompts/    versioned prompt files       evals/  cases, judge labels, mock answers, calibration output
gate/       thresholds.yaml              runs/   committed baseline + change run, BASELINE pointer
failures/   logged production failures   docs/   transcripts of the demo commands
tests/      graders, DB safety, gate, promote, runner, suite discrimination      report/index.html
```
