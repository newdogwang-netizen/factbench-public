#!/usr/bin/env python3
"""Build a dataset-style release manifest (manifest.json).

Third-party eval frameworks / leaderboards can consume the benchmark from this
single file: task list, per-file sha256, difficulty tiers, pass rules, license.
Re-run after any task change; CI can diff it to detect silent drift.
"""
import glob
import hashlib
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    tasks = []
    for td in sorted(glob.glob(os.path.join(ROOT, "tasks", "*"))):
        name = os.path.basename(td)
        toml = open(os.path.join(td, "task.toml"), encoding="utf-8").read()
        get = lambda k, d="": (re.search(k + r'\s*=\s*"?([^"\n]+)"?', toml) or [None, d])[1]
        files = {}
        for f in sorted(glob.glob(os.path.join(td, "**", "*"), recursive=True)):
            if os.path.isfile(f) and "__pycache__" not in f and "/tests/scorer/" not in f:
                files[os.path.relpath(f, td)] = sha(f)
        fs = json.load(open(os.path.join(td, "tests", "factset.json")))
        tasks.append({
            "id": name,
            "difficulty": get("difficulty"),
            "gold_facts": len(fs.get("facts") or []),
            "must_cover": sum(1 for x in fs["facts"]
                              if (x.get("salience") or {}).get("must_cover")),
            "reviewed": bool((fs.get("factset") or {}).get("reviewed")),
            "pass_rule": get("pass_rule"),
            "files": files,
        })
    manifest = {
        "name": "factbench",
        "version": "2.2.0",
        "protocol": "detfact-v2.1",
        "license": "Apache-2.0",
        "homepage": "https://github.com/newdogwang-netizen/factbench-public",
        "data_provenance": "fully synthetic (fictional patients); no real patient data",
        "gold_policy": ("note-consensus: fact enters gold with >=3 independent model "
                        "supporters and stable-field agreement (k_support=3 high-precision "
                        "subset, NOT unanimous-only), refined by LLM arbitration with "
                        "recorded human-override decisions"),
        "review_status": ("public factsets are marked reviewed=false: LLM-arbitrated with "
                          "human-override decisions applied, but the 30-card human "
                          "spot-check for the synthetic track is pending"),
        "scoring": "deterministic (no LLM, no network); see scorer/ and tests/verify.py",
        "task_count": len(tasks),
        "tasks": tasks,
    }
    out = os.path.join(ROOT, "manifest.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print("manifest:", out, "-", len(tasks), "tasks")


if __name__ == "__main__":
    main()
