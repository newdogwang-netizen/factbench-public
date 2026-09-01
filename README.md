# FactBench — factual-consistency benchmark for clinical note generation

*Protocol `detfact-v2.1` · fully synthetic public track · harbor-style task layout*

**Live leaderboard:** https://newdogwang-netizen.github.io/factbench-public/

FactBench measures whether an LLM/agent can turn a (fictional) doctor–patient
consultation transcript into a **factually consistent** clinical note. Scoring is
**deterministic** — a rule-based clinical parser plus a tiered fact matcher — so every
verdict is reproducible and auditable down to the transcript quote.

## Why another benchmark

Clinical-note factuality failures are quiet and dangerous: a dose written as
50 mg instead of 100 mg, a stopped medication written as current, a fabricated
order. Nearly every existing note benchmark hands the grading to a stronger
LLM judge — which is precisely the component that is noisy on these details,
open to prompt injection from the note itself, biased toward its own model
family, and impossible to audit ("did the model improve, or did the judge
change?"). FactBench is built the other way around:

- **The score is a measurement, not an opinion.** Coverage and critical
  errors come from a deterministic parser + tiered fact matcher: offline,
  no network, bit-for-bit reproducible; every point traces to a sealed gold
  fact with 7–8 quoted supporting sentences and a transcript anchor.
- **A critical error is non-overturnable by construction.** It must both
  contradict the answer key *and* be confirmed against the source transcript
  (the claimed value appears nowhere near the entity). To appeal one you
  would need to find the value in the source — and then it would never have
  been convicted. We adjudicated 34 consecutive raw flags against frontier
  models by hand; every rule that let a faithful sentence be accused was
  fixed at the root, and the faithful-sentence corpus is sealed as a
  permanent regression gate.
- **The instrument's own error rates are measured and published, like lab
  equipment.** Negative controls: reference notes and a 24-note
  adjudicated-faithful corpus must score zero false convictions. Positive
  controls: a per-class error-injection recall card (dose flips, negation
  flips, history flips, drug swaps...) re-runs on every change with a sealed
  drop tolerance. An LLM-judge benchmark can state none of these numbers.
- **Coverage charges only for what is provably achievable.** A fact enters
  the denominator only if the answer-key pool's own notes score it through
  this exact pipeline; required fields are what a majority of those notes
  actually carried. Name-dropping a drug without its dose earns nothing;
  nobody is punished for extraction noise.
- **LLMs sit only in a published appeals lane.** Ambiguous sentences
  (~1–2% of claims) are listed as `frame_disputes` and may be re-examined by
  two family-isolated judges — unanimous verdicts only, and they can never
  mint or remove a single point of score.
- **Everything needed to disagree with us ships in the repo**: sealed gold
  with per-fact provenance, the scorer source, the control gates
  (`make check`, `make mutation-check`), and the design audits that forced
  each rule (`docs/DESIGN.md`).

## Related work — why this slot was empty

Existing healthcare evals measure something adjacent, not this:

- **Medical-knowledge QA** (MedQA, MMLU-Med, and successors) tests what a
  model *knows*, not whether it stays factual when transcribing a specific
  patient's consultation.
- **Physician-rubric conversation evals** (e.g. HealthBench-style) grade
  open-ended clinical dialogue with LLM/rubric judges — valuable for
  bedside-manner-and-safety breadth, but the grading is judge-dependent and
  not auditable at the level of "this dose contradicts this sentence".
- **Dialogue-to-note datasets** (ACI-Bench, MTS-Dialog, MEDIQA tasks) are the
  closest task-wise, but score with n-gram/embedding overlap (ROUGE,
  BERTScore) or LLM judges — a note can fabricate a dose and still score
  well on overlap, or be graded differently when the judge model changes.

To our knowledge there is no public, deterministic, sentence-appealable
leaderboard for **factual consistency of generated clinical notes**. That is
the slot FactBench fills: fact-level scoring where every conviction is
confirmed against the source transcript and every rule has a published
control gate. (If we missed a comparable effort, open an issue — the
comparison table has room.)

## How gold is built (note-consensus v2)

The transcript is never mined directly (ASR noise poisons facts). Instead:

1. 7 independent models each write a note from the same transcript;
2. one deterministic parser extracts atomic facts from each note (**the same parser
   scores contestants** — extraction bias cancels by symmetry);
3. facts enter gold only with **≥3 independent supporters** and no stable-field
   disagreement (a high-precision consensus subset — *not* unanimous-only gold;
   unanimity would starve gold, since generators are selective about what they write);
   weak fields need a supporting quote; every fact must anchor back to the transcript
   (fuzzy, ASR-tolerant);
4. an arbitration loop refines the set (LLM arbiter from a model family isolated from
   all generators; recorded human-override decisions take precedence). **Review
   status**: public factsets are marked `reviewed: false` — LLM-arbitrated with human
   overrides applied, but the systematic human spot-check for this synthetic track is
   still pending. Treat gold as high-precision but challengeable (see CONTRIBUTING);
5. importance is separated from existence: **must-cover** labels come from an
   independent reference note (its author model is excluded from the consensus pool).

## Scoring semantics (what the numbers mean)

Two independent axes, never blended:

- **Coverage** — of the must-cover facts, how many the note earns credit for.
  Charging is *empirically derived*: a fact charges only if ≥3 of the pool's
  own notes score it through this exact pipeline, and only for the fields a
  majority of those notes carried (`salience.cover_fields` / `cover_probe`).
  Naming a drug without its dose earns nothing where the pool wrote doses.
- **Critical errors** — confirmed dangerous mistakes under the *two-vote
  rule*: the claim must contradict a must-not-err gold fact **and** be
  deterministically confirmed against the source transcript (a dose that
  appears nowhere near the drug, etc.). Unconfirmed candidates surface as
  **`frame_disputes`** — published, outside the pass rule, optionally
  re-examined by a dual-judge LLM lane (unanimous verdicts only,
  `adjudicated_crit`).
- **Pass** per task = `critical_wrong == 0 AND coverage ≥ MIN_COVERAGE`
  (reference coverage × 0.7, capped at 0.5).

Full rationale with the audits that forced each rule: [`docs/DESIGN.md`](docs/DESIGN.md);
mechanics: [`PROTOCOL.md`](PROTOCOL.md).

## Task layout (harbor-style)

```
tasks/<case>/
├── task.toml            # metadata, provenance, pass rule
├── environment/         # agent input: transcript + template + instruction
├── solution/note.md     # oracle (reference note; guaranteed critical_wrong == 0)
└── tests/verify.py      # deterministic verifier + sealed gold factset
```

An empty/no-op note fails (coverage 0); the oracle passes; mutation classes from the
positive-control card double as cheat-trials.

## Known limits (published, not hidden)

**Why the rules are shaped this way** — pass-bar calibration, empirically
derived coverage, the two-vote critical channel, and why the score is produced by a
deterministic scorer with LLMs confined to an appeals lane (vs. direct
LLM-judge grading) — is documented in [`docs/DESIGN.md`](docs/DESIGN.md).

See `PROTOCOL.md` (Known limits): narrative-tense residuals, weak primary-channel
recall on whole-drug-swap and plan-fabrication (covered by fabrication alarms and
coverage-drop channels), specialty domain gate (out-of-domain cases are excluded
explicitly, never silently blended).

## Data provenance & privacy

**All public tasks are fully synthetic** — fictional patients, clinicians, and events,
generated for this benchmark. No real patient data is present in this repository. The
export tool physically refuses to package non-synthetic cases.

**Validity scope (honest):** per-task scoring is deterministic, oracle-verified
(official harness: 25/25 oracle reward = 1.0) and mutation-tested (dose-flip cheat
trials are caught). However, an internal real-transcript track (never published)
shows that cross-model *rankings* on this synthetic track do **not** transfer to
real ASR-transcript performance (Spearman 0.46; an author-rotation experiment ruled
out stylistic bias — the tracks measure different capability axes: clean-dialogue
note-writing here vs. noisy-ASR note-writing internally). Treat scores as measuring
exactly what the tasks contain. Difficulty tiers are in task metadata; noisier
ASR-like transcript recipes are on the roadmap.


## Results

The live leaderboard (all published runs, deterministic + dual-judge
adjudicated, 8 languages) is at
**https://newdogwang-netizen.github.io/factbench-public/**. Gold-pool models'
own scores are annotated on the board where applicable — in-pool models carry
a structural familiarity advantage, so read same-family rows with the notes
column.

## How to run

Five ways, from lightest to most standardized:

**1. Any OpenAI-compatible API (no install)** — see Quickstart below. One script,
stdlib-only, works with OpenAI / Azure / vLLM / Ollama / any `/chat/completions`
endpoint.

**2. Official harbor harness** — `pip install harbor`, then:

```bash
harbor run -p tasks -a <agent>     # e.g. claude-code, codex, ... (agent's own API key required)
harbor run -p tasks -a oracle      # sanity: reference solutions, expect 25/25 reward 1.0
```

harbor builds each task's environment image (agent sees only transcript + template,
no network, no gold), collects `note.md`, then runs the deterministic verifier and
writes per-trial rewards.

