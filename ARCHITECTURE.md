# Architecture note

## Design
The agent is one model call (schema + rules in the prompt → one SELECT, or `REFUSE:`). Everything interesting is the loop:

```
config (YAML+prompt, hashed) ─► runner (async, cached) ─► graders ─► run store ─► compare ─► gate ─► CI status
                                                                          ▲                    │
             evals/cases.jsonl ◄── promote (validate, dedupe, verify it fails) ◄── logged failure + human gold SQL
```
* **Boundaries.** `db` (safe execution), `providers` (one method), `agent`, `graders`, `runner`, `store`, `compare/gate`, `promote`, `report` are separate modules with plain-dict interfaces. The runner does not know about gating; the gate does not know about providers.
* **Grading.** Primary grader is execution accuracy over result sets, column- and order-tolerant, because SQL text is a poor oracle. Refusals are checked deterministically. An LLM judge decides only ambiguous cases and is compared with execution everywhere else; its agreement with 22 hand labels (kappa, false-accept, false-reject) is measured by `calibrate` and shown in the report.
* **Comparison and gate.** Paired by case id so growing the suite never looks like a regression; provider errors are excluded and gated separately so a flaky API can't masquerade as a regression. Rules are per-case (critical flips), per-category, overall, and cost/latency (warn). Thresholds are in one YAML file and justified for a suite where one case is 6.7 pts.
* **Failure → test.** `promote` refuses cases that the failing config already passes. That check is the difference between a regression *test* and a regression *decoration*.

## Trade-offs
* **Small, hand-written, discriminating set over a big benchmark.** I checked discrimination directly: a plausible "cost-cut" prompt drops from 14/15 to 10/15 and fails specifically on refusals, multi-join and subquery cases while *fixing* one join case, i.e. a mixed picture the gate still calls correctly (test: `test_suite_discriminates_good_vs_degraded`).
* **Strict gate over statistical purity.** With 15 cases, exact McNemar on 5 regressions vs 1 fix gives p≈0.22. The gate is intentionally stricter (critical flips, category drops) and prints the p-value rather than hiding the tension.
* **Files over a database.** Run = directory of JSON/JSONL. Trivial to diff, commit and inspect; no service to run for a reviewer.
* **Cache by (config hash, case, schema).** Makes re-runs free and diffs exact; the nightly job disables it to detect provider drift.
* **Static HTML report, no server.** Fine at 15 cases (drill-down inlined), wrong at 10,000.

## Scale: 10,000 cases × 50 configs/week
Already in place: bounded concurrency, retry with backoff/jitter, cache, judge sampling (`--judge-sample-rate`), per-run cost accounting, JSONL results (streamable). Needed: (1) PR runs on a **stratified subset** (all critical + a fixed sample per category), full suite nightly and on release candidates; (2) shard cases across CI jobs and merge results; (3) a token-bucket rate limiter shared across workers instead of per-call backoff; (4) summaries in Postgres and results in object storage, with the report served (paginated) from that; (5) per-case stability history to auto-quarantine flaky cases. Storage: 10k cases ≈ 10-20 MB/run ⇒ ~1 GB/week at 50 runs; system prompt stored once per run, not per case.

## Gaps (honest)
* **Groq free tier forced `--concurrency 1`.** At `--concurrency 4`, `openai/gpt-oss-120b` hit Groq's free-tier rate limit
  (8,000 tokens/minute) and 4 of 15 cases failed with `infra_error` (429s) even after retry/backoff. Concurrency 1 fixed it
  but the wall-clock time for 15 cases went from under 10s to ~30s. A production setup would need either a paid tier with a
  higher TPM limit, or a token-bucket limiter in the runner that paces requests to a known budget instead of firing them
  concurrently and backing off after the fact.

* **No real-model run is committed.** The authoring sandbox blocked LLM APIs, so committed runs use the deterministic mock provider. The loop, gate, report and promotion are exercised end to end; the numbers are not evidence about any real model. The Groq configs are written but **untested by me**, as is the CI workflow on GitHub itself (I ran its steps locally; YAML parses).
* The judge shown is a deliberately naive mock (kappa 0.64, flagged untrusted). A real judge needs re-calibration on more than 22 labels, and the runner does not yet *refuse* to let an uncalibrated judge gate `judge`-graded cases; it only reports it.
* 15 cases cannot support confidence intervals; categories have n=2-3. No N-repeat voting, so a real-API flake can flip a case.
* Column-tolerant matching can (rarely) accept a wrong query that happens to project the right values; ties in gold answers are avoided rather than handled.
* Production ingestion is a JSON file plus a CLI, not a trace pipeline; gold SQL is human-supplied by design.

## Two more weeks would buy
1. Real-model baselines, judge calibration on 100+ labels from two annotators (inter-annotator agreement), judge-trust enforced in the gate.
2. Suite growth to ~300 cases (BIRD/Spider subsets + promoted failures), Wilson intervals per category, N=3 repeats with majority vote for real APIs.
3. Trace ingestion: sample production/thumbs-down traces, cluster failures, propose gold candidates for human review, open a PR automatically.
4. Sharded CI, stratified PR runs, Postgres-backed store, hosted report with history and per-case flake rate.
5. Config bisect: when the nightly run regresses, automatically bisect config/prompt commits to find the culprit.
