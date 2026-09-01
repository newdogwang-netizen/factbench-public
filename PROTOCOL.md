# Protocol `detfact-v2.1` — public summary

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

## Empirically-derived coverage (2026-09-01)
Coverage charging is derived by running the production scoring pipeline over
the consensus pool's own complete notes — no textual heuristics, no
hand-picked field lists, no parser assumptions:

- A must-cover fact is **chargeable** iff at least `k_support` (3) pool notes
  actually score it through this exact pipeline in their natural context —
  the same evidence threshold that admitted the fact into gold. Facts below
  it leave the coverage denominator with the probe recorded
  (`salience.cover_probe`, counts published); duplicated sibling frames and
  sensor-hard phrasings exit automatically.
- A field is **demanded** iff a strict majority of those scoring notes carried
  it (`salience.cover_fields`). Identity fields (object/subject) are never
  demanded: anchor-tier support already establishes identity.
- Consequences by construction: every charged point has been achieved by
  real independent notes through the real scorer; naming a drug without its
  dose earns nothing where the pool wrote doses; nobody is charged for
  extraction noise or for facts the sensor cannot see in situ; when the
  parser improves, re-derivation re-admits facts automatically.
- The pass bar is `reference coverage x 0.7`, capped at 0.5, with no
  artificial floor — the empty-note-fails gate guards degenerate tasks.
- The lenient mention-count (`must_cover_hit_any`) and the exempt count
  (`must_cover_exempt`) are reported for transparency.

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

Design rationale for every rule above (and the comparison with direct
LLM-judge scoring) lives in `docs/DESIGN.md`.

The full internal protocol (real-transcript validity track, per-class mutation
recall card, sealed hashes) is maintained privately; this file is the public,
self-contained summary.
