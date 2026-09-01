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
        tr_p = os.path.join(td, "environment", "transcript.txt")
        tr = open(tr_p, encoding="utf-8").read() if os.path.isfile(tr_p) else None
        done = False
        for f in fs["facts"]:
            if done:
                break
            sal = f.get("salience") or {}
            flds = f.get("fields") or {}
            drug, val = (flds.get("object") or ""), str(flds.get("value") or "")
            if not sal.get("must_not_err") or not drug or not val.isdigit():
                continue
            flip = str(int(val) * 2)
            # Injection validity: the parser must actually bind the flipped
            # value at the chosen site (a narrative sentence the parser reads
            # as prose is not a valid injection point). Try each occurrence.
            for m in re.finditer(re.escape(drug) + r"[^.\n]{0,60}?\b" + re.escape(val) + r"\b",
                                 note, re.I):
                seg = m.group(0)
                if re.search(r"[/]\s*" + re.escape(val) + r"\b|\b" + re.escape(val) + r"\s*/", seg):
                    continue  # ratio formulations are not single-value doses
                mutated = note[:m.start()] + seg.replace(val, flip, 1) + note[m.end():]
                claims = parse_template_claims(mutated)["claims"]
                if not any(str((c.get("fields") or {}).get("value")) == flip for c in claims):
                    continue  # parser did not bind the flip here: invalid site
                rep = evaluate(fs, claims, transcript=tr)
                fkey = f.get("key")
                reached = any(
                    r.get("matched_fact_key") == fkey
                    and str((claims[r["index"]].get("fields") or {}).get("value")) == flip
                    for r in rep["per_claim"])
                if not reached:
                    continue  # flip bound to a parser-junk anchor: invalid site
                crit = rep["counts"].get("critical_wrong", 0)
                ambiguous = any(
                    r.get("crit") == "dispute" and any(
                        (d.get("reason") or "").endswith("anchored_in_source")
                        for d in (r.get("crit_detail") or {}).get("demoted") or [])
                    for r in rep["per_claim"])
                if ambiguous:
                    continue  # flip coincides with a real source value: invalid
                tested += 1
                if crit > 0:
                    caught += 1
                else:
                    misses.append("{}: {} {}->{}".format(name, drug, val, flip))
                done = True
                break
    print("mutation check: {}/{} dose flips caught".format(caught, tested))
    for miss in misses:
        print("  MISS", miss)
    if tested == 0 or caught < tested:
        sys.exit(1)
    print("MUTATION CHECK PASS")


main()