**3. Score-only** — you already generated notes with your own harness:

```bash
python3 scripts/run_api_model.py --notes-dir /path/to/notes --label my-model
# expects <notes-dir>/<task_id>/note.md (or <task_id>.md); no API calls
```

**4. Scoring service (Docker)** — for product/CI integration:

```bash
docker build -f service/Dockerfile -t factbench-service .
docker run -p 8830:8830 factbench-service
curl -X POST localhost:8830/score -d '{"task_id":"case_s001_syn","note":"..."}'
```

**5. Custom agent / manual** — for your own harness:

```bash
cd tasks/<case>
# give your agent environment/transcript.txt + environment/template.txt
# it must write note.md into some working directory, then:
cd <workdir> && python3 /path/to/tasks/<case>/tests/verify.py
# prints JSON metrics; exit 0 = pass (critical_wrong == 0 and coverage >= MIN_COVERAGE)
```

Scoring never calls an LLM — results are bit-for-bit reproducible. To validate the
task set itself (oracle passes, empty note fails): `python3 scripts/check_tasks.py`.

## Quickstart: score any OpenAI-compatible API

No agent harness required — the runner speaks plain `/chat/completions`:

```bash
OPENAI_BASE_URL=https://api.example.com/v1 \
OPENAI_API_KEY=sk-... \
MODEL=your-model-name \
python3 scripts/run_api_model.py
```

Per-task results and `results/<model>/summary.json` (pass rate, mean coverage,
critical-error count, case-level bootstrap CI, safety flags) are produced locally;
scoring is fully deterministic and offline. Outputs missing the required
`<final_generated_text>` tags are recorded as `invalid`/`salvaged` generations —
raw chain-of-thought is never silently scored. The runner uses the benchmark's canonical generation protocol (same
system prompt and output tags as the reference notes) so comparisons are fair.
Alternatively, run through the official harbor harness: `harbor run -p tasks -a <agent>`.

## Results site

Published runs are shown on a GitHub Pages leaderboard at
https://newdogwang-netizen.github.io/factbench-public/ (GitHub Pages, branch
`master`, folder `/docs`). Publishing a run is
one command + one commit:

```bash
python3 scripts/publish_result.py results/<model>/summary.json --label "Model Name"
```

## Dataset manifest

`manifest.json` is a dataset-style release manifest (task list, per-file sha256,
difficulty tiers, pass rules, provenance, review status) for research pipelines,
leaderboards, and third-party eval frameworks. Rebuild with
`python3 scripts/build_manifest.py`; `CITATION.cff` has the citation entry.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
