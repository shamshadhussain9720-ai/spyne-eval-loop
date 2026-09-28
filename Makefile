PY ?= python
JUDGE ?= configs/judge_mock.yaml

.PHONY: setup test validate demo report calibrate compare groq-demo

setup:            ## install pinned dependencies
	$(PY) -m pip install -r requirements.txt

test:             ## unit tests + suite-discrimination tests (offline)
	$(PY) -m pytest -q

validate:         ## gold SQL of every case must execute
	$(PY) -m evalloop validate-cases

demo:             ## offline end-to-end: baseline run, change run, diff, gate (last step exits 1 on purpose)
	$(PY) -m evalloop run --config configs/mock/v1.yaml --judge $(JUDGE) --label demo-baseline --no-cache
	$(PY) -m evalloop run --config configs/mock/v2.yaml --judge $(JUDGE) --label demo-change --no-cache
	$(PY) -m evalloop compare --base $$(ls runs | grep demo-baseline | tail -1) --cand $$(ls runs | grep demo-change | tail -1)

compare:          ## latest run vs the blessed baseline (exit 1 = regression)
	$(PY) -m evalloop compare

report:           ## write report/index.html
	$(PY) -m evalloop report

calibrate:        ## judge vs human labels
	$(PY) -m evalloop calibrate --judge $(JUDGE)

groq-demo:        ## same loop with a real model (needs GROQ_API_KEY)
	$(PY) -m evalloop calibrate --judge configs/judge_groq.yaml
	$(PY) -m evalloop run --config configs/groq/v1.yaml --judge configs/judge_groq.yaml --label groq-baseline --concurrency 4
	$(PY) -m evalloop run --config configs/groq/v2.yaml --judge configs/judge_groq.yaml --label groq-change --concurrency 4
