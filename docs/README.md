# Results site (GitHub Pages)

This directory is a static leaderboard served by GitHub Pages.

## One-time setup (already done for this repo — kept for forks)
Repository **Settings → Pages → Source: "Deploy from a branch" → Branch: `master`, folder: `/docs`** → Save.
The site appears at `https://<org>.github.io/<repo>/` within a minute.

## Publishing a new result
```bash
# 1. run the benchmark (any of the run paths in the main README)
OPENAI_BASE_URL=... OPENAI_API_KEY=... MODEL=... python3 scripts/run_api_model.py
# 2. register the run on the site (schema-checked; hand-typed numbers are refused)
python3 scripts/publish_result.py results/<model>/summary.json --label "Model Name" --note "optional"
# 3. ship it
git add docs && git commit -m "results: <model> <date>" && git push
```
Each publish appends one entry to `docs/data/index.json`; repeated publishes of the
same label build the History trend automatically. The page is a single static
HTML file (8 languages, light/dark) — no build step, no dependencies.

## Local preview
```bash
cd docs && python3 -m http.server 8841   # open http://localhost:8841
```
