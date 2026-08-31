# FactBench public checks — no LLM, no network, no API keys needed.

check:            ## oracle passes / empty note fails on every task
	python3 scripts/check_tasks.py

mutation-check:   ## gold-anchored dose flips must be caught (positive control)
	python3 scripts/mutation_check.py

test:             ## scorer unit tests (stdlib unittest, no deps)
	python3 tests/test_scorer.py

sync-scorer:      ## propagate scorer/ into every task's tests/scorer/
	python3 scripts/sync_scorer.py

all: check mutation-check test

.PHONY: check mutation-check test sync-scorer all
