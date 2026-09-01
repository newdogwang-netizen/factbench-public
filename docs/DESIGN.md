# FactBench — Design Rationale

Every scoring rule in this benchmark exists because a simpler rule failed an
audit. This document records what each rule is for, and why the overall
architecture is a deterministic scorer with a narrowly-scoped LLM appeals
lane — rather than the common "have a stronger LLM grade the note" design.

## 1. The two axes: coverage and critical errors

A clinical note fails in two independent ways: it can **omit** what matters,
and it can **state** something wrong. One blended score hides which happened,
so we never blend them:

- **Coverage** — of the facts that matter, how many made it into the note.
- **Critical errors** — statements that contradict the sealed answer key in a
  clinically dangerous way (wrong dose, wrong frequency, a stopped drug
  written as current, an asserted finding the patient denied).

The pass rule per task is `critical_wrong == 0 AND coverage >= MIN_COVERAGE`:
one fabrication vetoes the task outright; being merely error-free is not
enough if the note is hollow.

## 2. Why the pass bar is relative (reference coverage × 0.7, clamped 30–50%)

An absolute bar ("all notes need 60% coverage") pretends every consultation
is equally writable. It is not: our tasks range from tidy follow-ups to
rambling multi-topic visits. So each task's bar is anchored to an
**independent reference note** — written by a model excluded from that case's
answer-key consensus pool, under the same prompt as every contestant:

- `MIN_COVERAGE = reference coverage × 0.7`, clamped to [0.30, 0.50].
- Meaning: *reach at least 70% of the information completeness that an
  independent, competent writer achieved on this exact transcript.*
- The bar scales with real task difficulty, is identical for every
  contestant, and by construction the reference itself always passes — which
  is what keeps the shipped oracle solution green (harbor CLI 25/25).
- Honest trade-off: a task whose reference is weak lowers the bar for
  everyone equally. This is the price of difficulty-adaptive standards; it
  cannot change rankings.

## 3. Quorum-demanded coverage: no hand-picked field list

