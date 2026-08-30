# FactBench public checks — no LLM, no network, no API keys needed.

check:            ## oracle passes / empty note fails on every task
	python3 scripts/check_tasks.py

mutation-check:   ## gold-anchored dose flips must be caught (positive control)
	python3 scripts/mutation_check.py

sync-scorer:      ## propagate scorer/ into every task's tests/scorer/
	python3 scripts/sync_scorer.py

all: check mutation-check

.PHONY: check mutation-check sync-scorer all
