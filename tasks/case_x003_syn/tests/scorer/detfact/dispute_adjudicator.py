"""Optional LLM regression pass over frame disputes.

A frame dispute is a candidate critical error that the deterministic
two-vote layer (detfact/critconfirm.py) could not confirm: the claimed value
is anchored somewhere in the source, or the sentence carries past/trial
morphology the rule layer cannot convict on. Disputes never enter the
deterministic pass rule; when the evaluator provides an LLM gateway, this
module re-examines each dispute against the source excerpts and reports which
ones are real errors.

Configuration is environment-only so the public build ships no internal ids:
  DETFACT_ADJUDICATOR_GATEWAY  full chat-completions URL
  DETFACT_ADJUDICATOR_MODEL    model id sent in the request body
If either is unset, adjudication is unavailable and disputes are retained
as-is (this is the documented no-LLM degradation mode).
"""
import json
import os
import re
import urllib.request

from detfact.critconfirm import (fuzzy_windows, _entity_tokens, _norm,
                                 _num_variants)

_PROMPT = (
    "You are auditing one sentence of a clinical note against the source "
    "conversation transcript.\n\n"
    "Source transcript excerpts (ASR style, may contain misspellings):\n"
    "---\n{excerpts}\n---\n\n"
    "Note sentence under audit:\n\"{quote}\"\n\n"
    "For context only — an automatic matcher flagged this sentence against "
    "an answer-key fact ({fact}; flagged: {fields}). The flag is often a "
    "matching artifact; it is NOT evidence. Judge ONLY whether the sentence "
    "is supported by the source text.\n\n"
    "If the sentence, read as written, matches something said in the source "
    "(verbatim or paraphrased), answer FAITHFUL — this includes faithful "
    "history (an earlier dose, a stopped medication, a resolved or improved "
    "symptom, what the patient mis-remembered aloud). Answer ERROR only when "
    "the sentence misstates the source: a number that appears nowhere, "
    "asserting as current something the source says was stopped, asserting "
    "stopped for a medication the source says is current, or stating "
    "something the source denies or never says. If the excerpts do not "
    "contain enough of the source to decide, answer UNCLEAR — never guess "
    "ERROR from absence in a partial excerpt unless the claim's key detail "
    "should have appeared there.\n"
    "Answer with exactly one word: ERROR or FAITHFUL or UNCLEAR.")

_QUOTE_STOP = {
    "patient", "clinician", "doctor", "reports", "reported", "notes", "noted",
    "states", "stated", "denies", "denied", "current", "currently", "history",
    "continue", "continues", "described", "including", "without", "milligrams",
    "tablets", "morning", "evening", "bedtime", "follow", "review", "plan",
    "assessment", "medication", "medications", "previously", "increased",
    "decreased", "started", "stopped", "severe", "moderate",
}


def enabled():
    return bool(os.environ.get("DETFACT_ADJUDICATOR_GATEWAY")
                and os.environ.get("DETFACT_ADJUDICATOR_MODEL"))


