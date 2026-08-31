#!/usr/bin/env python3
"""Standalone task checker: oracle passes, no-op fails, files complete.

Runs against the self-contained scorer in ./scorer — no external deps.
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scorer"))
from detfact.template_parser import parse_template_claims  # noqa: E402
from detfact_scorer import evaluate  # noqa: E402

LEAK_RE = re.compile(
    r"\bscenario:|\binclude 3-5\b|\bneed (?:produce|craft|include|ensure|final|doses|3-5)"
    r"|hard requirements|speaker labels|\btranscript should\b|\brun-on dialogue\b", re.I)

REQUIRED = ["task.toml", "instruction.md", "environment/transcript.txt",
            "environment/Dockerfile", "solution/note.md", "solution/solve.sh",
            "tests/factset.json", "tests/verify.py", "tests/test.sh"]


def score(fs, text, min_cov):
    claims = parse_template_claims(text)["claims"] if text.strip() else []
    rep = evaluate(fs, claims)
    mne = {f["key"] for f in fs["facts"] if (f.get("salience") or {}).get("must_not_err")}
    crit = sum(1 for r in rep["per_claim"]
               if r.get("verdict") == "wrong_fact" and r.get("matched_fact_key") in mne)
    mct = sum(1 for f in fs["facts"] if (f.get("salience") or {}).get("must_cover"))
    cov = rep["counts"].get("must_cover_hit", 0) / max(1, mct)
    return cov, crit, (crit == 0 and cov >= min_cov)


def main():
    failures = []
    tasks = sorted(glob.glob(os.path.join(ROOT, "tasks", "*")))
    for td in tasks:
        name = os.path.basename(td)
        missing = [r for r in REQUIRED if not os.path.isfile(os.path.join(td, r))]
        if missing:
            failures.append(name + ": missing " + ",".join(missing))
            continue
        tr = open(os.path.join(td, "environment", "transcript.txt"), encoding="utf-8").read()
        if LEAK_RE.search(tr):
            failures.append(name + ": transcript contains author meta-instructions (prompt leakage)")
            continue
        fs = json.load(open(os.path.join(td, "tests", "factset.json")))
        m = re.search(r"MIN_COVERAGE = ([0-9.]+)",
                      open(os.path.join(td, "tests", "verify.py")).read())
        min_cov = float(m.group(1)) if m else 0.5
        cov, crit, ok = score(fs, open(os.path.join(td, "solution", "note.md"),
                                       encoding="utf-8").read(), min_cov)
        _, _, noop_ok = score(fs, "", min_cov)
        print("{}: oracle cov={:.0%} crit={} pass={} | no-op pass={}".format(
            name, cov, crit, ok, noop_ok))
        if not ok:
            failures.append(name + ": oracle fails verifier")
        if noop_ok:
            failures.append(name + ": empty note passes — verifier broken")
    if failures:
        print("\nCHECK FAIL:")
        for f in failures:
            print("  ✗", f)
        sys.exit(1)
    print("\nCHECK PASS ({} tasks)".format(len(tasks)))


if __name__ == "__main__":
    main()