Naive coverage ("the note mentioned the drug") is farmable: name every drug,
skip every dose, collect the points. Strict coverage ("match every field of
the gold fact") punishes people for extraction noise in gold (a `time:
morning` no human would write). Both fail; both were measured before being
replaced.

The rule that replaced them derives the requirement from the same consensus
that built the answer key. Every gold fact ships with the verbatim sentences
of the 7–8 pool models that supported it. At seal time we compute, per fact:

> **Coverage may charge only for what real notes have proven measurable:
> a fact is chargeable iff at least k_support (3) of the pool's own complete
> notes score it through the production pipeline; a field is demanded iff a
> strict majority of those notes carried it.**

The probes are the pool notes themselves, run through the exact scorer that
grades contestants — no textual heuristics and no assumptions about the
parser. Measurability is therefore not a separate gate but a corollary: if
most real notes scored the point in situ, the sensor demonstrably sees it;
facts the sensor cannot see (or duplicated sibling frames no single note can
multiply support) leave the denominator automatically, with the probe
evidence sealed per fact (`salience.cover_probe`) and counts published.
Identity fields (object/subject) are never demanded — anchor-tier support
already establishes identity.

Sealed as `salience.cover_fields`; scoring then requires the union of the
note's supporting sentences to include every chargeable field.

Consequences, all by construction rather than by tuning:
- 8/8 authors wrote the dose → dose is chargeable → bare name-dropping earns
  nothing.
- 1/8 wrote the noisy time value → not chargeable → nobody is punished for
  gold's extraction noise.
- Fields may be assembled across sentences — "one atomic fact per bullet" is
  rewarded, not punished.
- No human ever picks the field list; it is recomputed from evidence and is
  auditable per fact (`cover_fields_votes`).

The lenient mention-count is still published (`must_cover_hit_any`) so anyone
can see exactly how much water the strict rule squeezed out.

## 4. The two-vote critical channel: a conviction must be non-overturnable

The design axiom, adopted after an audit in which **32 of 32** raw critical
flags against frontier models were overturned by reading the source
transcript (all were faithful history documentation — initial doses,
from-doses, past trials, resolved side effects — colliding with
current-state answer-key facts):

> **A published critical error must be immune to appeal. If a human reading
> the transcript could argue the sentence is faithful, it must not be a
> critical error.**

So a conviction needs two independent votes:

1. the claim contradicts a `must_not_err` gold fact (tiered matcher), and
2. deterministic source confirmation: the claimed value appears **nowhere**
   in an ASR-tolerant topic window around the entity (digit and
   spelled-number variants; unit-aware; tight adjacency for unitless
   pain-scale numbers); for status/polarity, the sentence carries no
   past/negation morphology the parser is known to misread.

Overturning a confirmed conviction would require finding the value in the
source — and had it been there, the conviction would not have confirmed.
The burden of proof is flipped by construction, not by review.

Candidates failing vote 2 are **frame disputes**: published per task, never
part of the pass rule.

## 5. The LLM appeals lane — and why this is not "an LLM judge with extra steps"

Disputes may optionally be re-examined by LLM judges (environment-gated;
without a gateway they are simply retained as data). Two judges from
families isolated from the contestants read the source excerpts and one
sentence, and answer one word; **only unanimous verdicts are reported**.

This is the part that looks, from a distance, like every other LLM-judged
benchmark. The differences are structural, not cosmetic:

| | Direct LLM-judge scoring | FactBench |
|---|---|---|
| What the LLM decides | the score | nothing about the score |
| Question size | "grade this note" (open-ended) | "is this one sentence supported by these excerpts?" (atomic) |
| Scope | every note, every run | only pre-filtered disputes (~1–2% of claims), only when a gateway is provided |
| Reproducibility | changes when the judge model, prompt, or temperature changes | deterministic score is bit-reproducible offline; the LLM lane can only re-label a *displayed* dispute |
| Audit trail | a scalar and maybe a rationale | every point traces to a sealed gold fact with 7–8 quoted supporters, a transcript anchor, and a named rule; every penalty is appealable sentence-by-sentence |
| Prompt injection via the note | a real attack surface (notes can flatter or instruct the judge) | the scorer never "reads" the note as instructions; it parses it |
| Judge error rate | unknown, unmeasured | measured and sealed: mutation recall card per error class (positive control), 0-FP floors on doctor notes and a 24-note adjudicated-faithful corpus (negative controls) |
| Self-preference / family bias | systematic and documented in the literature | judges family-isolated from contestants; unanimity required; a judge can only move a dispute to "real error", never mint or remove coverage |
| Drift over time | "did the model improve or did the judge change?" is undecidable | any instrument change must re-pass both control gates; baselines are sealed and diffed |

The one-line version: **LLMs are used where judgment is genuinely needed —
building the answer key by multi-model consensus, and adjudicating a small,
published set of ambiguous sentences — and are structurally barred from the
part that produces the number.** A benchmark's number should be a
measurement, not an opinion; opinions are confined to an appeals court whose
docket, verdicts, and error rates are all public.

## 6. Control gates: the instrument is itself under test

Every change to the parser, matcher, or confirmation layer must re-pass, in
one command:

- **Negative controls (false-positive floor):** reference doctor notes on
  three tracks must score 0 confirmed critical errors; a sealed corpus of
  24 model-note excerpts — each individually adjudicated faithful against
  its transcript — must also score 0. Any rule change that re-convicts a
  faithful history sentence turns the gate red.
- **Positive controls (sensitivity):** per-class error injections
  (dose flips, negation removals, frequency flips, history flips, drug
  swaps, fabricated dates/laterality...) with a sealed per-class recall
  card and a drop tolerance of 2 points. Injections whose mutated value
  happens to coincide with a real source value are excluded as invalid
  injections, and that exclusion is itself reported.
- **Oracle/no-op:** the shipped reference solution must pass all 25 tasks
  (verified via the official harbor CLI) and the empty note must fail all 25.

Numbers on the leaderboard therefore come with a provenance: which
instrument version, which sealed answer key (hashes in `manifest.json`),
which gates were green.

## 7. Known limits (kept on the label)

- Frame errors (a historical dose written as if current) land in the dispute
  lane by design; they are visible but only fail a run if the LLM lane
  unanimously convicts.
- Synthetic-track rankings do not transfer to real-ASR-transcript rankings;
  the benchmark measures factual discipline on clean fictional consults.
- The pass bar inherits the reference author's completeness per task.
- Human clinical blind review of the answer keys is the outstanding upgrade;
  everything above is engineering-grade, not clinically endorsed.
