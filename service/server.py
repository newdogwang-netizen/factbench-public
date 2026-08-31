#!/usr/bin/env python3
"""FactBench scoring service — a thin HTTP adapter over tasks/ and scorer/.

Endpoints:
  GET  /tasks                 -> task ids + metadata
  GET  /health                -> {"ok": true}
  POST /score                 -> body {"task_id": "...", "note": "..."}
                                 returns the same JSON as tests/verify.py

The service never calls an LLM and needs no network egress. It is an adapter
for product/CI integration; the task layout stays the source of truth.
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scorer"))
from detfact.template_parser import parse_template_claims  # noqa: E402
from detfact.safety import safety_signals  # noqa: E402
from detfact_consensus import DRUG_NAMES  # noqa: E402
from detfact_scorer import evaluate  # noqa: E402

TASKS = {}
for td in sorted(os.listdir(os.path.join(ROOT, "tasks"))):
    base = os.path.join(ROOT, "tasks", td)
    fsp = os.path.join(base, "tests", "factset.json")
    if not os.path.isfile(fsp):
        continue
    vp = open(os.path.join(base, "tests", "verify.py"), encoding="utf-8").read()
    m = re.search(r"MIN_COVERAGE = ([0-9.]+)", vp)
    TASKS[td] = {
        "factset": json.load(open(fsp)),
        "min_coverage": float(m.group(1)) if m else 0.5,
        "transcript": open(os.path.join(base, "environment", "transcript.txt"),
                           encoding="utf-8").read(),
    }


def score(task_id, note):
    t = TASKS[task_id]
    fs = t["factset"]
    claims = parse_template_claims(note)["claims"] if note.strip() else []
    rep = evaluate(fs, claims)
    mne = {f["key"] for f in fs["facts"] if (f.get("salience") or {}).get("must_not_err")}
    crit = sum(1 for r in rep["per_claim"]
               if r.get("verdict") == "wrong_fact" and r.get("matched_fact_key") in mne)
    mct = sum(1 for f in fs["facts"] if (f.get("salience") or {}).get("must_cover"))
    hit = rep["counts"].get("must_cover_hit", 0)
    sc = {}
    for f in safety_signals(claims, t["transcript"], DRUG_NAMES):
        sc[f["type"]] = sc.get(f["type"], 0) + 1
    return {"task_id": task_id, "must_cover_hit": hit, "must_cover_total": mct,
            "coverage": round(hit / max(1, mct), 4), "critical_wrong": crit,
            "potential_fabrication": rep["counts"].get("potential_fabrication", 0),
            "safety_flags": sc, "min_coverage": t["min_coverage"],
            "pass": crit == 0 and hit / max(1, mct) >= t["min_coverage"]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "tasks": len(TASKS)})
        if self.path == "/tasks":
            return self._send(200, {"tasks": sorted(TASKS)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/score":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            task_id, note = req["task_id"], req["note"]
            if task_id not in TASKS:
                return self._send(400, {"error": "unknown task_id"})
            return self._send(200, score(task_id, note))
        except Exception as exc:
            return self._send(400, {"error": str(exc)[:200]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8830"))
    print("factbench scoring service on :%d (%d tasks)" % (port, len(TASKS)))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
