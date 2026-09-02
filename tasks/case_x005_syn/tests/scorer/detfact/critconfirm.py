"""Crit confirmation layer — the "two-vote" rule.

A critical error (wrong_fact matched to a must_not_err gold fact) is CONFIRMED
only when a second, independent, deterministic vote agrees that the claim is
fabricated rather than a frame collision with faithful history documentation:

- value / unit / time(frequency) mismatch: the claimed value must have NO
  anchor in the source transcript near the drug/entity (fuzzy, ASR-tolerant
  window search over digit and spelled-number variants). A value that does
  appear in the source (e.g. a historical or titration dose) demotes the crit
  to a frame dispute.
- status mismatch, claim says active while gold says stopped/past: demoted if
  the claim's own evidence quote carries past/trial morphology (the parser
  misread its own sentence).
- status mismatch, claim says stopped/past while gold says active: demoted if
  the transcript window around the drug carries stop/trial morphology (the
  history the claim documents really happened).
- polarity mismatch: demoted if either the quote carries past morphology or
  the transcript window carries stop/resolution morphology.

A crit is confirmed if ANY mismatched field survives demotion. Demoted crits
are reported as frame disputes (counted and annotated per claim) but do not
enter the pass rule; an optional LLM adjudication pass may re-examine them
when the evaluator provides a gateway.

Rationale: on 2026-08-31 all 17 raw crits of a frontier model were adjudicated
against the source transcripts and 17/17 were overturnable frame collisions
(initial doses, from-doses, past trials, resolved side effects, faithful
reports of a patient's misrecall). Under this layer a crit means "the claimed
value/assertion has no support anywhere in the source", which cannot be
overturned by any frame argument.
"""
import re

WINDOW_RADIUS = 300

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]

_TOKEN_STOP = {
    "patient", "continue", "continued", "daily", "before", "after", "meals",
    "with", "take", "takes", "taking", "tablet", "tablets", "capsule",
    "capsules", "milligram", "milligrams", "morning", "night", "bedtime",
    "evening", "once", "twice", "hold", "resume", "stopped", "started",
    "increase", "increased", "decrease", "decreased", "medication", "current",
    "history", "needed", "weekly",
}

_ACTIVE = {"active", "ordered", "present", "current"}
_PASTISH = {"past", "stop", "stopped", "historical", "resolved", "worsened"}

# Past/trial morphology inside the claim's own sentence: if present, a parser
# verdict of "active/positive" cannot be trusted enough to convict.
_PAST_QUOTE_RE = re.compile(
    r"\b(was|were|had|has been|have been|tried|took|caused|gave|made "
    r"(?:me|her|him|them)|stopped|stopping|quitting|discontinued|discontinuing|resolved|went away|used to|"
    r"previously|previous|prior|past|formerly|temporarily|switched back|back to|before pregnancy|history of|initially|at first|"
    r"completed|finished|tapered|used|felt|recalls?|recalled|remembers?|remembered|being (?:prescribed|given|started|tried)|were prescribed|"
    r"(?:increased|decreased|reduced|raised|lowered|changed|bumped|titrated|improved|worsened)"
    r"(?:\s+\S+){0,3}?\s+(?:from|to)\b|"
    r"back (?:in|then)|(?:years?|months?|weeks?|days?) ago|last "
    r"(?:year|month|week|spring|summer|fall|autumn|winter)|in the "
    r"(?:spring|summer|fall|autumn|winter)|no longer)\b", re.I)

# Explicit negation morphology inside the claim's own sentence: if present, a
# parser verdict of polarity=positive is a list-parse failure, not evidence.
_NEG_QUOTE_RE = re.compile(
    r"^\s*no\b|\bno (?:known|current|regular|new|other|significant|side effects?|issues?|problems?|complaints?|reactions?)\b"
    r"|\bdenies\b|\bdenied\b|\bwithout\b|\bnegative for\b|\bnone\b"
    r"|\bnot? (?:allerg|need|use|tak)", re.I)

