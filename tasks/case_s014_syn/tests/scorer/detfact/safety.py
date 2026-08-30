"""Note-vs-source safety signals (deterministic, advisory).

Adopted from production verify systems: flags note content that the source
transcript does not support, on three high-risk surfaces:

- medication_near_miss: the note names a known drug that is absent from the
  source, while the source contains a *different* known drug whose surface
  form is nearly identical (edit distance <= 2). Catches ASR/typo-grade drug
  swaps (hydroxyzine vs hydralazine) that whole-drug-swap channels miss.
  ASR-correction is exempt by construction: if the nearby source token is NOT
  a known drug (e.g. "Citroline"), correcting it to a real drug is legitimate.
- unsupported_date: the note states a specific calendar date whose components
  (month + day number / numeric date) cannot be found in the source, including
  spelled-out forms ("march fourteenth", "3/14").
- unsupported_laterality: the note asserts a side (left/right/bilateral) in a
  clinical claim while the source never mentions that side at all.

All signals are advisory (reported, not auto-failed) until their false-positive
floor is calibrated like every other channel.
"""
import re

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_ABBR = {m[:3]: m for m in _MONTHS}
_LATERAL_RE = re.compile(r"\b(left|right|bilateral)\b", re.I)
_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?\b")
_DATE_MONTHNAME_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r"|" + "|".join(_MONTH_ABBR) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.I)
_SPELLED_DAY = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}
_TENS = {20: "twenty", 30: "thirty"}
for _t, _tw in _TENS.items():
    for _u, _uw in (("first", 1), ("second", 2), ("third", 3), ("fourth", 4),
                    ("fifth", 5), ("sixth", 6), ("seventh", 7), ("eighth", 8),
                    ("ninth", 9)):
        if _t + _uw <= 31:
            _SPELLED_DAY[_tw + " " + _u] = _t + _uw


def _edit_distance(a, b, cap=3):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = cap + 1
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            best = min(best, v)
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _norm_word(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def _source_words(source_text):
    return {_norm_word(w) for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", source_text)}


def medication_near_miss(claims, source_text, drug_names):
    """Note drug absent from source + a *different* known drug nearby in
    surface form present in source -> near-miss flag."""
    src_words = _source_words(source_text)
    known = {_norm_word(d): d for d in drug_names if len(_norm_word(d)) >= 6}
    src_drugs = {w for w in src_words if w in known}
    flags = []
    seen = set()
    for i, claim in enumerate(claims):
        if (claim.get("kind") or "") not in ("medication", "medication_change"):
            continue
        obj = _norm_word(str((claim.get("fields") or {}).get("object") or ""))
        if len(obj) < 6 or obj not in known or obj in seen:
            continue
        if obj in src_words:
            continue  # drug is in the source; nothing suspicious
        # ASR-correction exemption: a non-drug source token close to the note
        # drug means the model corrected a transcription, not swapped a drug
        if any(_edit_distance(obj, w, 2) <= 2 for w in src_words if w not in known):
            continue
        for sd in src_drugs:
            # drug-vs-drug window: famous confusion pairs sit at distance 3
            # (hydroxyzine/hydralazine); require length>=8 and same 2-prefix
            # to keep precision. ASR-correction exemption above stays at <=2.
            if sd != obj and len(obj) >= 8 and len(sd) >= 8 \
                    and obj[:2] == sd[:2] and _edit_distance(obj, sd, 3) <= 3:
                seen.add(obj)
                flags.append({"type": "medication_near_miss", "claim_index": i,
                              "note_drug": known[obj], "source_drug": known[sd]})
                break
    return flags


def _date_supported(month, day, year, blob):
    blob = blob.replace("-", " ")
    if month and month not in blob:
        return False
    if day:
        spelled = [w for w, n in _SPELLED_DAY.items() if n == int(day)]
        if not re.search(r"\b" + re.escape(str(int(day))) + r"\b", blob) \
                and not any(w in blob for w in spelled):
            return False
    if year and year not in blob and _spell_year(year) not in blob:
        return False
    return True


def _spelled(day):
    for w, n in _SPELLED_DAY.items():
        if n == int(day):
            return w
    return "\x00"


def _spell_year(y):
    y = str(y)
    if len(y) == 4 and y.startswith("20"):
        return "twenty " + {"20": "twenty", "21": "twenty one", "22": "twenty two",
                            "23": "twenty three", "24": "twenty four",
                            "25": "twenty five", "26": "twenty six"}.get(y[2:], "\x00")
    return "\x00"


_ADMIN_DATE_RE = re.compile(
    r"\b(follow[- ]?up|return to work|return visit|appointment|scheduled|"
    r"presenting for|seen (?:on|today)|visit (?:on|date)|next visit|dob|date of birth)\b", re.I)


def _date_flag_type(quote):
    # scheduling/header dates come from the clinic system, not the dialogue;
    # keep them in a separate advisory subtype so the clinical-date floor is 0
    return "unsupported_date_admin" if _ADMIN_DATE_RE.search(quote) else "unsupported_date"


def unsupported_dates(claims, source_text):
    blob = " " + source_text.lower() + " "
    flags = []
    for i, claim in enumerate(claims):
        q = claim.get("evidence_quote") or ""
        # clinical non-date slash forms: pain score N/10, BP 120/80,
        # UK duration shorthand N/12 (months) N/52 (weeks) N/7 (days)
        q = re.sub(r"\b\d{1,2}\s*/\s*(?:10|12|52|7)\b(?!\s*/)", " ", q)
        q = re.sub(r"\b\d{2,3}\s*/\s*\d{2,3}\b(?!\s*/\s*\d)", " ", q)  # BP-like pairs
        for m in _DATE_NUMERIC_RE.finditer(q):
            mo, day, yr = m.groups()
            if not (1 <= int(mo) <= 12 and 1 <= int(day) <= 31):
                continue  # not a calendar date
            month_name = _MONTHS[int(mo) - 1]
            numeric = re.search(re.escape(m.group(0).replace(" ", "")), blob.replace(" ", ""))
            if numeric:
                continue
            if not _date_supported(month_name, day, yr, blob):
                flags.append({"type": _date_flag_type(q), "claim_index": i,
                              "date": m.group(0)})
        for m in _DATE_MONTHNAME_RE.finditer(q):
            mon, day = m.group(1).lower(), m.group(2)
            mon_full = _MONTH_ABBR.get(mon[:3], mon)
            if not _date_supported(mon_full, day, None, blob):
                flags.append({"type": _date_flag_type(q), "claim_index": i,
                              "date": m.group(0)})
    return flags


def unsupported_laterality(claims, source_text):
    src_sides = {m.group(1).lower() for m in _LATERAL_RE.finditer(source_text)}
    flags = []
    for i, claim in enumerate(claims):
        q = claim.get("evidence_quote") or ""
        for m in _LATERAL_RE.finditer(q):
            side = m.group(1).lower()
            supported = side in src_sides or (
                side == "bilateral" and ({"left", "right"} <= src_sides
                                         or re.search(r"\bboth\b", source_text, re.I)))
            if not supported:
                flags.append({"type": "unsupported_laterality", "claim_index": i,
                              "side": side})
                break
    return flags


def safety_signals(claims, source_text, drug_names=None):
    if not source_text:
        return []
    flags = []
    if drug_names:
        flags += medication_near_miss(claims, source_text, drug_names)
    flags += unsupported_dates(claims, source_text)
    flags += unsupported_laterality(claims, source_text)
    return flags