def _ask(model, prompt, timeout=90):
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000, "temperature": 0}
    req = urllib.request.Request(
        os.environ["DETFACT_ADJUDICATOR_GATEWAY"],
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    msg = r["choices"][0]["message"]
    text = (msg.get("content") or msg.get("reasoning_content") or "")
    hits = re.findall(r"\b(ERROR|FAITHFUL|UNCLEAR)\b", text.upper())
    return hits[-1] if hits else None


def adjudicate_dispute(claim, fact, transcript, mismatch_fields):
    """Returns 'error' | 'faithful' | 'unclear' (network failure -> 'unclear')."""
    tnorm = _norm(transcript)
    # Wider excerpts than the deterministic layer: entity windows plus windows
    # around every mention of the disputed value (ASR spells doses far from
    # the drug name; a too-narrow excerpt makes the judge convict blind).
    windows = fuzzy_windows(tnorm, _entity_tokens(claim, fact), radius=600)
    val = (claim.get("fields") or {}).get("value")
    if val:
        for v in _num_variants(val):
            for m in re.finditer(r"(?<![\w.])" + re.escape(v) + r"(?![\w.])",
                                 tnorm):
                windows.append(tnorm[max(0, m.start() - 600): m.start() + 600])
    # Sliding-window content match against the sentence itself — the entity
    # anchor can be a matcher artifact ("clinician") that points the excerpts
    # at the wrong part of the conversation. Score each transcript window by
    # how many of the sentence's content words it contains; keep the best.
    qwords = {w for w in re.findall(r"[a-z]{4,}", _norm(
        str(claim.get("evidence_quote") or ""))) if w not in _QUOTE_STOP}
    if qwords:
        step, width = 400, 1000
        scored = []
        for start in range(0, max(1, len(tnorm) - width // 2), step):
            seg = tnorm[start: start + width]
            hits = sum(1 for w in qwords if w in seg)
            if hits >= 2:
                scored.append((hits, start))
        for hits, start in sorted(scored, reverse=True)[:3]:
            windows.append(tnorm[start: start + width])
    if not windows:
        windows = [tnorm[:3000]]
    excerpts = "\n...\n".join(dict.fromkeys(windows))[:12000]
    ff = {k: v for k, v in (fact.get("fields") or {}).items() if v}
    fields = ", ".join(
        "{}: note says '{}' vs key '{}'".format(
            m.get("field"), m.get("claim"), m.get("fact"))
        for m in mismatch_fields) or "(unspecified)"
    prompt = _PROMPT.format(excerpts=excerpts,
                            quote=str(claim.get("evidence_quote") or ""),
                            fact=json.dumps(ff, ensure_ascii=False),
                            fields=fields)
    # Multi-judge quorum (comma-separated model list): unanimous ERROR
    # convicts, unanimous FAITHFUL acquits, anything else stays a dispute —
    # a published adjudicated crit must itself be non-overturnable.
    votes = []
    for model in os.environ["DETFACT_ADJUDICATOR_MODEL"].split(","):
        try:
            votes.append(_ask(model.strip(), prompt))
        except Exception:
            votes.append(None)
    if votes and all(v == "ERROR" for v in votes):
        return "error"
    if votes and all(v == "FAITHFUL" for v in votes):
        return "faithful"
    return "unclear"


def adjudicate_report(report, claims, factset, transcript):
    """Walk a scorer report, adjudicate every frame dispute in place.

    Adds row["crit_adjudication"] and returns
    (adjudicated_errors, faithful, unclear). No-op if not enabled().
    """
    if not enabled():
        return None
    fact_by_key = {}
    for f in factset.get("facts", []):
        fact_by_key.setdefault(f.get("key"), f)
    err = ok = unclear = 0
    SETTLED = {"advice_negation", "hedged_conditional_mention",
               "negation_scopes_change_word", "alias_statement",
               "heading_no_assertion", "bare_mention_status_untrusted",
               "from_side_value", "zero_value_artifact",
               "prn_rate_compatible", "clock_time_dispute"}
    for row in report.get("per_claim", []):
        if row.get("crit") != "dispute":
            continue
        reasons = {d.get("reason") for d in
                   (row.get("crit_detail") or {}).get("demoted") or []}
        if reasons and reasons <= SETTLED:
            # Deterministically settled as non-assertions/grammar-historical:
            # sending them to LLM judges only risks wrongful convictions.
            row["crit_adjudication"] = "faithful"
            ok += 1
            continue
        mmf = []
        for rs in row.get("reasons") or []:
            if rs.get("code") == "field_mismatch":
                mmf.extend(rs.get("fields") or [])
        v = adjudicate_dispute(claims[row["index"]],
                               fact_by_key.get(row.get("matched_fact_key")) or {},
                               transcript, mmf)
        row["crit_adjudication"] = v
        if v == "error":
            err += 1
        elif v == "faithful":
            ok += 1
        else:
            unclear += 1
    report["counts"]["adjudicated_crit"] = err
    report["counts"]["adjudicated_faithful"] = ok
    report["counts"]["adjudicated_unclear"] = unclear
    return err, ok, unclear