# Hedged/conditional mentions ("consider X if ...", "could add Y, though")
# discuss an option; they assert neither status nor polarity.
_HEDGE_QUOTE_RE = re.compile(
    r"\b(consider(?:ing|ed)?|may|might|could|option(?:s|al)?|"
    r"if (?:needed|not|symptoms|no improvement)|would rather|though|"
    r"discuss(?:ed)? (?:as|the option)|versus|vs\.?)\b", re.I)

# Advice/instruction negation ("advised not to take X", "no ibuprofen
# because unsafe"): the negation is the CONTENT of advice, not a polarity
# assertion about the patient's state.
_ADVICE_NEG_RE = re.compile(
    r"\badvised? (?:to )?not\b|\badvised against\b|\bnot to (?:take|use|"
    r"exceed|combine|mix)\b|\bavoid(?:ing|ed)?\b|\bdo not\b|\bdon'?t\b|"
    r"\bshould not\b|\bno \w+ because\b|\bnot safe\b|\bnot recommended\b|"
    r"\bdoes not go away\b|\bnot overdo(?:ing)?\b|\boverdoing\b", re.I)

_CLOCK_RE = re.compile(r"^\s*\d{1,2}[:.]\d{2}\s*(?:am|pm)?\s*$"
                       r"|^\s*\d{1,2}\s*(?:am|pm)\s*$", re.I)

# Stop/trial/resolution morphology in the transcript window around the drug:
# if present, a claim documenting a stop/past frame has source support.
_STOP_WINDOW_RE = re.compile(
    r"\b(stopped|stopping|stop (?:it|that|this)|discontinued|came off|"
    r"off (?:it|that)|quit|no longer|not (?:on|taking)|tried (?:it|that)?|"
    r"trial of|switched (?:from|off|you to|to)|got stopped|no more|"
    r"went away|resolved|cleared up|(?:years?|months?|weeks?) ago|used to)\b",
    re.I)

_FREQ_PATTERNS = [
    ("bid", r"\btwice\s+(?:a\s+day|daily|per\s+day)\b|\btwo\s+times\s+a\s+day\b|\bb\.?i\.?d\.?\b"),
    ("tid", r"\bthree\s+times\s+(?:a\s+day|daily|per\s+day)\b|\bt\.?i\.?d\.?\b"),
    ("qid", r"\bfour\s+times\s+(?:a\s+day|daily|per\s+day)\b|\bq\.?i\.?d\.?\b"),
    ("qod", r"\bevery\s+other\s+day\b"),
    ("weekly", r"\bonce\s+a\s+week\b|\bweekly\b|\bevery\s+week\b|\bper\s+week\b|\b(?:\d+|one|two|three|four|five|six)(?:\s+or\s+(?:\d+|one|two|three|four|five|six))?\s+days?\s+a\s+week\b"),
    ("monthly", r"\b(?:once|twice|once or twice)\s+a\s+month\b|\bmonthly\b|\bper\s+month\b"),
    ("prn", r"\bas\s+needed\b|\bwhen\s+needed\b|\bp\.?r\.?n\.?\b|\bsometimes\b|\boccasionall?y\b|\bonce in a while\b"),
    ("qhs", r"\bat\s+bed\s?time\b|\bnightly\b|\bat\s+night\b|\bevery\s+night\b|\bq\.?h\.?s\.?\b"),
    ("qam", r"\bin\s+the\s+morning\b|\bevery\s+morning\b|\beach\s+morning\b"),
    ("qd", r"\bonce\s+(?:a\s+day|daily)\b|\bdaily\b|\bevery\s+day\b|\beach\s+day\b|\ba\s+day\b|\bq\.?d\.?\b"),
]

