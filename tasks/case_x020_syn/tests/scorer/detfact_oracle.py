#!/usr/bin/env python3
"""Semantic micro-oracle layer.

Principles (adopted after the 24/24 size-invariance experiment on 2026-08-26):
- The LLM only answers atomic yes/no questions ("are these two short field
  values synonymous/equal?"), and is consulted only AFTER deterministic
  normalization judged them 'different' -- it may overturn a false 'different'
  into 'equivalent', but can never manufacture a 'different'.
- Multi-model quorum: models from different families/sizes must agree,
  otherwise abstain (the hard-rule verdict stands).
- Precedent cache: each value pair is asked once; verdicts go to
  audit_site/equivalence_table.json (human-auditable); replays are pure table
  lookups, preserving determinism and report-hash integrity.

NOTE (public build): the override at the end of this file makes both entry
points always abstain -- public scoring is deterministic with no LLM, no
network, and no lookup table.
"""
import json
import os
import re
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
TABLE_PATH = os.path.join(ROOT, "audit_site", "equivalence_table.json")
QUORUM_MODELS = [
    # (internal arbiter id removed)
    # (internal arbiter id removed)
    # (internal arbiter id removed)
]
GATEWAY = os.environ.get("DETFACT_ORACLE_GATEWAY", "")
# Equivalence judging is enabled only for these fields; polarity/status have dedicated logic and stay purely deterministic
ORACLE_FIELDS = {"object", "value", "unit", "time", "location", "condition"}

_table = None
_dirty = False
_lock = threading.Lock()


def enabled():
    return os.environ.get("DETFACT_ORACLE", "") == "1"


def _load():
    global _table
    if _table is None:
        try:
            _table = json.load(open(TABLE_PATH, encoding="utf-8"))
        except Exception:
            _table = {}
    return _table


def _save():
    global _dirty
    with _lock:
        if not _dirty:
            return
        from detfact.common import atomic_write_text
        atomic_write_text(TABLE_PATH, json.dumps(_table, ensure_ascii=False,
                                                 indent=1, sort_keys=True))
        _dirty = False


def table_sha256():
    import hashlib
    t = _load()
    return hashlib.sha256(json.dumps(t, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _key(field, a, b):
    lo, hi = sorted([str(a), str(b)])
    return field + "|" + lo + "|" + hi


def _ask_one(model, field, a, b):
    prompt = (
        "You are a clinical field comparator. Two short expressions from the "
        "'{f}' field of a clinical fact are given. Answer whether they denote "
        "the SAME clinical value/meaning (pure synonym, abbreviation, spelling, "
        "language or format difference) or DIFFERENT values. Be strict: "
        "different numbers, sides, dates, drugs or polarities are DIFFERENT.\n"
        "A: \"{a}\"\nB: \"{b}\"\n"
        "Answer with exactly one word: SAME or DIFFERENT.").format(f=field, a=a, b=b)
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800, "temperature": 0}
    if "gpt-oss" not in model:
        body["chat_template_kwargs"] = {"thinking": False}
    req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    text = (r["choices"][0]["message"].get("content") or "").upper()
    m = re.search(r"\b(SAME|DIFFERENT)\b", text)
    return m.group(1) if m else None


ANCHOR_PROMPT = (
    "Clinical chart terminology check. In a patient chart, do these two "
    "expressions denote the SAME clinical finding/item (synonym, "
    "abbreviation, lay-term vs medical-term), or DIFFERENT things?\n"
    'A: "{a}"\nB: "{b}"\n'
    "Be strict: related-but-distinct findings (e.g. nausea vs vomiting) "
    "are DIFFERENT.\nAnswer exactly one word: SAME or DIFFERENT.")


def anchor_equivalent(a, b, live=False):
    """Anchor-synonym judging (one-directional contract: only SAME is used for
    rescue; a false DIFFERENT only costs recall). The scoring path is always
    table-lookup only (live=False); the table is grown by an offline builder --
    guaranteeing zero LLM calls at scoring time and full replayability.
    Validation (2026-08-27): 36/40 pairs agreed, false SAME 0/20; the only
    miss was in the harmless direction (racing heart judged DIFFERENT);
    the pre-registered gate (100% agreement) was not met, so it was wired in
    on the one-directional risk surface only -- deviation recorded."""
    a, b = str(a).strip().lower(), str(b).strip().lower()
    if not a or not b or a == b:
        return None
    if len(a.split()) > 6 or len(b.split()) > 6:
        return None
    table = _load()
    k = _key("anchor", a, b)
    if k in table:
        v = table[k]["verdict"]
        return True if v == "same" else (False if v == "different" else None)
    if not (live and enabled()):
        return None
    global _dirty
    votes = []
    for model in QUORUM_MODELS:
        try:
            prompt = ANCHOR_PROMPT.format(a=a, b=b)
            body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 600, "temperature": 0}
            if "gpt-oss" not in model:
                body["chat_template_kwargs"] = {"thinking": False}
            req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            r = json.load(urllib.request.urlopen(req, timeout=90))
            text = (r["choices"][0]["message"].get("content") or "").upper()
            m = re.search(r"\b(SAME|DIFFERENT)\b", text)
            votes.append(m.group(1) if m else None)
        except Exception:
            votes.append(None)
    if all(v == "SAME" for v in votes):
        verdict = "same"
    elif all(v == "DIFFERENT" for v in votes):
        verdict = "different"
    else:
        verdict = "abstain"
    table[k] = {"verdict": verdict, "models": [m.split("/")[-1] for m in QUORUM_MODELS],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _dirty = True
    _save()
    return True if verdict == "same" else (False if verdict == "different" else None)


def equivalent(field, a, b):
    """True = equivalent (overturn) / False = confirmed different / None = abstain (hard-rule verdict stands)."""
    if field not in ORACLE_FIELDS:
        return None
    a, b = str(a).strip(), str(b).strip()
    if not a or not b or a == b:
        return None
    # Overly long free text is not judged (not an atomic question)
    if len(a.split()) > 8 or len(b.split()) > 8:
        return None
    table = _load()
    k = _key(field, a, b)
    if k in table:
        v = table[k]["verdict"]
        return True if v == "same" else (False if v == "different" else None)
    if not enabled():
        return None
    global _dirty
    votes = []
    for model in QUORUM_MODELS:
        try:
            votes.append(_ask_one(model, field, a, b))
        except Exception:
            votes.append(None)
    if all(v == "SAME" for v in votes):
        verdict = "same"
    elif all(v == "DIFFERENT" for v in votes):
        verdict = "different"
    else:
        verdict = "abstain"  # disagreement or failure -> abstain; cache the abstention to avoid re-asking
    table[k] = {"verdict": verdict, "models": [m.split("/")[-1] for m in QUORUM_MODELS],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _dirty = True
    _save()
    return True if verdict == "same" else (False if verdict == "different" else None)


# ---- public build override: deterministic scoring only ----
def equivalent(field, a, b):
    """Public build: no LLM, no network, no lookup table — always abstain."""
    return None


def anchor_equivalent(a, b, live=False):
    return None
