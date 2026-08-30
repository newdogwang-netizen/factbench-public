# FactBench — factual-consistency benchmark for clinical note generation

*Protocol `detfact-v2.0` · fully synthetic public track · harbor-style task layout*

FactBench measures whether an LLM/agent can turn a (fictional) doctor–patient
consultation transcript into a **factually consistent** clinical note. Scoring is
**deterministic** — a rule-based clinical parser plus a tiered fact matcher — so every
verdict is reproducible and auditable down to the transcript quote.

## Why another benchmark

Clinical-note factuality failures are quiet and dangerous: a dose written as 50 mg
instead of 100 mg, a current medication written as past, a fabricated order. LLM
judges are themselves noisy on exactly these details. FactBench's instrument was
therefore calibrated like lab equipment:

- **Negative control**: an independent reference note (never part of gold
  construction) must score **zero critical errors** on every task.
- **Positive control**: a per-class mutation recall card (dose flip, frequency flip,
  negation flip, history flip, resolve flip, drug swap, plan fabrication) is re-run on
  every change; recall may not drop beyond tolerance.
- Both gates ship in this repo: `make check` (oracle passes / empty note fails) and
  `make mutation-check` (dose-flip mutations must be caught). CI is red if either trips.

## How gold is built (note-consensus v2)

The transcript is never mined directly (ASR noise poisons facts). Instead:

1. 7 independent models each write a note from the same transcript;
2. one deterministic parser extracts atomic facts from each note (**the same parser
   scores contestants** — extraction bias cancels by symmetry);
3. facts enter gold only with ≥3 independent supporters and no stable-field
   disagreement; weak fields need a supporting quote; every fact must anchor back to
   the transcript (fuzzy, ASR-tolerant);
4. an arbitration loop (LLM + human spot-check) refines the set; all decisions are
   keyed and replayable;
5. importance is separated from existence: **must-cover** labels come from an
   independent reference note (its author model is excluded from the consensus pool).

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


## Baseline results (informational)

Scores of the seven gold-pool models' own notes against the released gold
(canonical protocol, deterministic scoring). **Read with care**: these models
participated in gold consensus, so in-pool scores carry a structural familiarity
advantage and are published for orientation only, not as a leaderboard.
`critical_wrong` counts are raw instrument readings (known narrative-tense
artifact classes included, not individually adjudicated).

| model | must-cover coverage | critical_wrong (raw) | tasks |
|---|---|---|---|
| qwen3-max | 78% | 10 | 22 |
| gpt-5.4 | 77% | 16 | 25 |
| gpt-5.6-sol | 72% | 11 | 25 |
| glm-5p2 | 61% | 4 | 21 |
| kimi-k3 | 61% | 5 | 21 |
| minimax-m3 | 57% | 3 | 20 |
| deepseek-v4-flash | 54% | 5 | 21 |

To benchmark an outside model, use the Quickstart below — its scores are directly
comparable to the coverage column (same protocol, same verifier).

## How to run

Three ways, from lightest to most standardized:

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

**3. Custom agent / manual** — for your own harness:

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
critical-error count) are produced locally; scoring is fully deterministic and
offline. The runner uses the benchmark's canonical generation protocol (same
system prompt and output tags as the reference notes) so comparisons are fair.
Alternatively, run through the official harbor harness: `harbor run -p tasks -a <agent>`.

## License

Apache-2.0 (harbor ecosystem convention). See `LICENSE` and `NOTICE`.
