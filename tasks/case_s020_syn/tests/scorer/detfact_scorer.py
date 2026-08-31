#!/usr/bin/env python3
"""Deterministic detfact scorer.

Inputs:
- a detfact/factset/v1 JSON exported by detfact_factset.py, or an audit JSON
- a JSON object with claims[]

The scorer uses no LLM and no fuzzy semantic matching. Claim identity is the
canonical anchor produced by detfact_consensus.canonicalize_claim.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

from detfact_consensus import FIELDS, STABLE_FIELDS, canonicalize_claim, norm_field, relevant
from detfact_factset import PROTOCOL_VERSION, RULES_VERSION, make_factset
from detfact.schema import ValidationError, validate_claims_doc, validate_factset
import detfact_oracle as oracle

REPORT_PROTOCOL = "detfact/report/v1"
EVALUATOR_VERSION = "detfact-scorer/0.2"
UNMATCHED_POLICIES = {"auto", "hallucination", "not_in_factset"}
IDENTITY_RULE = "canonical_anchor tiered deterministic match (exact > base > group-key > token-overlap); direction compared as a check"

# direction segments a canonical_anchor may carry after scope.anchor_key
DIRECTION_SEGMENTS = {
    "present", "absent", "active", "improved", "worsened",
    "ordered", "planned", "stop", "done", "decreased", "increased",
}
# only truly contradictory direction pairs count as wrong_fact; a claim saying
# "present" against a gold "active" (or one side missing) is compatible
OPPOSITE_DIRECTIONS = [
    ({"present", "active", "done"}, {"absent"}),
    ({"improved"}, {"worsened"}),
    ({"increased"}, {"decreased"}),
]
COMPAT_GROUPS = {"symptom": ["diagnosis"], "diagnosis": ["symptom"]}


def split_anchor(anchor):
    parts = [p for p in (anchor or "").split(".") if p]
    if len(parts) < 3:
        return {"scope": ".".join(parts[:-1]), "anchor_key": parts[-1] if parts else "",
                "direction": "", "condition": ""}
    scope = ".".join(parts[:2])
    anchor_key = parts[2]
    direction = ""
    cond_parts = []
    for seg in parts[3:]:
        if not direction and not cond_parts and seg in DIRECTION_SEGMENTS:
            direction = seg
        else:
            cond_parts.append(seg)
    return {"scope": scope, "anchor_key": anchor_key,
            "direction": direction, "condition": ".".join(cond_parts)}


# Self-referential pronoun normalization: "feeling not himself" and gold "not myself"
# are the same assertion; pronoun person varies with narrative perspective and does
# not constitute an identity difference
_SELF_PRONOUNS = {"myself", "himself", "herself", "themselves", "oneself"}


def identity_tokens(anchor_key, condition):
    toks = set(anchor_key.split("_")) | set(condition.replace(".", "_").split("_"))
    toks.discard("")
    toks.discard("after")
    if toks & _SELF_PRONOUNS:
        toks = (toks - _SELF_PRONOUNS) | {"self"}
    return toks


def directions_conflict(a, b):
    if not a or not b or a == b:
        return False
    for s1, s2 in OPPOSITE_DIRECTIONS:
        if (a in s1 and b in s2) or (a in s2 and b in s1):
            return True
    return False


# status values are paraphrase-prone ("present" vs "active"); only genuinely
# contradictory pairs count as wrong_fact, everything else is compatible
CONTRA_STATUS = [
    ({"active", "present", "done"}, {"stop", "past", "never", "absent"}),
    # "resolved" asserts termination, a true contradiction with currently-active
    # (past is exempted because it can coexist with currently-active; resolved is not)
    ({"active", "present"}, {"resolved"}),
    ({"improved"}, {"worsened"}),
    ({"increased"}, {"decreased"}),
]


def status_conflict(a, b):
    if not a or not b or a == b:
        return False
    for s1, s2 in CONTRA_STATUS:
        if (a in s1 and b in s2) or (a in s2 and b in s1):
            return True
    return False


# ---- Dimensioned comparison of the time field ----
# Frequency (times per day), clock time (when taken), and course (since when) are
# three orthogonal dimensions: only same-dimension differences constitute a
# contradiction; cross-dimension values are complementary information
# (daily + bedtime = once every night).
_RATE_PATTERNS = [
    (re.compile(r"\btwice daily\b|\b2 times daily\b|\bbid\b|\bevery 12 hours\b"), 2),
    (re.compile(r"\bthree times daily\b|\b3 times daily\b|\btid\b|\bevery 8 hours\b"), 3),
    (re.compile(r"\bfour times daily\b|\b4 times daily\b|\bqid\b|\bevery 6 hours\b"), 4),
    (re.compile(r"\bdaily\b|\bnightly\b|\bevery 24 hours\b|\bevery day\b"), 1),
    (re.compile(r"\bweekly\b|\bonce a week\b"), 7),
]
# Clock-time words are folded into day-part buckets before comparison (bedtime and night do not contradict)
_TIME_POINT_BUCKETS = {
    "morning": "morning", "breakfast": "morning",
    "noon": "midday", "lunch": "midday",
    "afternoon": "afternoon",
    "evening": "evening", "dinner": "evening",
    "night": "night", "bedtime": "night", "midnight": "night",
}
_TIME_POINT_RE = re.compile(
    r"\b(morning|afternoon|evening|night|bedtime|noon|midnight|breakfast|lunch|dinner)\b")


def _freq_rate(t):
    for rx, rate in _RATE_PATTERNS:
        if rx.search(t):
            return rate
    return None


def _time_buckets(t):
    return {_TIME_POINT_BUCKETS[w] for w in _TIME_POINT_RE.findall(t)}


def time_mismatch(cv, fv):
    """Dimensioned time comparison; True = contradiction. Frequency is compared
    first (preserves FREQ_FLIP injection recall), clock times are compared via
    day-part buckets, a one-sided frequency/clock-time value is treated as
    complementary to the other dimension, and course-like values fall back to
    token subset (subset = under-specific, not a contradiction)."""
    if cv == "current" or fv == "current":
        return False  # "current" is the parser's present-tense default and carries no information
    cr, fr = _freq_rate(cv), _freq_rate(fv)
    if cr is not None and fr is not None:
        return cr != fr
    cp, fp = _time_buckets(cv), _time_buckets(fv)
    if cp and fp:
        return not (cp & fp)
    if cr is not None or fr is not None or cp or fp:
        return False
    ct, ft = set(cv.split()), set(fv.split())
    return not (ct <= ft or ft <= ct)


# Order/plan frame: a different event from the "currently (not) taking" current-state fact; do not compare polarity
PLAN_STATUSES = {"ordered", "plan", "planned", "prescribed", "recommended"}
# Efficacy frame ("gabapentin helps with afternoon cravings"): time refers to when the effect applies, not when the drug is taken
EFFICACY_STATUSES = {"helping", "effective"}
# Symptom/diagnosis groups: "historical" and "currently active" coexist clinically; past<->active is not a contradiction
SYMPTOM_GROUPS = {"symptom", "diagnosis", "condition"}


def canonical_bytes(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_doc(doc):
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_factset(path, audit_statuses=None):
    doc = load_json(path)
    if doc.get("protocol_version") == PROTOCOL_VERSION:
        try:
            validate_factset(doc)
        except ValidationError as exc:
            raise SystemExit(exc.code + ": " + exc.message)
        return doc
    if "facts" in doc and "stats" in doc:
        return make_factset(doc, source_path=path, statuses=audit_statuses or ["certain"])
    raise SystemExit("unsupported factset/audit JSON: " + path)


def load_claims(path):
    doc = load_json(path)
    try:
        return validate_claims_doc(doc)
    except ValidationError as exc:
        raise SystemExit(exc.code + ": " + exc.message)


def fact_key(fact):
    return fact.get("canonical_anchor") or fact.get("key")


def build_fact_index(factset):
    """Tiered deterministic indexes: exact anchor, base (scope.anchor_key),
    group-key (group.anchor_key), and per-group parsed rows for token overlap."""
    idx = {
        "exact": defaultdict(list),
        "base": defaultdict(list),
        "group_key": defaultdict(list),
        "by_group": defaultdict(list),
        "all": [],
    }
    for pos, fact in enumerate(factset.get("facts", [])):
        key = fact_key(fact)
        if not key:
            continue
        parsed = split_anchor(key)
        group = parsed["scope"].split(".")[0] if parsed["scope"] else ""
        entry = (pos, fact, parsed)
        idx["all"].append(entry)
        idx["exact"][key].append(entry)
        idx["base"][parsed["scope"] + "." + parsed["anchor_key"]].append(entry)
        idx["group_key"][group + "." + parsed["anchor_key"]].append(entry)
        idx["by_group"][group].append(entry)
    return idx


def claim_canon(claim):
    return canonicalize_claim(claim.get("kind"), claim)


def match_fact(idx, canon):
    """Return (tier_name, [entries]) for the first tier with candidates."""
    anchor = canon.get("canonical_anchor") or ""
    group = canon.get("group") or ""
    scope = canon.get("scope") or ""
    anchor_key = canon.get("anchor_key") or ""
    tiers = [
        ("exact_anchor", idx["exact"].get(anchor, [])),
        ("base_anchor", idx["base"].get(scope + "." + anchor_key, [])),
        ("group_key", idx["group_key"].get(group + "." + anchor_key, [])),
    ]
    for name, entries in tiers:
        if entries:
            if name == "base_anchor" and group == "med":
                # Multiple frames of the same drug (currently taking / stopped /
                # dose changed) are often scattered across scopes: merge in the
                # group_key candidates for joint disambiguation, preferring
                # direction-compatible frames, so a home-med-list entry does not
                # collide with a discontinuation fact
                merged = {id(e): e for e in entries}
                for e in idx["group_key"].get(group + "." + anchor_key, []):
                    merged[id(e)] = e
                return name, list(merged.values())
            return name, entries
    ctoks = identity_tokens(anchor_key, canon.get("condition_anchor") or "")
    overlap = []
    # symptom/diagnosis are clinically adjacent: models often report the same
    # finding under either kind, so token overlap may cross that pair only
    groups = [group]
    if group in COMPAT_GROUPS:
        groups += [g for g in COMPAT_GROUPS[group] if g != group]
    for g in groups:
        for entry in idx["by_group"].get(g, []):
            ftoks = identity_tokens(entry[2]["anchor_key"], entry[2]["condition"])
            if ctoks and ftoks and relevant(" ".join(sorted(ctoks)), " ".join(sorted(ftoks))):
                overlap.append(entry)
    if overlap:
        return "token_overlap", overlap
    # Oracle anchor synonym layer (pure table lookup, zero LLM calls; the table
    # is grown by the offline builder): deep rewrites (NKDA <-> no known drug
    # allergies) are invisible at the token layer; precedents judged SAME
    # unanimously by three models can rescue them. One-way contract: a false
    # DIFFERENT only costs recall.
    syn = []
    canon_obj = (canon.get("object") or canon.get("anchor_key") or "").replace("_", " ")
    if canon_obj:
        for g in groups:
            for entry in idx["by_group"].get(g, []):
                fo = ((entry[1].get("fields") or {}).get("object") or
                      entry[2]["anchor_key"].replace("_", " "))
                if oracle.anchor_equivalent(canon_obj, str(fo)) is True:
                    syn.append(entry)
    if syn:
        return "oracle_synonym", syn
    return "", []


# Generic symptoms / generic terms: literal equality is insufficient to confirm "the same event" (the same word often refers to different events)
GENERIC_OBJECTS = {
    "pain", "anxiety", "depression", "insomnia", "sleep difficulty",
    "fatigue", "nausea", "dizziness", "headache", "blood pressure",
    "medication", "symptom", "dolor", "ansiedad", "depresion",
    "memory impairment", "vitamin", "supplement",
}


def _tokens_subset(short, long_):
    """Whether all tokens of short (plural s and punctuation stripped) are contained in long_."""
    def toks(s):
        out = set()
        for w in s.replace("'s", " ").replace("’s", " ").split():
            w = w.strip("\"'“”().,;:!?").rstrip("s")
            # Light stemming: strip ed/ing inflection from long words (startled/startles → startl)
            if len(w) > 5 and w.endswith("ed"):
                w = w[:-2]
            elif len(w) > 6 and w.endswith("ing"):
                w = w[:-3]
            if w:
                out.add(w)
        return out
    st, lt = toks(short), toks(long_)
    st.discard("")
    lt.discard("")
    return bool(st) and st <= lt


def _frames_compatible(cdir, fdir):
    """Identity confirmation requires consistent event frames: same direction, or
    both in a current state. done/stop (single administration / discontinuation)
    are exclusive frames: any differing direction on the other side (including
    empty) means a different event."""
    if fdir in {"done", "stop"} or cdir in {"done", "stop"}:
        return cdir == fdir
    if not cdir or not fdir or cdir == fdir:
        return not directions_conflict(cdir, fdir)
    if {cdir, fdir} <= {"present", "active"}:
        return True
    return False


# Single-administration frame marker: a specific past time point (clock time /
# last night) = a one-off event, not the same event as a regular medication
# regimen (a claim without such a time point)
_SINGLE_DOSE_RE = re.compile(
    r"\b(last night|yesterday|tonight|this (?:morning|afternoon|evening))\b|\b\d{1,2}:\d{2}\b")


def _single_dose_frame(doc):
    t = norm_field("time", (doc.get("fields") or {}).get("time"))
    return bool(_SINGLE_DOSE_RE.search(t))


def identity_certain(claim, fact, claim_dir="", fact_dir="", index=None):
    """Identity confirmation for token-layer matches: the object is specific and
    equal (or judged same by the oracle), and the event frames are compatible.
    Generic symptom words and cross-frame same-drug pairs are not confirmed."""
    co = norm_field("object", (claim.get("fields") or {}).get("object"))
    fo = norm_field("object", (fact.get("fields") or {}).get("object"))
    if not co or not fo:
        return False
    if co in GENERIC_OBJECTS or fo in GENERIC_OBJECTS:
        # Exception: when the same generic word uniquely identifies one fact
        # within the factset, same word means same entity (the point of the
        # generic-word ban is "the same word often refers to different events";
        # when the whole case has only one anxiety fact there is no ambiguity).
        # Reuses the uniqueness principle of subset identity.
        if index is not None and co == fo:
            hits = sum(1 for _p, f2, _pr in index["all"]
                       if norm_field("object", (f2.get("fields") or {}).get("object")) == co)
            if hits == 1 and _frames_compatible(claim_dir, fact_dir)                     and _single_dose_frame(claim) == _single_dose_frame(fact):
                return True
        return False
    if not _frames_compatible(claim_dir, fact_dir):
        return False
    if _single_dose_frame(claim) != _single_dose_frame(fact):
        return False  # A single inpatient PRN colliding with the regular regimen (single Klonopin 0.5 vs 1mg BID)
    # Same first word plus token subset = an under-specific spelling of the same
    # entity (potassium ⊂ potassium gluconate); metoprolol tartrate vs succinate
    # is not a subset and will not be falsely confirmed.
    # The subset must uniquely identify one fact within the factset: a bare
    # family word ("Vitamin" truncated from Vitamin B12) that hits multiple
    # vitamin facts does not confirm identity.
    ctoks, ftoks = co.split(), fo.split()
    if ctoks[0] == ftoks[0] and (set(ctoks) <= set(ftoks) or set(ftoks) <= set(ctoks)):
        shorter = min(set(ctoks), set(ftoks), key=len)
        if index is not None:
            hits = 0
            for _pos, fact2, _parsed in index["all"]:
                fo2 = norm_field("object", (fact2.get("fields") or {}).get("object"))
                if fo2 and shorter <= set(fo2.split()):
                    hits += 1
            if hits > 1:
                return False
        return True
    if co == fo:
        return True
    return oracle.equivalent("object", co, fo) is True


def selected_check_fields(fact, mode):
    if mode == "none":
        return []
    if mode == "stable":
        return sorted(STABLE_FIELDS)
    if mode == "all":
        return list(FIELDS)
    return fact.get("check_fields") or sorted(STABLE_FIELDS)


_MASS_UNITS = {"mg", "mcg", "g"}
_CONTAINER_RE = re.compile(r"\b(caps?|capsules?|tabs?|tablets?|pills?|drops?|gtt)\b")


def field_mismatches(claim, fact, fields, strict_extra_fields=False, tier=""):
    cfields = claim.get("fields") or {}
    ffields = fact.get("fields") or {}
    wrong = []
    extra = []
    # The plan frame (order/plan, pending execution) and the current-state frame
    # (taking / not taking) are different events: when one side is a plan and the
    # other is current state, status/polarity/value/time are not comparable
    _cstat = norm_field("status", cfields.get("status"))
    _fstat = norm_field("status", ffields.get("status"))
    plan_xor = (_cstat in PLAN_STATUSES) != (_fstat in PLAN_STATUSES)
    for fld in fields:
        cv = norm_field(fld, cfields.get(fld))
        if not cv:
            continue
        fv = norm_field(fld, ffields.get(fld))
        if not fv:
            if strict_extra_fields:
                extra.append({"field": fld, "claim": cv, "fact": None})
            continue
        if fld == "status":
            if plan_xor:
                continue
            cpol = norm_field("polarity", cfields.get("polarity"))
            fpol = norm_field("polarity", ffields.get("polarity"))
            if cpol == "negative" and fpol == "negative":
                continue  # Both sides agree on negation; the status difference is parser default-value noise
            if (len({cv, fv}) > 1 and {cv, fv} <= {"past", "active", "present"}
                    and norm_field("kind", fact.get("kind")) in SYMPTOM_GROUPS | {"vital"}):
                continue  # Historical and current symptoms/signs coexist; true contradictions go through the polarity/value channels
            if cpol == "negative" and fv in {"stop", "past", "on hold", "held"}:
                continue  # "not taking X" (negated present tense) and "discontinued / on hold" are the same reality
            if status_conflict(cv, fv):
                # Dose-change narrative exemption: when the quote contains
                # "from <old value> to <new value>" and the two values belong to the
                # claim's and the fact's value respectively, the status difference
                # between the old and new frames is two sides of the same event
                _q = (claim.get("evidence_quote") or "").lower()
                _cv2 = norm_field("value", cfields.get("value"))
                _fv2 = norm_field("value", ffields.get("value"))
                if _cv2 and _fv2 and (re.search(
                        r"from\s+" + re.escape(_fv2) + r"\s*(?:mg|mcg|g)?\s+(?:to|down to|up to)\s+" + re.escape(_cv2), _q)
                        or re.search(
                        r"from\s+" + re.escape(_cv2) + r"\s*(?:mg|mcg|g)?\s+(?:to|down to|up to)\s+" + re.escape(_fv2), _q)):
                    continue
                wrong.append({"field": fld, "claim": cv, "fact": fv})
            continue
        if fld == "polarity":
            if plan_xor:
                continue  # An order to start a drug coexists with "not currently taking"; do not compare polarity across frames
            if _cstat == "recorded":
                continue  # The list-membership frame ("on the med list / not taking") does not assert whether currently taking
            if cv == "negative" and fv == "positive" and _fstat in {"stop", "past", "on hold", "held"} \
                    and _cstat in {"active", "present", "stop", "past", "prior instance", "recorded", ""}:
                continue  # "not currently taking" is synonymous with gold "stopped / on hold (positive polarity)"
            if cv == "negative" and _cstat == "past" and fv == "positive" \
                    and _fstat not in {"stop", "past"}:
                continue  # Not taking for a stretch in the past (previously stopped) coexists with currently taking; not a contradiction
            if cv == "positive" and fv == "negative" \
                    and _cstat in {"past", "stop"} and _fstat in {"stop", "past"}:
                continue  # "previously took (now stopped)" and gold "negative-polarity discontinuation" are symmetric spellings of the same reality
            if cv != fv:
                wrong.append({"field": fld, "claim": cv, "fact": fv})
            continue
        if fld == "time":
            if plan_xor:
                continue
            fstat = norm_field("status", ffields.get("status"))
            if fstat in EFFICACY_STATUSES:
                continue  # In the efficacy frame, time is when the effect applies; not comparable to the dosing time
            if time_mismatch(cv, fv) and oracle.equivalent(fld, cv, fv) is not True:
                wrong.append({"field": fld, "claim": cv, "fact": fv})
            continue
        if fld == "value":
            if plan_xor:
                continue
            # A single-administration time point (7:51 PM) and a regular frequency
            # (BID) are different administration events: a single inpatient 0.5mg
            # coexists with the regular outpatient 1mg BID; doses are not
            # comparable (case_021 dual-frame)
            ctime = norm_field("time", cfields.get("time")) or ""
            ftime = norm_field("time", ffields.get("time")) or ""
            _clock = re.compile(r"\d{1,2}:\d{2}|\b(?:am|pm)\b|\blast night\b|\btonight\b", re.I)
            c_clock, f_clock = bool(_clock.search(ctime)), bool(_clock.search(ftime))
            c_rate, f_rate = _freq_rate(ctime), _freq_rate(ftime)
            if (c_clock and f_rate is not None) or (f_clock and c_rate is not None):
                continue
            # Dimension guard: a mass dose (100 mg) and a count (2 capsules) are
            # two dimensions and not comparable ("CoQ10 100mg, 2 capsules" are
            # both true at once)
            cu = norm_field("unit", cfields.get("unit"))
            fu = norm_field("unit", ffields.get("unit"))
            c_raw = str(cfields.get("value") or "")
            f_raw = str(ffields.get("value") or "")
            if (cu in _MASS_UNITS and (_CONTAINER_RE.search(f_raw.lower()) or _CONTAINER_RE.search(fu))) or \
               (fu in _MASS_UNITS and (_CONTAINER_RE.search(c_raw.lower()) or _CONTAINER_RE.search(cu))) or \
               (not cu and _CONTAINER_RE.search(f_raw.lower())) or \
               (not fu and (_CONTAINER_RE.search(c_raw.lower()) or _CONTAINER_RE.search(cu))) or \
               (not cu and _CONTAINER_RE.search(fu)):
                continue
            if not fu and cu in _MASS_UNITS and fv:
                # Gold value has no unit: look in the claim quote for the word
                # adjacent to that value -- "2 capsules" is the count dimension,
                # true alongside the claim's mass dose (100mg) with no
                # contradiction; adjacent mg (e.g. "600 mg (1200 mg)") is the same
                # dimension and the contradiction stands
                q = (claim.get("evidence_quote") or "").lower()
                if re.search(r"\b" + re.escape(fv) + r"\s*(capsules?|caps?|tablets?|tabs?|pills?)\b", q):
                    continue
            # Total vs per-tablet dose: "300 mg as two 150 mg tablets",
            # "100mg (two 50mg pills)" -- the same sentence contains both the total
            # and the per-tablet strength; picking different numbers is not a
            # factual contradiction
            q0 = (claim.get("evidence_quote") or "").lower()
            if cv and fv and cv in q0 and fv in q0 and re.search(
                    r"\b(?:two|three|2|3)\s+\d+\s*(?:mg|mcg)\s+(?:tablets?|pills?|caps?)", q0):
                continue
            # Dose-change narrative: "decreased from 10 mg to 5 mg" contains both
            # the old and new values in one sentence; a claim picking up the old
            # value is not asserting a different dose
            q = (claim.get("evidence_quote") or "").lower()
            if cv and fv and re.search(
                    r"from\s+" + re.escape(cv) + r"\s*(?:mg|mcg|g)?\s+(?:to|down to|up to)\s+" + re.escape(fv),
                    q) or re.search(
                    r"from\s+" + re.escape(fv) + r"\s*(?:mg|mcg|g)?\s+(?:to|down to|up to)\s+" + re.escape(cv),
                    q):
                continue
            # The claim value carries a container word in the quote ("1 tablet")
            # while gold is a mass strength (mg): count and strength are two
            # dimensions (sennosides-docusate 1 tablet vs 50mg)
            if cv and re.search(r"\b" + re.escape(cv) + r"\s*(?:tablets?|tabs?|caps?|capsules?|pills?)\b", q0) \
                    and (fu in _MASS_UNITS or re.search(r"\d\s*(?:mg|mcg)\b", f_raw.lower())):
                continue
            if cv != fv and oracle.equivalent(fld, cv, fv) is not True:
                wrong.append({"field": fld, "claim": cv, "fact": fv})
            continue
        if fld == "object":
            anchored = tier in {"exact_anchor", "base_anchor", "group_key"}
            if anchored and (cv in GENERIC_OBJECTS or fv in GENERIC_OBJECTS):
                # Generic words (pain/depression) carry no contradiction-capable
                # information, only under-specificity; anchored tiers only --
                # identity at the token_overlap tier is already uncertain, and the
                # exemption would launder unrelated claims into supported
                continue
            if fv and (_tokens_subset(fv, cv)
                       or _tokens_subset(fv, str(cfields.get("object") or "").lower())):
                # A telegraphic long-sentence object contains the fact's entity
                # name ("having brain zaps today despite..." ⊇ "brain zap"): this
                # is narrative elaboration, not a contradiction. norm_object
                # collapses long sentences into the drug name, so also check the
                # raw object
                continue
            if fv.replace(".", "").isdigit():
                craw_obj = str(cfields.get("object") or "")
                if re.search(r"\b" + re.escape(fv) + r"\b", cv) or not re.search(r"\d", craw_obj):
                    # Numeric gold object: the narrative contains the number
                    # (60-year woman ⊇ 60), or contains no digits at all (asserts
                    # no number, so nothing to contradict)
                    continue
            # Different truncations of the same long instruction sentence are not a
            # contradiction: both objects are long sentences (>=8 words) with the
            # same first 4 words ("call or go to labor and delivery for A" vs "... for B")
            _ct, _ft = cv.split(), fv.split()
            if len(_ct) >= 8 and len(_ft) >= 8 and _ct[:4] == _ft[:4]:
                continue
            # A denial claim whose object has zero overlap with the fact's object
            # = a different statement in the same group, not a contradiction:
            # "denies suicide plan" (current-risk denial) ≠ a miswrite of gold
            # "family history of suicide" (a historical event)
            _cpol = norm_field("polarity", cfields.get("polarity"))
            if anchored and _cpol == "negative" and cv != fv and not relevant(cv, fv):
                continue
            # free-text field: paraphrase with high token overlap is compatible;
            # divergent tokens (e.g. "2 bedroom" vs "3 bedroom") still mismatch
            if cv != fv and not relevant(cv, fv) and oracle.equivalent(fld, cv, fv) is not True:
                wrong.append({"field": fld, "claim": cv, "fact": fv})
            continue
        if fld == "subject":
            craw = (str(cfields.get("subject") or "") + " " + str(cfields.get("object") or "")).lower()
            if fv and fv != "patient" and _tokens_subset(fv, craw):
                continue  # The fact's subject (cousin's boyfriend) is contained in the claim's own narrative
            if cv != fv and oracle.equivalent(fld, cv, fv) is not True:
                wrong.append({"field": fld, "claim": cv, "fact": fv})
            continue
        if cv != fv and oracle.equivalent(fld, cv, fv) is not True:
            wrong.append({"field": fld, "claim": cv, "fact": fv})
    return wrong, extra


def count_potential_fabrications(claims, per_claim, factset):
    """Out-of-bounds fabrication channel (borrowed from the production system's
    PotentialHallucination): a dose-bearing medication claim with no same-entity
    fact anywhere in gold -> potential fabrication.
    This is a "needs human review" alarm, not a conviction: gold is a
    high-precision subset, and the model may legitimately mention drugs gold
    does not include; the consult_note reference reading is this channel's
    noise floor. Same-entity determination uses token subsets (to avoid treating
    rewrites / missing anchors as fabrication)."""
    gold_objs = []
    for f in factset.get("facts", []):
        fo = norm_field("object", (f.get("fields") or {}).get("object"))
        if fo:
            gold_objs.append(fo)
    n = 0
    flagged = []
    for row, claim in zip(per_claim, claims):
        if row.get("verdict") not in {"not_in_factset", "hallucination"}:
            continue
        if claim.get("kind") != "medication":
            continue
        cfields = claim.get("fields") or {}
        if not norm_field("value", cfields.get("value")):
            continue  # Only dose-bearing claims: a dose with no entity to hang it on = the fabrication shape with the highest patient-safety risk
        co = norm_field("object", cfields.get("object"))
        if not co:
            continue
        if any(_tokens_subset(co, fo) or _tokens_subset(fo, co) for fo in gold_objs):
            continue  # Gold has a same-entity fact: a matching gap, not fabrication
        n += 1
        flagged.append(row.get("index"))
    return n, flagged


def rescue_rematch(index, claim, canon, matched_pos, wrong_fields,
                   check_mode, strict_extra_fields):
    """Cross-frame rescue: search the whole factset for a sibling fact with a
    strictly equal object, compatible direction, and zero field conflicts. The
    sibling must itself have a value for every originally conflicting field
    (an under-specified fact is "zero-conflict" only because it asserts nothing,
    and must not be used to launder; injected fake errors therefore have no
    legitimate rescue target). Returns None if not found."""
    co = norm_field("object", (claim.get("fields") or {}).get("object"))
    if not co or co in GENERIC_OBJECTS:
        return None
    claim_dir = canon.get("direction") or ""
    need = {w.get("field") for w in wrong_fields if w.get("field") != "direction"}
    for pos, fact, parsed in index["all"]:
        if pos == matched_pos:
            continue
        ffields = fact.get("fields") or {}
        fo = norm_field("object", ffields.get("object"))
        if fo != co:
            continue
        if directions_conflict(claim_dir, parsed["direction"]):
            continue
        # No rescue across the plan/current-state frames: plan_xor exempts
        # value/status/time entirely, making "zero-conflict" meaningless -- a
        # correct planned dose elsewhere in the note must not launder a wrong
        # dose of the current medication (observed in a cheat trial: apixaban
        # 5→10 was once missed via a plan-frame sibling rescue)
        cstat = norm_field("status", (claim.get("fields") or {}).get("status"))
        fstat = norm_field("status", ffields.get("status"))
        if (cstat in PLAN_STATUSES) != (fstat in PLAN_STATUSES):
            continue
        if any(not norm_field(f, ffields.get(f)) for f in need):
            continue
        w, e = field_mismatches(claim, fact, selected_check_fields(fact, check_mode),
                                strict_extra_fields)
        if not w and not e:
            return pos, fact, parsed
    return None


def default_unmatched_policy(factset):
    selection = factset.get("selection") or {}
    scope = selection.get("gold_scope") or selection.get("scope")
    if scope in {"high_precision_subset", "non_exhaustive", "partial"}:
        return "not_in_factset"
    if (
        selection.get("policy") == "auto_best_full_support_stable"
        and selection.get("statuses") == ["release"]
    ):
        return "not_in_factset"
    return "hallucination"


def resolve_unmatched_policy(factset, policy):
    policy = policy or "auto"
    if policy not in UNMATCHED_POLICIES:
        raise ValueError("invalid unmatched_policy: " + str(policy))
    if policy == "auto":
        return default_unmatched_policy(factset)
    return policy


def covered_fact_fields(fact, supported_claims):
    ffields = fact.get("fields") or {}
    missing = []
    for fld in fact.get("coverage_fields") or []:
        target = norm_field(fld, ffields.get(fld))
        if not target:
            continue
        found = False
        for claim in supported_claims:
            cv = norm_field(fld, (claim.get("fields") or {}).get(fld))
            if cv == target:
                found = True
                break
        if not found:
            missing.append(fld)
    return missing


def evaluate(factset, claims, check_mode="factset", strict_extra_fields=False,
             unmatched_policy="auto", transcript=None):
    from detfact_consensus import derive_patient_names, set_case_subject_aliases
    set_case_subject_aliases(derive_patient_names(factset.get("facts") or []))
    index = build_fact_index(factset)
    resolved_unmatched_policy = resolve_unmatched_policy(factset, unmatched_policy)
    per_claim = []
    supported_by_fact = defaultdict(list)
    counts = Counter()
    counts["total"] = len(claims)
    counts["total_claims"] = len(claims)
    counts["total_facts"] = len(factset.get("facts", []))

    for i, claim in enumerate(claims):
        canon = claim_canon(claim)
        anchor = canon.get("canonical_anchor") or ""
        tier, matches = match_fact(index, canon) if anchor else ("", [])
        if len(matches) > 1:
            # deterministic disambiguation: prefer candidates whose direction
            # does not contradict the claim's
            compatible = [m for m in matches
                          if not directions_conflict(canon.get("direction") or "", m[2]["direction"])]
            if len(compatible) == 1:
                matches = compatible
            elif len(compatible) > 1:
                # Second-level disambiguation: the unique zero-field-conflict
                # candidate wins (deterministic: by fact position; with several
                # zero-conflict candidates take the earliest -- they agree with
                # each other, so the supported semantics are the same; with no
                # zero-conflict candidate keep ambiguous -> unknown, opening no
                # new wrong path)
                zero = []
                for m in compatible:
                    w2, e2 = field_mismatches(claim, m[1],
                                              selected_check_fields(m[1], check_mode),
                                              strict_extra_fields, tier=tier)
                    if not w2 and not e2:
                        zero.append(m)
                if zero:
                    pick = min(zero, key=lambda m: m[0])
                    # A value-bearing claim must not be laundered via a
                    # zero-conflict candidate whose value was never confronted
                    # (under-specification must not launder -- same principle as
                    # rescue). When the picked candidate's value is empty, or its
                    # value was exempted by plan_xor, switch to confronting a
                    # same-frame valued candidate; with no same-frame valued
                    # candidate keep the original pick (legitimate cross-frame
                    # scenarios are undisturbed).
                    cval = norm_field("value", (claim.get("fields") or {}).get("value"))
                    _cstat = norm_field("status", (claim.get("fields") or {}).get("status"))
                    def _same_frame(m):
                        fstat = norm_field("status", (m[1].get("fields") or {}).get("status"))
                        return (_cstat in PLAN_STATUSES) == (fstat in PLAN_STATUSES)
                    _pv = norm_field("value", (pick[1].get("fields") or {}).get("value"))
                    if cval and (not _pv or not _same_frame(pick)):
                        samef_valued = [
                            m for m in compatible
                            if norm_field("value", (m[1].get("fields") or {}).get("value"))
                            and _same_frame(m)]
                        if samef_valued:
                            szero = [m for m in samef_valued if m in zero]
                            pick = min(szero or samef_valued, key=lambda m: m[0])
                    matches = [pick]
                else:
                    matches = compatible
        row = {
            "index": i,
            "key": claim.get("key"),
            "canonical_anchor": anchor,
            "match_tier": tier or None,
            "verdict": None,
            "matched_fact_key": None,
            "reasons": [],
        }
        if not anchor:
            row["verdict"] = "unknown"
            row["reasons"].append({"code": "missing_identity", "message": "claim has no canonical anchor"})
            counts["unknown"] += 1
        elif not matches:
            row["verdict"] = resolved_unmatched_policy
            if resolved_unmatched_policy == "hallucination":
                message = "canonical anchor not present in exhaustive FactSet"
            else:
                message = "canonical anchor not present in non-exhaustive FactSet"
            row["reasons"].append({
                "code": "no_matching_fact",
                "message": message,
                "unmatched_policy": resolved_unmatched_policy,
            })
            counts[resolved_unmatched_policy] += 1
        elif len(matches) > 1:
            row["verdict"] = "unknown"
            row["reasons"].append({"code": "ambiguous_fact", "message": "canonical anchor matched multiple facts"})
            counts["unknown"] += 1
        else:
            fact_pos, fact, fact_parsed = matches[0]
            row["matched_fact_key"] = fact.get("key")
            fields = selected_check_fields(fact, check_mode)
            wrong, extra = field_mismatches(claim, fact, fields, strict_extra_fields,
                                            tier=tier)
            claim_direction = canon.get("direction") or ""
            if directions_conflict(claim_direction, fact_parsed["direction"]):
                wrong.append({"field": "direction", "claim": claim_direction,
                              "fact": fact_parsed["direction"]})
            # Value-confrontation fence: when the claim asserts a value and the
            # matched fact does not, "zero-conflict" is under-specification and
            # must not launder (same principle as rescue). When a sibling fact
            # exists with an equal object, the same frame, and a value, force a
            # confrontation with it; if the confrontation surfaces a value
            # conflict, re-verdict as wrong against that fact.
            _cval = norm_field("value", (claim.get("fields") or {}).get("value"))
            _fval = norm_field("value", (fact.get("fields") or {}).get("value"))
            _q_low = (claim.get("evidence_quote") or "").lower()
            _fact_is_fromside = bool(_fval and re.search(
                r"from\s+" + re.escape(_fval) + r"\b", _q_low))
            if not wrong and not extra and _cval \
                    and canon.get("group") == "med" \
                    and re.match(r"^\d+(?:\.\d+)?$", _cval) \
                    and (not _fval or _fact_is_fromside):
                _co = norm_field("object", (claim.get("fields") or {}).get("object"))
                _cstat = norm_field("status", (claim.get("fields") or {}).get("status"))
                for _pos2, _f2, _p2 in index["all"]:
                    if _pos2 == fact_pos:
                        continue
                    _ff = _f2.get("fields") or {}
                    if norm_field("object", _ff.get("object")) != _co:
                        continue
                    if not norm_field("value", _ff.get("value")):
                        continue
                    _fstat2 = norm_field("status", _ff.get("status"))
                    if (_cstat in PLAN_STATUSES) != (_fstat2 in PLAN_STATUSES):
                        continue
                    _w2, _e2 = field_mismatches(claim, _f2,
                                                selected_check_fields(_f2, check_mode),
                                                strict_extra_fields, tier=tier)
                    if any(x.get("field") == "value" for x in _w2):
                        fact_pos, fact, fact_parsed = _pos2, _f2, _p2
                        row["matched_fact_key"] = _f2.get("key")
                        wrong, extra = _w2, _e2
                        row["reasons"].append({
                            "code": "unvetted_value_confront",
                            "message": "value-bearing claim confronted with valued sibling fact",
                        })
                    break
            if wrong and not extra:
                # Consistency-first rematch: when multiple frames of the same
                # entity are scattered under different anchors (on the home med
                # list vs stopped today), if a sibling fact exists with a strictly
                # equal object and zero field conflicts, verdict supported rather
                # than wrong. Injected fake errors have no zero-conflict
                # candidate, so by construction this rule does not hurt injection
                # recall.
                rescued = rescue_rematch(index, claim, canon, fact_pos, wrong,
                                         check_mode, strict_extra_fields)
                if rescued is not None:
                    fact_pos, fact, fact_parsed = rescued
                    row["matched_fact_key"] = fact.get("key")
                    row["reasons"].append({
                        "code": "cross_frame_rematch",
                        "message": "consistent sibling fact preferred over mismatched frame",
                    })
                    wrong = []
            if extra:
                row["verdict"] = "hallucination"
                row["reasons"].append({"code": "unsupported_field", "fields": extra})
                counts["hallucination"] += 1
            elif wrong and tier in ("token_overlap", "oracle_synonym") and not identity_certain(
                    claim, fact, canon.get("direction") or "", fact_parsed["direction"],
                    index=index):
                # At the loosest match tier with unconfirmed identity, do not
                # support a "contradiction" verdict (red-teaming confirmed this
                # tier contributes 2/3 of false wrong_fact); with confirmed
                # identity (normalized-equal objects or an oracle SAME, e.g. the
                # Potassium gluconate name collision), enforce as usual.
                row["verdict"] = "not_in_factset"
                row["reasons"].append({
                    "code": "weak_tier_mismatch",
                    "message": "token_overlap tier with field mismatch: identity too uncertain for wrong_fact",
                    "fields": wrong,
                })
                counts["not_in_factset"] += 1
            elif wrong:
                row["verdict"] = "wrong_fact"
                row["reasons"].append({"code": "field_mismatch", "fields": wrong})
                counts["wrong_fact"] += 1
            else:
                _cs = norm_field("status", (claim.get("fields") or {}).get("status"))
                _fs = norm_field("status", (fact.get("fields") or {}).get("status"))
                if (_cs in PLAN_STATUSES) != (_fs in PLAN_STATUSES):
                    # The plan frame and the current-state frame do not evidence
                    # each other: writing only the order does not count as
                    # covering the current-state fact, nor is it verdicted wrong
                    # (different frames, independent truth values). This also
                    # restores visibility of fabricated orders through the
                    # secondary "coverage drop" channel.
                    row["verdict"] = "not_in_factset"
                    row["reasons"].append({
                        "code": "frame_mismatch_plan",
                        "message": "plan-frame claim does not evidence a current-state fact",
                    })
                    counts["not_in_factset"] += 1
                else:
                    row["verdict"] = "supported"
                    counts["supported"] += 1
                    supported_by_fact[fact_pos].append(claim)
        per_claim.append(row)

    per_fact = []
    omitted = 0
    anchor_supported = 0
    partial = 0
    fully = 0
    no_anchor = 0
    for pos, fact in enumerate(factset.get("facts", [])):
        supported_claims = supported_by_fact.get(pos, [])
        missing = covered_fact_fields(fact, supported_claims) if supported_claims else list(fact.get("coverage_fields") or [])
        covered = bool(supported_claims) and not missing
        has_anchor_support = bool(supported_claims)
        if covered:
            coverage_status = "fully_covered"
            fully += 1
        elif has_anchor_support:
            coverage_status = "partial"
            partial += 1
            anchor_supported += 1
        else:
            coverage_status = "omitted"
            no_anchor += 1
        if covered:
            anchor_supported += 1
        if not covered:
            omitted += 1
        per_fact.append({
            "index": pos,
            "key": fact.get("key"),
            "canonical_anchor": fact_key(fact),
            "covered": covered,
            "anchor_supported": has_anchor_support,
            "coverage_status": coverage_status,
            "supporting_claim_indexes": [
                i for i, row in enumerate(per_claim)
                if row.get("verdict") == "supported" and row.get("matched_fact_key") == fact.get("key")
            ],
            "missing_coverage_fields": missing,
        })
    counts["omitted_facts"] = omitted
    counts["anchor_supported_facts"] = anchor_supported
    counts["partial_covered_facts"] = partial
    counts["fully_covered_facts"] = fully
    counts["no_anchor_facts"] = no_anchor

    # Two-axis key metrics: emitted only when gold carries salience labels
    # must_cover: the must-cover set defined by clinician triage; must_not_err: facts where an error is catastrophic
    labeled = [f for f in factset.get("facts", []) if isinstance(f.get("salience"), dict)]
    if labeled:
        mc_total = mc_hit = 0
        err_fact_keys = set()
        mc_hit_any = 0
        for pos, fact in enumerate(factset.get("facts", [])):
            sal = fact.get("salience") or {}
            if sal.get("must_cover"):
                mc_total += 1
                sup = supported_by_fact.get(pos)
                if sup:
                    mc_hit_any += 1
                    demanded = sal.get("cover_fields")
                    if demanded is None:
                        # legacy factset without derived cover fields: lenient
                        mc_hit += 1
                    else:
                        # Quorum-demanded coverage: the union of supporting
                        # claims' fields must include every field that a
                        # strict majority of the consensus-pool authors
                        # actually wrote (sealed per fact as
                        # salience.cover_fields). Bare mentions cannot farm
                        # credit for dosed facts; fields no competent author
                        # writes cannot cost anyone credit.
                        missing = covered_fact_fields(fact, sup)
                        if not (set(missing) & set(demanded)):
                            mc_hit += 1
            if sal.get("must_not_err"):
                err_fact_keys.add(fact.get("key"))
        # Two-vote rule: wrong_fact x must_not_err is only the first vote.
        # When the source transcript is available, critconfirm casts the
        # second vote (source anchoring / tense morphology); unconfirmed
        # candidates are demoted to frame_disputes — reported, but outside
        # the pass rule (iron law: a crit must be non-overturnable).
        critical_wrong = frame_disputes = 0
        if transcript:
            from detfact import critconfirm
            fact_by_key = {}
            for f in factset.get("facts", []):
                fact_by_key.setdefault(f.get("key"), f)
            for row in per_claim:
                if (row.get("verdict") != "wrong_fact"
                        or row.get("matched_fact_key") not in err_fact_keys):
                    continue
                mmf = []
                for rs in row.get("reasons") or []:
                    if rs.get("code") == "field_mismatch":
                        mmf.extend(rs.get("fields") or [])
                ok, detail = critconfirm.confirm(
                    claims[row["index"]],
                    fact_by_key.get(row.get("matched_fact_key")) or {},
                    mmf, transcript)
                if ok:
                    critical_wrong += 1
                    row["crit"] = "confirmed"
                    row["crit_detail"] = detail
                else:
                    frame_disputes += 1
                    row["crit"] = "dispute"
                    row["crit_detail"] = detail
        else:
            critical_wrong = sum(
                1 for row in per_claim
                if row.get("verdict") == "wrong_fact"
                and row.get("matched_fact_key") in err_fact_keys)
        counts["must_cover_total"] = mc_total
        counts["must_cover_hit"] = mc_hit
        counts["must_cover_hit_any"] = mc_hit_any  # lenient, for display only
        counts["critical_wrong"] = critical_wrong
        counts["frame_disputes"] = frame_disputes

    fab_n, fab_rows = count_potential_fabrications(claims, per_claim, factset)
    counts["potential_fabrication"] = fab_n
    for i in fab_rows:
        per_claim[i]["reasons"].append({
            "code": "potential_fabrication",
            "message": "dose-bearing medication claim with no same-entity gold fact",
        })

    verdict = "fail" if counts["wrong_fact"] + counts["hallucination"] + counts["unknown"] > 0 else "pass"
    report = {
        "protocol_version": REPORT_PROTOCOL,
        "factset": factset.get("factset") or {},
        "rules": {
            "version": RULES_VERSION,
            "identity": IDENTITY_RULE,
            "check_fields": check_mode,
            "strict_extra_fields": bool(strict_extra_fields),
            "unmatched_policy": resolved_unmatched_policy,
            "requested_unmatched_policy": unmatched_policy or "auto",
            "semantic_oracle": "on" if oracle.enabled() else "off",
            "equivalence_table_sha256": oracle.table_sha256() if oracle.enabled() else None,
        },
        "evaluator": {
            "version": EVALUATOR_VERSION,
            "build_hash": file_sha256(__file__),
        },
        "verdict": verdict,
        "counts": dict(counts),
        "per_claim": per_claim,
        "per_fact": per_fact,
        "report_sha256": None,
    }
    tmp = json.loads(json.dumps(report, ensure_ascii=False))
    tmp["report_sha256"] = None
    report["report_sha256"] = sha256_doc(tmp)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factset", required=True, help="detfact factset JSON, or audit consensus JSON")
    ap.add_argument("--claims", required=True, help="claims JSON with claims[]")
    ap.add_argument("--out", default=None)
    ap.add_argument("--audit-statuses", default="certain",
                    help="when --factset points to audit JSON, statuses to include, or all")
    ap.add_argument("--check-fields", choices=["factset", "stable", "all", "none"], default="factset")
    ap.add_argument("--strict-extra-fields", action="store_true")
    ap.add_argument("--no-fail-exit", action="store_true")
    args = ap.parse_args()

    statuses = [s.strip() for s in args.audit_statuses.split(",") if s.strip()]
    factset = load_factset(args.factset, audit_statuses=statuses)
    claims = load_claims(args.claims)
    report = evaluate(factset, claims, check_mode=args.check_fields,
                      strict_extra_fields=args.strict_extra_fields)
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            f.write(body)
    else:
        sys.stdout.write(body)
    c = report["counts"]
    print("verdict={verdict} supported={supported} wrong={wrong_fact} hallucination={hallucination} unknown={unknown} omitted={omitted_facts} report_sha256={sha}".format(
        verdict=report["verdict"], supported=c.get("supported", 0),
        wrong_fact=c.get("wrong_fact", 0), hallucination=c.get("hallucination", 0),
        unknown=c.get("unknown", 0), omitted_facts=c.get("omitted_facts", 0),
        sha=report["report_sha256"],
    ), file=sys.stderr)
    if report["verdict"] == "fail" and not args.no_fail_exit:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
