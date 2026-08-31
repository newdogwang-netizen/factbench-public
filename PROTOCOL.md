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

## The two-vote critical channel (`detfact-v2.1`, 2026-08-31)
A critical error needs two independent votes:
1. the claim contradicts a `must_not_err` gold fact (tiered matcher), AND
2. deterministic source confirmation (`scorer/detfact/critconfirm.py`): the
   claimed value has no anchor anywhere near the drug/entity in the source
   transcript (fuzzy, ASR-tolerant, digit + spelled-number variants), or — for
   status/polarity — the sentence carries no past/trial morphology that the
   parser is known to misread.

Candidates that fail vote 2 are **frame disputes**: reported per task
(`frame_disputes`) but outside the pass rule. Rationale: an audit of 17 raw
crits from a frontier model found 17/17 were faithful history documentation
(initial doses, from-doses, past trials, resolved side effects) colliding
with current-state gold facts. A confirmed crit is designed to be
non-overturnable: overturning it would require finding the value in the
source, and then it would not have been confirmed.

Optionally, a runner with an LLM gateway can re-examine disputes
(`scorer/detfact/dispute_adjudicator.py`): two independent judges see the
source excerpts and the sentence; only unanimous verdicts are reported
(`adjudicated_crit`). Without a gateway, disputes are simply retained.

## Quorum-demanded coverage (2026-08-31)
Coverage credit for a must-cover fact requires the union of the note's
supporting sentences to include every field that a **strict majority of the
consensus-pool authors actually wrote** in their own sentences for that fact
(sealed per fact as `salience.cover_fields`, derived deterministically from
the fact's evidence quotes — no hand-picked field list). Consequences:
- Naming a drug without its dose earns nothing when the pool wrote the dose.
- A field no competent author writes (e.g. an extraction-noise time value)
  cannot cost anyone credit.
- Fields may be assembled across sentences ("one atomic fact per bullet" is
  rewarded, not punished); duplicate-anchor sentences merge by strict upgrade
  (a more complete restatement replaces a sparser one only when no filled
  field conflicts).
The lenient mention-count is still reported as `must_cover_hit_any` for
transparency but plays no role in scores.

## Calibration (both gates re-run on every change)
- Negative control: reference notes score critical_wrong == 0 on all 25 tasks,
  and a sealed corpus of adjudicated-faithful history sentences must produce
  0 confirmed crits (the anti-overturn gate).
- Positive control: gold-anchored dose-flip mutations of the reference notes
  must be caught (`make mutation-check`).
- Oracle passes / empty note fails on every task (`make check`).

## Known limits (published, not hidden)
- Frame errors (a historical dose written as if current) are demoted to the
  dispute lane by design; they are visible but do not fail a run unless the
  LLM adjudication pass confirms them.
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
