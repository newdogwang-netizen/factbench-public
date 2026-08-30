# Contributing to FactBench

## The iron law

Every change — parser rule, matcher tier, gold fact, new task — must pass **both
calibration gates** before merge:

```
make check            # oracle passes / empty note fails on every task
make mutation-check   # gold-anchored dose flips must be caught
```

1. **Negative control**: reference notes must score `critical_wrong == 0` on every
   task (`make check`). One false accusation on a correct note is an instrument defect.
2. **Positive control**: injected dose-flip mutations must be caught
   (`make mutation-check`). A change that silently loses sensitivity is rejected.

Gold content is produced by an internal sealed pipeline (consensus + arbitration +
hash-sealing); it is not regenerated from this repo. Challenge gold via issues
(see below) — accepted challenges are fixed upstream and re-exported.

## Proposing a new task

1. Tasks must be **fully synthetic** (fictional patients; generated transcripts).
   The exporter refuses non-synthetic sources; do not try to work around it.
2. A task enters via the sealed pipeline, not by hand-writing gold:
   transcript → 7-model notes → fact-level consensus (≥3 supporters, stable fields)
   → quote gate → transcript anchoring → arbitration → salience.
3. The domain gate must report `valid` (≥5 released facts). Out-of-domain
   specialties are explicitly excluded until parser coverage is extended.
4. Include the oracle note (independent model, excluded from the consensus pool)
   and verify: oracle passes, empty note fails.

## Challenging gold ("the third net")

If you believe a gold fact is wrong, open an issue with transcript quotes. The
benchmark treats **≥2 independent model families disagreeing with gold on the same
field** as automatic grounds for review — but exoneration only ever happens by
transcript evidence, never by vote count alone. All verdicts are recorded and
replayable (`gold_review_verdicts.json`).

## Style

- Deterministic before smart: no LLM in the scoring path.
- One-directional contracts: semantic-equivalence lookups may only rescue a
  false-difference, never create one.
- Every counted number must be traceable to quotes; aggregate numbers must be
  checked against their distribution (a healthy total can hide starved cases).
