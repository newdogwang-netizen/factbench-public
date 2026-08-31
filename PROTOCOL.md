# Protocol `detfact-v2.0` — public summary

## How gold was built
1. Seven models each wrote a note from the same fictional transcript
   (kimi-k3, deepseek-v4-flash, glm-5p2, qwen3-max, minimax-m3, gpt-5.6-sol, gpt-5.4).
2. One deterministic parser extracted atomic facts from every note — the same
   parser that scores contestants, so extraction bias cancels by symmetry.
3. A fact entered gold only with >=3 independent supporters and no stable-field
   disagreement; weak fields (time/unit/condition) required a supporting quote;
   every fact had to anchor back to the transcript (fuzzy, ASR-tolerant).
4. An LLM arbitration pass (arbiter isolated from all generator families)
   refined the set; human override decisions take precedence and are recorded.
5. Importance is separated from existence: must-cover labels come from an
   independent reference note whose author model was excluded from that case's
   consensus pool.

## Calibration (both gates re-run on every change)
- Negative control: reference notes score critical_wrong == 0 on all 25 tasks.
- Positive control: gold-anchored dose-flip mutations of the reference notes
  must be caught (`make mutation-check`).
- Oracle passes / empty note fails on every task (`make check`).

## Known limits (published, not hidden)
- Narrative-tense residuals: notes describing current medications in past-tense
  narration can still occasionally parse as historical.
- Whole-drug-swap and plan-fabrication are weak on the primary wrong-fact
  channel; they are covered by fabrication alarms and coverage-drop instead.
- Synthetic-track rankings do not transfer to real-ASR-transcript rankings
  (different capability axes — see README "Validity scope").
- Specialty coverage is bounded by the parser's clinical rule domain; tasks
  ship only for specialties that pass the domain gate.

The full internal protocol (real-transcript validity track, per-class mutation
recall card, sealed hashes) is maintained privately; this file is the public,
self-contained summary.
