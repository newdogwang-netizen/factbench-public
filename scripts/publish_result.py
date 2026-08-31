#!/usr/bin/env python3
"""Publish a benchmark run to the GitHub Pages leaderboard (docs/).

Usage:
  python3 scripts/publish_result.py results/<model>/summary.json \
      [--label display-name] [--note "free text shown on the site"]

Guards:
- accepts only runner-produced summary.json (schema check) — no hand-typed numbers;
- sensitive-string scan before anything lands in docs/;
- registry docs/data/index.json is append-only (one entry per publish).
Publishing = run this + `git commit` + push.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
REQUIRED = {"model", "tasks", "passed", "pass_rate", "mean_coverage", "rows"}
SENSITIVE = re.compile(
    r"/mnt/data2|/home/Admin|127\.0\.0\.1|9093|9094|nemotron|accounts/fireworks", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("summary")
    ap.add_argument("--label", default=None)
    ap.add_argument("--note", default="")
    ap.add_argument("--crit-kind",
                    choices=["raw", "calibrated", "confirmed", "confirmed+llm"],
                    default=None,
                    help="default derives from the summary: two-vote summaries "
                         "are 'confirmed' ('confirmed+llm' when the dispute "
                         "adjudication pass ran); legacy summaries are 'raw'")
    args = ap.parse_args()
    raw = open(args.summary, encoding="utf-8").read()
    if SENSITIVE.search(raw):
        raise SystemExit("REFUSED: summary contains internal strings; clean it first")
    doc = json.loads(raw)
    missing = REQUIRED - set(doc)
    if missing:
        raise SystemExit("REFUSED: not a runner summary (missing %s)" % sorted(missing))
    manifest = json.load(open(os.path.join(ROOT, "manifest.json")))
    label = args.label or doc["model"]
    date = datetime.date.today().isoformat()
    slug = re.sub(r"[^\w.-]+", "_", label) + "__" + date
    os.makedirs(os.path.join(DOCS, "data"), exist_ok=True)
    dst = os.path.join(DOCS, "data", slug + ".json")
    shutil.copy(args.summary, dst)
    idx_path = os.path.join(DOCS, "data", "index.json")
    idx = json.load(open(idx_path)) if os.path.isfile(idx_path) else {"entries": []}
    # same label + same date = correction of today's run: replace, don't duplicate
    idx["entries"] = [e for e in idx["entries"]
                      if not (e["label"] == label and e["date"] == date)]
    idx["entries"].append({
        "label": label, "date": date, "file": slug + ".json",
        "tasks": doc["tasks"], "pass_rate": doc["pass_rate"],
        "mean_coverage": doc["mean_coverage"],
        "coverage_ci95": doc.get("coverage_ci95"),
        "critical_wrong": doc.get("total_critical_wrong", 0),
        "frame_disputes": doc.get("total_frame_disputes"),
        "adjudicated_crit": doc.get("adjudicated_crit"),
        "crit_kind": args.crit_kind or (
            ("confirmed+llm" if doc.get("adjudicated_crit") is not None
             else "confirmed")
            if "total_frame_disputes" in doc else "raw"),
        "safety_flags": sum((doc.get("safety_flags") or {}).values()),
        "benchmark_version": manifest["version"],
        "protocol": manifest["protocol"],
        "summary_sha256": hashlib.sha256(raw.encode()).hexdigest()[:16],
        "note": args.note,
    })
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False, indent=1)
    print("published:", label, date, "->", dst)
    print("now: git add docs && git commit && git push  (Pages updates automatically)")


if __name__ == "__main__":
    main()