_UNIT_VARIANTS = {
    "mg": ["mg", "milligram", "milligrams"],
    "mcg": ["mcg", "microgram", "micrograms", "ug"],
    "g": ["g", "gram", "grams"],
    "ml": ["ml", "milliliter", "milliliters", "millilitre", "millilitres"],
    "units": ["unit", "units"],
    "%": ["percent", "%"],
}


def _int_words(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("" if not o else " " + _ONES[o])
    if n < 1000:
        h, r = divmod(n, 100)
        head = _ONES[h] + " hundred"
        return head if not r else head + " " + _int_words(r)
    if n < 10000:
        th, r = divmod(n, 1000)
        head = _ONES[th] + " thousand"
        return head if not r else head + " " + _int_words(r)
    return str(n)


def _num_variants(value):
    """All ways a dose number plausibly appears in an ASR transcript."""
    s = str(value).strip().lower().replace(",", "")
    out = {s}
    try:
        f = float(s)
    except ValueError:
        return out
    if f != int(f):  # decimal: "two point five" / "point zero three"
        whole, frac = s.split(".", 1)
        try:
            w = _int_words(int(whole))
        except ValueError:
            return out
        dwords = " ".join(_ONES[int(c)] for c in frac if c.isdigit())
        out.add(w + " point " + dwords)
        if int(whole) == 0:  # ASR drops the leading zero: "point zero three"
            out.add("point " + dwords)
            out.add("point " + dwords.replace("zero", "oh"))
        return out
    n = int(f)
    out.add(str(n))
    if n >= 1000:
        out.add("{:,}".format(n))
    if 0 <= n < 10000:
        w = _int_words(n)
        out.add(w)
        out.add(w.replace(" hundred ", " hundred and "))
        if 100 <= n < 1000:
            h, r = divmod(n, 100)
            if r:  # ASR grouped form: 325 -> "three twenty five"
                out.add(_ONES[h] + " " + _int_words(r))
            else:  # 300 -> "three hundred" already present
                pass
        if n == 1000:
            out.add("a thousand")
            out.add("thousand")
        if 1000 < n < 10000 and n % 100 == 0 and (n // 100) % 10 != 0:
            out.add(_int_words(n // 100) + " hundred")  # 1500 -> fifteen hundred
    return {v for v in out if v}


def _norm(text):
    text = text.lower().replace("-", " ")
    return re.sub(r"\s+", " ", text)


def _edit_le(a, b, maxd):
    """True if levenshtein(a, b) <= maxd (banded DP, early exit)."""
    if abs(len(a) - len(b)) > maxd:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > maxd:
            return False
        prev = cur
    return prev[-1] <= maxd


def _multi_drug(text_norm):
    try:
        from detfact_consensus import DRUG_NAMES
    except Exception:
        return False
    n = 0
    for d in DRUG_NAMES:
        d = str(d).lower()
        if len(d) >= 5 and d in text_norm:
            n += 1
            if n >= 2:
                return True
    return False


def _entity_tokens(claim, fact):
    toks = []
    for src in ((fact.get("fields") or {}).get("object"),
                (claim.get("fields") or {}).get("object")):
        for t in re.findall(r"[a-z]{5,}", _norm(str(src or ""))):
            if t not in _TOKEN_STOP and t not in toks:
                toks.append(t)
    return toks


_DRUG_POS_CACHE = {}


def _drug_positions(transcript_norm):
    """Positions of every known drug mention (topic-window boundaries)."""
    key = hash(transcript_norm)
    if key in _DRUG_POS_CACHE:
        return _DRUG_POS_CACHE[key]
    out = []
    try:
        from detfact_consensus import DRUG_NAMES
        toks = sorted({str(d).lower() for d in DRUG_NAMES if len(str(d)) >= 5})
        if toks:
            pat = re.compile("|".join(re.escape(t) for t in toks))
            out = [(m.start(), m.group(0)) for m in pat.finditer(transcript_norm)]
    except Exception:
        out = []
    _DRUG_POS_CACHE[key] = out
    return out


def fuzzy_windows(transcript_norm, tokens, radius=WINDOW_RADIUS,
                  topic_extend=True):
    """Transcript excerpts around exact or ASR-misspelled entity mentions.

    ASR conversations often state a dose long after the drug was last named,
    with no other drug in between. The forward edge therefore extends to the
    next mention of a DIFFERENT known drug (capped at 1500 chars) — a topic
    window — rather than a fixed radius.
    """
    spans = []
    for tok in tokens:
        found = [m.start() for m in re.finditer(re.escape(tok), transcript_norm)]
        if not found and len(tok) >= 5:
            maxd = 2 if len(tok) >= 7 else 1
            for m in re.finditer(r"[a-z]{4,}", transcript_norm):
                w = m.group(0)
                if w[:3] == tok[:3] and _edit_le(w, tok, maxd):
                    found.append(m.start())
        for pos in found:
            spans.append((pos, tok))
    boundaries = _drug_positions(transcript_norm) if topic_extend else []
    windows = []
    all_pos = sorted(set(spans))
    tok_positions = [p for p, _ in all_pos]
    # Same-topic aliases: a known drug that ever appears within 60 chars of
    # this entity is treated as another name for it (brand/generic pairs are
    # usually introduced appositively: "fluoxetine? prozac"), so it does not
    # cut the topic window.
    aliases = set()
    for bpos, bdrug in boundaries:
        if any(abs(bpos - p) < 60 for p in tok_positions):
            aliases.add(bdrug)
    for pos, tok in all_pos:
        end = pos + radius
        if topic_extend:
            nxt = None
            for bpos, bdrug in boundaries:
                if (bpos > pos + len(tok) and bdrug not in aliases
                        and tok[:5] not in bdrug and bdrug[:5] not in tok):
                    nxt = bpos
                    break
            cap = pos + 1500
            end = max(end, min(nxt, cap) if nxt is not None else cap)
        windows.append(transcript_norm[max(0, pos - radius): end])
    return windows


def _value_anchored(value, windows, unit=None):
    variants = _num_variants(value)
    unit_words = None
    if unit:
        unit_words = _UNIT_VARIANTS.get(str(unit).strip().lower())
    for win in windows:
        for v in variants:
            for m in re.finditer(r"(?<![\w.])" + re.escape(v) + r"(?![\w.])", win):
                if not unit_words:
                    return True
                tail = win[m.end(): m.end() + 30]
                if any(re.match(r"\s*\W*" + re.escape(u) + r"\b", tail, re.I)
                       for u in unit_words):
                    return True
    return False


def _freq_canon_set(text):
    out, rest = set(), text
    for canon, pat in _FREQ_PATTERNS:
        if re.search(pat, rest, re.I):
            out.add(canon)
            rest = re.sub(pat, " ", rest, flags=re.I)
    return out


def _freq_anchored(claim_time, windows):
    canon = _freq_canon_set(_norm(str(claim_time)))
    if not canon:
        return None  # not a frequency expression -> vote not applicable
    for win in windows:
        if canon & _freq_canon_set(win):
            return True
    return False


def confirm(claim, fact, mismatch_fields, transcript, factset=None):
    """Second vote on a candidate crit.

    Returns (confirmed: bool, detail: dict). confirmed=False means every
    mismatched field was demoted to a frame dispute.
    """
    cf = claim.get("fields") or {}
    quote = str(claim.get("evidence_quote") or "")
    qn_early = _norm(quote)
    # Headings ("**Allergies:**") assert nothing at all.
    if re.match(r"^\W*[\w /()&-]+:\s*\W*$", quote.strip()):
        return False, {"surviving": [],
                       "demoted": [{"field": "*",
                                    "reason": "heading_no_assertion"}]}
    # A bare noun phrase ("Remeron (Mirtazapine)") carries no frame of its
    # own — its parsed status is inherited from lost list context, so status
    # convictions on it are untrusted. It DOES assert existence (a note
    # listing a finding asserts it), so polarity/value convictions stand.
    _bare = len(re.findall(r"[a-z]+", qn_early)) <= 4 and not re.search(
        r"\b(is|are|was|were|takes?|taking|reports?|denies|denied|"
        r"continues?|started|stopped|has|have|had|no|not)\b", qn_early)
    # Alias/terminology statements ("mirtazapine is also called Remeron")
    # assert naming, not status.
    _alias = bool(re.search(r"\b(?:also (?:called|known as)|a\.?k\.?a\.?)\b",
                            qn_early))
    tnorm = _norm(transcript)
    windows = fuzzy_windows(tnorm, _entity_tokens(claim, fact))
    if not windows:
        # Entity never appears in the source, even fuzzily (or a brand/generic
        # alias we cannot resolve deterministically): fall back to the whole
        # transcript so an alias cannot manufacture an unfalsifiable crit.
        windows = [tnorm]
    surviving, demoted = [], []
    for mm in mismatch_fields:
        field = mm.get("field")
        demote = None
        if field in ("value",):
            val = cf.get("value")
            # Sibling-frame value match: if another gold fact under the
            # same anchor carries exactly the claimed value, the claim hit
            # the wrong sibling frame — a matcher artifact, not an error.
            if factset is not None and val not in (None, ""):
                _anchor_base = ".".join(
                    str(fact.get("canonical_anchor") or "").split(".")[:3])
                for _of in factset.get("facts", []):
                    if _of is fact:
                        continue
                    if ".".join(str(_of.get("canonical_anchor") or "")
                                .split(".")[:3]) != _anchor_base:
                        continue
                    if str((_of.get("fields") or {}).get("value")) == str(val):
                        demoted.append({"field": field,
                                        "reason": "sibling_value_match"})
                        break
                else:
                    _of = None
                if demoted and demoted[-1]["reason"] == "sibling_value_match":
                    continue
            # A value the parser claims but which does not literally appear
            # in the evidence sentence is a parse artifact ("300" read as
            # "30"), never evidence.
            if val not in (None, "") and not any(
                    re.search(r"(?<![\w.])" + re.escape(v) + r"(?![\w.])",
                              _norm(quote))
                    for v in _num_variants(val)):
                demoted.append({"field": field,
                                "reason": "value_not_in_quote"})
                continue
            _sv = re.escape(str(val or ""))
            if _sv and re.search(
                    r"\bfrom\s+" + _sv + r"(?:\s*/\s*10)?\s*(?:to|down to|"
                    r"up to|→)\b", quote, re.I):
                # Explicit from→to construction: the from-side value is
                # historical by the sentence's own grammar.
                demoted.append({"field": field, "reason": "from_side_value"})
                continue
            try:
                if float(str(val)) == 0:
                    # No real dose is 0: a zero value is a formatting/parse
                    # artifact ("1 000 mg" thousands-space), never evidence.
                    demoted.append({"field": field,
                                    "reason": "zero_value_artifact"})
                    continue
            except (TypeError, ValueError):
                pass
            fval = str(mm.get("fact") if mm.get("fact") is not None
                       else (fact.get("fields") or {}).get("value") or "")
            cnum = None
            try:
                cnum = float(str(val))
            except (TypeError, ValueError):
                pass
            rng = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*$",
                           fval)
            if rng and cnum is not None and \
                    float(rng.group(1)) <= cnum <= float(rng.group(2)):
                # Gold value is a range that contains the claimed value:
                # under-specific, not a contradiction.
                demote = "value_within_gold_range"
                demoted.append({"field": field, "reason": demote})
                continue
            fnum = None
            try:
                fnum = float(fval)
            except ValueError:
                pass
            if not rng and (cnum is None) != (fnum is None):
                # Numeric vs non-numeric gold value ("always"): the two are
                # not comparable; conviction impossible.
                demoted.append({"field": field,
                                "reason": "value_type_incomparable"})
                continue
            unit = cf.get("unit") or (fact.get("fields") or {}).get("unit")
            unitless = False
            if not unit:
                try:
                    float(str(val))
                    unitless = True
                except (TypeError, ValueError):
                    pass
            if unitless:
                # Unitless values (pain scales, ratings, counts) collide with
                # durations ("six weeks", "twelve months") in wide windows.
                # Exonerate only on a tight adjacency anchor next to the
                # entity itself.
                tnorm2 = _norm(transcript)
                tight = fuzzy_windows(tnorm2, _entity_tokens(claim, fact),
                                      radius=60, topic_extend=False)
                if tight and _value_anchored(val, tight):
                    demote = "value_anchored_in_source"
            elif _value_anchored(val, windows):
                demote = "value_anchored_in_source"
            else:
                # Enumeration attribution: the sentence names several drugs
                # ("estradiol 10 mg, testosterone, and progesterone 300 mg")
                # and the matcher may bind one drug's value to another's
                # fact. If the disputed value anchors in the topic window of
                # ANY OTHER drug named in the same sentence, attribution is
                # ambiguous -> dispute lane.
                try:
                    from detfact_consensus import DRUG_NAMES
                    qn = _norm(quote)
                    fact_toks = set(_entity_tokens(claim, fact))
                    others = [d.lower() for d in DRUG_NAMES
                              if len(str(d)) >= 5 and d.lower() in qn
                              and d.lower() not in fact_toks]
                    if others:
                        tn3 = _norm(transcript)
                        owin = fuzzy_windows(tn3, sorted(set(others))[:6])
                        # No unit adjacency required: speech separates numbers from units
                        if owin and _value_anchored(val, owin):
                            demote = "enumeration_value_attribution"
                except Exception:
                    pass
        elif field == "unit":
            if _value_anchored(cf.get("value"), windows, unit=cf.get("unit")):
                demote = "value_unit_anchored_in_source"
        elif field == "time":
            if _PAST_QUOTE_RE.search(quote):
                # Historical/temporary regimen sentences: their frequency
                # describes a past arrangement, not the current schedule.
                demoted.append({"field": field,
                                "reason": "quote_past_morphology"})
                continue
            c_time = str(mm.get("claim") or cf.get("time") or "")
            f_time = str(mm.get("fact") or "")
            if "prn" in _freq_canon_set(_norm(quote)):
                # The sentence itself is an as-needed usage description
                # ("PRN, 3-4 days a week, once those days"): its frequency
                # words describe usage pattern, not a fixed regimen — a
                # dimension mix the deterministic layer cannot convict on.
                demote = "prn_usage_pattern"
            elif _CLOCK_RE.match(c_time) or _CLOCK_RE.match(f_time):
                # Clock-of-day values are tiny numbers the deterministic
                # layer cannot safely convict on; dispute lane decides.
                demote = "clock_time_dispute"
            else:
                c_canon = _freq_canon_set(_norm(c_time))
                f_canon = _freq_canon_set(_norm(f_time))
                if "prn" in (c_canon | f_canon) and c_canon != f_canon:
                    # PRN and a rate ("once or twice a month as needed")
                    # describe the same regimen; not a contradiction.
                    demote = "prn_rate_compatible"
                else:
                    fa = _freq_anchored(c_time, windows)
                    if fa is True:
                        demote = "frequency_anchored_in_source"
                    elif fa is None and _value_anchored(c_time, windows):
                        demote = "time_value_anchored_in_source"
        elif field == "status":
            c_stat = str(mm.get("claim") or cf.get("status") or "").lower()
            if _HEDGE_QUOTE_RE.search(quote):
                demote = "hedged_conditional_mention"
            elif " including " in qn_early and _multi_drug(qn_early):
                # "longstanding use of ... including A, B, C": a summarizing
                # enumeration carries no per-item status assertion.
                demote = "enumeration_status_untrusted"
            elif _alias:
                demote = "alias_statement"
            elif _bare:
                demote = "bare_mention_status_untrusted"
            elif _NEG_QUOTE_RE.search(quote):
                # Explicit negation sentences ("No known drug allergies other
                # than...") get unreliable status parses; same reasoning as
                # the polarity list-parse guard.
                demote = "quote_negation_morphology"
            elif c_stat in _ACTIVE and _PAST_QUOTE_RE.search(quote):
                demote = "quote_past_morphology"
            elif c_stat in _PASTISH:
                # Past-tense status sentences are exactly the surface the
                # parser proved unreliable on; sentence-level tense cannot
                # convict deterministically. Route to dispute (LLM pass).
                if any(_STOP_WINDOW_RE.search(w) for w in windows):
                    demote = "stop_supported_in_source"
                elif _PAST_QUOTE_RE.search(quote):
                    demote = "quote_past_morphology"
        elif field == "polarity":
            c_pol = str(mm.get("claim") or cf.get("polarity") or "").lower()
            if _HEDGE_QUOTE_RE.search(quote):
                demote = "hedged_conditional_mention"
            elif c_pol == "negative" and _ADVICE_NEG_RE.search(quote):
                demote = "advice_negation"
            elif (c_pol == "negative"
                  and (_wm := re.search(r"\bwithout\s+([a-z]{4,})", qn_early))
                  and _wm.group(1) not in set(re.findall(
                      r"[a-z]{5,}", _norm(str((fact.get("fields") or {})
                                              .get("object") or ""))))):
                # "melatonin (helps ... without trazodone)": the negation
                # scopes a different entity, not this claim's anchor.
                demote = "negation_scopes_other_entity"
            elif c_pol == "negative" and re.search(
                    r"\b(?:no|not|without)\s+(?:any\s+)?(?:change|changes|"
                    r"modification|modifications|adjustment|adjustments)\b",
                    qn_early):
                # "continue ... without any changes": the negation scopes the
                # change-word, not the entity — a continuation idiom, not a
                # denial.
                demote = "negation_scopes_change_word"
            elif c_pol == "positive" and _NEG_QUOTE_RE.search(quote):
                # The sentence itself is an explicit negation ("No known
                # allergy to..."): polarity=positive is a list-parse
                # failure on the claim side, not evidence.
                demote = "quote_negation_morphology"
            elif _PAST_QUOTE_RE.search(quote):
                # Past-framed sentence (resolved side effect, prior trial):
                # faithful-history surface in both directions.
                demote = "quote_past_morphology"
            elif c_pol == "negative" and any(
                    re.search(r"\bwithout\b|\bno\s|\bnot\b|\bdenie[sd]\b", w) and any(
                        t in w for t in _entity_tokens(claim, fact))
                    for w in windows) and re.search(
                        r"\bwithout\b|\bno\s|\bnot\b|\bdenie[sd]\b", qn_early):
                # The negation phrasing itself is anchored in the source near
                # this entity ("sleeps just fine without trazodone"): the
                # note repeats the source's own negation.
                demote = "negation_anchored_in_source"
            elif c_pol == "negative" and any(
                    _STOP_WINDOW_RE.search(w) for w in windows):
                # Model negates; source has stop/resolution language nearby.
                # The reverse direction (asserting a denied finding) is a
                # real error and is never exonerated by stop language.
                demote = "stop_supported_in_source"
        elif field == "object":
            # Object-level mismatches were never a working conviction channel
            # (drug swaps are caught by the near-miss/fabrication alarms);
            # matcher noise here goes to the dispute lane.
            demote = "object_field_dispute"
        if demote:
            demoted.append({"field": field, "reason": demote})
        else:
            surviving.append(field)
    return bool(surviving), {"surviving": surviving, "demoted": demoted}
