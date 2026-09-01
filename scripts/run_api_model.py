#!/usr/bin/env python3
"""Run FactBench against ANY OpenAI-compatible chat API. Stdlib only.

Usage:
  OPENAI_BASE_URL=https://api.example.com/v1 \
  OPENAI_API_KEY=sk-... \
  MODEL=your-model-name \
  python3 scripts/run_api_model.py [--tasks tasks] [--out results/<model>] [--max-out 8000]

For each task: sends instruction + transcript to /chat/completions, writes the
returned note to <out>/<task>/note.md, scores it with the task's own
deterministic verifier (tests/verify.py logic, in-process), and writes a
leaderboard summary to <out>/summary.json.

No LLM is involved in scoring — only in note generation.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scorer"))
from detfact.template_parser import parse_template_claims  # noqa: E402
from detfact_scorer import evaluate  # noqa: E402

# Canonical FactBench generation protocol (identical to how gold-pool and
# reference notes were produced — fair comparison requires the same protocol).
SYSTEM = (
    "You are a clinical note generator being evaluated for factual consistency. "
    "Use only facts explicitly stated in the provided sources. Follow the template "
    "where relevant, and omit template placeholders or sections that are not "
    "explicitly supported. Do not invent details. Do not output reasoning, analysis, "
    "planning notes, prompt summaries, or commentary. "
    "Use clear section headings and write factual content as bullets. Each bullet "
    "should contain one atomic fact whenever possible. Output the final note only "
    "inside <final_generated_text> and </final_generated_text> tags, with no other "
    "text outside those tags."
)
FINAL_START, FINAL_END = "<final_generated_text>", "</final_generated_text>"


def build_user(transcript, template):
    return ("=== TRANSCRIPT ===\n" + transcript + "\n\n"
            "=== TEMPLATE ===\n" + template + "\n\n"
            "=== ADDITIONS ===\nABSENT\n\n"
            "Generate the final answer/note according to the TEMPLATE. Use only explicit "
            "source facts from TRANSCRIPT and ADDITIONS. Keep the note human-readable, "
            "but make every bullet an atomic factual statement when the template permits. "
            "Return exactly this shape and nothing else:\n"
            + FINAL_START + "\n[final note text]\n" + FINAL_END)


_NOTE_SHAPE_RE = re.compile(r"^\s*(?:#+\s+\S|\*\*[^*]+:\*\*|[A-Z][A-Za-z &/]+:\s*$)", re.M)


def extract_final(raw):
    """Strict protocol extraction.

    Returns (note_text, status): status is "ok" (tags present),
    "salvaged" (no tags, but the raw output is clearly note-shaped:
    starts with a section heading and contains no meta-commentary),
    or "invalid" (no tags and not note-shaped -> scored as empty).
    Silent raw-text scoring is not allowed: chain-of-thought preambles
    would be parsed into noise claims and unfairly graded.
    """
    m = re.search(re.escape(FINAL_START) + r"(.*?)" + re.escape(FINAL_END), raw, re.S)
    if m:
        return m.group(1).strip(), "ok"
    txt = raw.strip()
    head = txt[:200]
    if _NOTE_SHAPE_RE.search(head) and not re.search(
            r"\b(i will|let me|here is|sure,|as an ai|reasoning:)\b", head, re.I):
        return txt, "salvaged"
    return "", "invalid"


def _post(base, key, payload, timeout):
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (key or "none")})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(base, key, model, messages, max_out, timeout=600):
    payload = {"model": model, "messages": messages, "max_tokens": max_out}
    try:
        data = _post(base, key, payload, timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        # o-series / reasoning endpoints reject max_tokens
        if exc.code == 400 and "max_completion_tokens" in body:
            payload.pop("max_tokens")
            payload["max_completion_tokens"] = max_out
            data = _post(base, key, payload, timeout)
        else:
            raise
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content.strip():
        # reasoning models may put everything in reasoning_content
        content = msg.get("reasoning_content") or ""
    return content


def score(task_dir, note):
    fs = json.load(open(os.path.join(task_dir, "tests", "factset.json")))
    m = re.search(r"MIN_COVERAGE = ([0-9.]+)",
                  open(os.path.join(task_dir, "tests", "verify.py")).read())
    min_cov = float(m.group(1)) if m else 0.5
    claims = parse_template_claims(note)["claims"] if note.strip() else []
    _src_p = os.path.join(task_dir, "environment", "transcript.txt")
    _src = open(_src_p, encoding="utf-8").read() if os.path.isfile(_src_p) else ""
    # Two-vote critical channel: contradiction with a must_not_err gold fact
    # must be confirmed against the source transcript (scorer/detfact/
    # critconfirm.py); unconfirmed candidates surface as frame_disputes.
    rep = evaluate(fs, claims, transcript=_src or None)
    crit = rep["counts"].get("critical_wrong", 0)
    disputes = rep["counts"].get("frame_disputes", 0)
    mct = sum(1 for f in fs["facts"] if (f.get("salience") or {}).get("must_cover"))
    hit = rep["counts"].get("must_cover_hit", 0)
    # Optional LLM regression pass over disputes (environment-gated; without
    # a gateway the disputes are simply retained as data).
    adjudicated = None
    try:
        from detfact.dispute_adjudicator import adjudicate_report, enabled
        if enabled() and disputes:
            adjudicate_report(rep, claims, fs, _src)
            adjudicated = {
                "error": rep["counts"].get("adjudicated_crit", 0),
                "faithful": rep["counts"].get("adjudicated_faithful", 0),
                "unclear": rep["counts"].get("adjudicated_unclear", 0)}
    except Exception:
        adjudicated = None
    try:
        from detfact.safety import safety_signals
        from detfact_consensus import DRUG_NAMES
        _sf = safety_signals(claims, _src, DRUG_NAMES) if _src else []
    except Exception:
        _sf = []
    _sc = {}
    for _f in _sf:
        _sc[_f["type"]] = _sc.get(_f["type"], 0) + 1
    row = {"coverage": round(hit / max(1, mct), 4), "must_cover_hit": hit,
           "safety_flags": _sc,
           "must_cover_total": mct, "critical_wrong": crit,
           "frame_disputes": disputes,
           "potential_fabrication": rep["counts"].get("potential_fabrication", 0),
           "min_coverage": min_cov,
           "pass": crit == 0 and hit / max(1, mct) >= min_cov}
    if adjudicated is not None:
        row["dispute_adjudication"] = adjudicated
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=os.path.join(ROOT, "tasks"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-out", type=int, default=8000)
    ap.add_argument("--only", default=None, help="comma-separated task names")
    ap.add_argument("--notes-dir", default=None,
                    help="score-only mode: directory containing <task>/note.md "
                         "(or <task>.md) produced by your own harness; no API calls")
    ap.add_argument("--label", default=None, help="run label for score-only mode")
    args = ap.parse_args()
    base = os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("OPENAI_API_KEY", "")
    # score-only mode: an explicit --label must win over a leftover MODEL env
    model = (args.label if args.notes_dir and args.label
             else os.environ.get("MODEL") or (args.label or "score-only"))
    if not args.notes_dir and (not base or not os.environ.get("MODEL")):
        raise SystemExit("set OPENAI_BASE_URL and MODEL, or use --notes-dir for score-only mode")
    out = args.out or os.path.join(ROOT, "results", re.sub(r"[^\w.-]+", "_", model))
    os.makedirs(out, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    rows = []
    for td in sorted(glob.glob(os.path.join(args.tasks, "*"))):
        name = os.path.basename(td)
        if only and name not in only:
            continue
        note_path = os.path.join(out, name, "note.md")
        gen_status = "resumed"
        if args.notes_dir:
            ext = None
            for cand in (os.path.join(args.notes_dir, name, "note.md"),
                         os.path.join(args.notes_dir, name + ".md")):
                if os.path.isfile(cand):
                    ext = cand
                    break
            if not ext:
                rows.append({"task": name, "generation": "missing_note", "coverage": 0.0,
                             "critical_wrong": 0, "pass": False})
                print(name, "MISSING note in --notes-dir")
                continue
            note = open(ext, encoding="utf-8").read()
            gen_status = "external"
        elif os.path.isfile(note_path) and os.path.getsize(note_path) > 200:
            note = open(note_path, encoding="utf-8").read()  # resume
        else:
            transcript = open(os.path.join(td, "environment", "transcript.txt"),
                              encoding="utf-8").read()
            template = open(os.path.join(td, "environment", "template.txt"),
                            encoding="utf-8").read()
            try:
                note, gen_status = extract_final(chat(base, key, model, [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": build_user(transcript, template)}],
                    args.max_out))
            except Exception as exc:
                print(name, "GENERATION FAILED:", str(exc)[:120])
                rows.append({"task": name, "error": str(exc)[:200],
                             "generation": "error", "pass": False})
                continue
            if gen_status == "invalid":
                print(name, "INVALID GENERATION (no final tags, not note-shaped)")
                rows.append({"task": name, "generation": "invalid", "coverage": 0.0,
                             "critical_wrong": 0, "pass": False})
                continue
            os.makedirs(os.path.dirname(note_path), exist_ok=True)
            with open(note_path, "w", encoding="utf-8") as fh:
                fh.write(note)
        res = {"task": name, "generation": gen_status, **score(td, note)}
        rows.append(res)
        print("{}: cov={:.0%} crit={} pass={}".format(
            name, res["coverage"], res["critical_wrong"], res["pass"]))
    n = len(rows)
    passed = sum(1 for r in rows if r.get("pass"))
    cov = [r["coverage"] for r in rows if "coverage" in r]

    def _bootstrap_ci(values, iters=1000, seed=20260830):
        # case-level bootstrap (tasks are the resampling unit; trials within a
        # task are correlated and must not be treated as independent)
        if len(values) < 2:
            return None
        import random
        rng = random.Random(seed)
        means = sorted(sum(rng.choices(values, k=len(values))) / len(values)
                       for _ in range(iters))
        return [round(means[int(0.025 * iters)], 4),
                round(means[int(0.975 * iters)], 4)]

    summary = {"model": model, "tasks": n, "passed": passed,
               "pass_rate": round(passed / max(1, n), 4),
               "mean_coverage": round(sum(cov) / max(1, len(cov)), 4),
               "coverage_ci95": _bootstrap_ci(cov),
               "total_critical_wrong": sum(r.get("critical_wrong", 0) for r in rows),
               "total_frame_disputes": sum(r.get("frame_disputes", 0) for r in rows),
               "adjudicated_crit": (
                   sum((r.get("dispute_adjudication") or {}).get("error", 0) for r in rows)
                   if any(r.get("dispute_adjudication") for r in rows) else None),
               "safety_flags": {k: sum((r.get("safety_flags") or {}).get(k, 0) for r in rows)
                                 for k in {"medication_near_miss", "unsupported_date",
                                           "unsupported_date_admin", "unsupported_laterality"}},
               "rows": rows}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print("\n{} | pass {}/{} | mean coverage {:.0%} | critical_wrong {}".format(
        model, passed, n, summary["mean_coverage"], summary["total_critical_wrong"]))
    print("summary ->", os.path.join(out, "summary.json"))


if __name__ == "__main__":
    main()
