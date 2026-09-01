# For independent evaluators

This document is the operating protocol for a third party (e.g. an
independent evaluation organization) to run FactBench and report results —
including a held-out track — without trusting Heidi at any step it can
verify itself.

## What you can verify without us

1. **Task integrity.** `manifest.json` lists every task file with sha256 and
   the sealed gold hash. `python3 scripts/build_manifest.py` recomputes it.
2. **Instrument integrity.** The full scorer ships in `scorer/` (and is
   snapshotted inside every task at `tests/scorer/`). Run the control gates:
   - `python3 scripts/check_tasks.py` — the shipped reference solution must
     pass all 25 tasks and the empty note must fail all 25;
   - `python3 scripts/mutation_check.py` — gold-anchored dose flips injected
     into the reference notes must all be caught.
3. **Determinism.** Scoring uses no network and no LLM. Two runs of
   `tests/verify.py` on the same note are byte-identical. The official
   harness path (`pip install harbor; harbor run -p tasks -a oracle`) must
   report 25/25 reward 1.0.
4. **Every verdict's provenance.** Each gold fact carries 7–8 supporting
   sentences quoted from independent pool notes, a transcript anchor, the
   empirically derived chargeable fields (`salience.cover_fields`) and the
   probe evidence (`salience.cover_probe`). Any conviction can be appealed
   sentence-by-sentence; see CONTRIBUTING for the challenge process.

## Running models

- **Generation + scoring** (any OpenAI-compatible endpoint):

  ```bash
  OPENAI_BASE_URL=... OPENAI_API_KEY=... MODEL=... \
  python3 scripts/run_api_model.py
  ```

  The runner enforces the canonical generation protocol (same system prompt
  and final-answer tags as the reference notes) so all rows are comparable.
  Outputs without the required tags are recorded `salvaged`/`invalid` —
  chain-of-thought is never silently scored.

- **Score-only** (you generate notes with your own harness):

  ```bash
  python3 scripts/run_api_model.py --notes-dir <dir> --label <name>
  ```

- **Optional dispute adjudication** (LLM appeals lane): set
  `DETFACT_ADJUDICATOR_GATEWAY` and `DETFACT_ADJUDICATOR_MODEL`
  (comma-separated = multi-judge quorum, unanimous verdicts only). Without
  it, disputes are simply reported as counts — scores do not change either
  way. Use judge models family-isolated from the contestants.

- **Service mode** for CI/product integration: `service/Dockerfile` exposes
  `POST /score {task_id, note}` on port 8830.

## Reporting

`scripts/publish_result.py <summary.json> --label <name>` validates the
schema, refuses hand-edited numbers (sha-checked) and internal strings, and
appends to `docs/data/index.json` (the leaderboard reads it directly). Fields
per entry: coverage (+case-level bootstrap 95% CI), confirmed critical
errors, frame disputes (+LLM-confirmed real), pass rate, safety flags,
benchmark version, summary sha256.

## Held-out track (contamination control)

The public 25 tasks ship with gold and reference solutions, so they can be
trained on. A held-out set — same generator pipeline, same sealed gates,
transcripts and gold never published — is maintained privately. Protocol:

1. Evaluator (or vendor) generates notes for held-out transcripts provided
   under NDA, or submits a model endpoint;
2. Scoring runs the identical pipeline (`run_api_model.py --notes-dir` /
   endpoint mode) on the private tasks;
3. Public tasks' scores act as the calibration reference: a large
   public-vs-held-out gap for one model is itself reportable evidence of
   contamination or overfitting.

Contact: open a GitHub issue or see `CITATION.cff` for maintainer contact.
