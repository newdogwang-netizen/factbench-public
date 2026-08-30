#!/usr/bin/env python3
"""Public positive control: gold-anchored dose-flip mutations must be caught.

For each task that has a dosed must-not-err medication quoted in the reference
note, double that dose in the note and assert the verifier reports a critical
error (or at minimum fails). This is the open-source half of the internal
mutation recall card.
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


def main():
    caught, tested, misses = 0, 0, []
    for td in sorted(glob.glob(os.path.join(ROOT, "tasks", "*"))):
        name = os.path.basename(td)
        fs = json.load(open(os.path.join(td, "tests", "factset.json")))
        note = open(os.path.join(td, "solution", "note.md"), encoding="utf-8").read()
        hit = None
        for f in fs["facts"]:
            sal = f.get("salience") or {}
            flds = f.get("fields") or {}
            drug, val = (flds.get("object") or ""), str(flds.get("value") or "")
            if not sal.get("must_not_err") or not drug or not val.isdigit():
                continue
            m = re.search(re.escape(drug) + r"[^.\n]{0,60}?\b" + re.escape(val) + r"\b",
                          note, re.I)
            if m and re.search(r"[/]\s*" + re.escape(val) + r"\b|\b" + re.escape(val) + r"\s*/",
                               m.group(0)):
                continue  # 配比剂型(250/50)不是单值剂量,变异无效
            if m:
                hit = (drug, val, m)
                break
        if not hit:
            continue
        tested += 1
        drug, val, m = hit
        mutated = note[:m.start()] + m.group(0).replace(val, str(int(val) * 2), 1) + note[m.end():]
        rep = evaluate(fs, parse_template_claims(mutated)["claims"])
        mne = {x["key"] for x in fs["facts"] if (x.get("salience") or {}).get("must_not_err")}
        crit = sum(1 for r in rep["per_claim"]
                   if r.get("verdict") == "wrong_fact" and r.get("matched_fact_key") in mne)
        if crit > 0:
            caught += 1
        else:
            misses.append("{}: {} {}->{}".format(name, drug, val, int(val) * 2))
    print("mutation check: {}/{} dose flips caught".format(caught, tested))
    for miss in misses:
        print("  MISS", miss)
    if tested == 0 or caught < tested:
        sys.exit(1)
    print("MUTATION CHECK PASS")


if __name__ == "__main__":
    main()
