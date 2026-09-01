#!/usr/bin/env python3
"""Deterministic verifier: note.md -> detfact parser -> score against the sealed gold factset."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scorer"))
from detfact.template_parser import parse_template_claims
from detfact_scorer import evaluate

fs = json.load(open(os.path.join(HERE, "factset.json")))
def _find_transcript():
    for cand in ("/app/transcript.txt",
                 os.path.join(HERE, "..", "environment", "transcript.txt")):
        if os.path.isfile(cand):
            return open(cand, encoding="utf-8").read()
    return ""
note_path = os.path.join(os.getcwd(), "note.md")
if not os.path.isfile(note_path):
    print(json.dumps({"pass": False, "reason": "note.md missing"})); sys.exit(1)
claims = parse_template_claims(open(note_path, encoding="utf-8").read())["claims"]
_src = _find_transcript()
# Two-vote critical channel: a crit needs (1) a contradiction with a
# must_not_err gold fact AND (2) deterministic confirmation against the
# source transcript (detfact/critconfirm.py). Unconfirmed candidates are
# reported as frame_disputes: visible, but outside the pass rule.
rep = evaluate(fs, claims, transcript=_src or None)
crit = rep["counts"].get("critical_wrong", 0)
disputes = rep["counts"].get("frame_disputes", 0)
mc_total = sum(1 for f in fs["facts"] if (f.get("salience") or {}).get("must_cover"))
mc_hit = rep["counts"].get("must_cover_hit", 0)
fab = rep["counts"].get("potential_fabrication", 0)
try:
    from detfact.safety import safety_signals
    from detfact_consensus import DRUG_NAMES
    safety = safety_signals(claims, _src, DRUG_NAMES) if _src else []
except Exception:
    safety = []
safety_counts = {}
for _f in safety:
    safety_counts[_f["type"]] = safety_counts.get(_f["type"], 0) + 1
MIN_COVERAGE = 0.29  # oracle-calibrated threshold (reference-note coverage x 0.7)
result = {"must_cover_hit": mc_hit, "must_cover_total": mc_total,
          "coverage": round(mc_hit / max(1, mc_total), 4),
          "critical_wrong": crit, "frame_disputes": disputes,
          "potential_fabrication": fab,
          "safety_flags": safety_counts,  # advisory: not part of the pass rule
          "min_coverage": MIN_COVERAGE,
          "pass": crit == 0 and mc_hit / max(1, mc_total) >= MIN_COVERAGE}
print(json.dumps(result))
try:
    os.makedirs("/logs/verifier", exist_ok=True)
    with open("/logs/verifier/metrics.json", "w") as fh:
        json.dump(result, fh)
except OSError:
    pass
sys.exit(0 if result["pass"] else 1)
