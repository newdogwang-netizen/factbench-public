#!/usr/bin/env python3
"""detfact consensus aligner (deterministic, no LLM): merges multi-model claims into gold candidates plus an adjudication worklist.
Usage: python3 detfact_consensus.py --gen gold_candidates/<case> --case-meta <cases dir>/<case>/meta.json --out audit_site/data/<case>.json
"""
import argparse, collections, json, os, re, unicodedata

MODEL_ORDER = [
    "kimi-k3",
    "deepseek-v4-flash",
    "glm-5p2",
    "qwen3-max",
    "minimax-m3",
    "gpt-5.6-sol",
    "gpt-5.4",
]
FIELDS = ["subject", "predicate", "object", "value", "unit", "time",
          "location", "owner", "status", "polarity", "condition"]
STABLE_FIELDS = {"subject", "object", "polarity"}
# Generic subject aliases only. Case-specific aliases (e.g. patient names) are
# PHI and must be supplied via a local file (override path with
# DETFACT_SUBJECT_ALIASES_FILE); no such file ships with this repo.
GENERIC_SUBJECT_ALIASES = {"patient", "member", "client", "self", "paciente",
                           # patient role words in pediatric scenarios
                           "infant", "baby", "newborn", "toddler", "child",
                           "adolescent", "teen", "bebe", "nino", "nina"}

def _load_local_subject_aliases():
    path = os.environ.get(
        "DETFACT_SUBJECT_ALIASES_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "subject_aliases.json"))
    try:
        with open(path) as fh:
            return {re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", str(x).lower())).strip()
                    for x in json.load(fh)}
    except Exception:
        return set()

PATIENT_SUBJECT_ALIASES = GENERIC_SUBJECT_ALIASES | _load_local_subject_aliases()

# Per-case dynamic patient aliases: derived from the case's own demographic/name
# facts, so that when an extraction model uses the patient's name as the subject it
# still normalizes to "patient".
CASE_SUBJECT_ALIASES = set()


def set_case_subject_aliases(names):
    global CASE_SUBJECT_ALIASES
    out = set()
    for n in names or []:
        k = word_key(n)
        if not k:
            continue
        out.add(k)
        for tok in k.split(" "):
            if len(tok) > 1:
                out.add(tok)
    CASE_SUBJECT_ALIASES = out


def derive_patient_names(rows):
    """rows: a list of claims or facts; takes the object value of demographic name entries."""
    names = []
    for r in rows or []:
        kind = norm_tok(r.get("kind") or r.get("canonical_kind") or "")
        key = norm_tok(r.get("key") or "")
        fields_ = r.get("fields") or {}
        pred = norm_tok(fields_.get("predicate") or "")
        if KIND_GROUP.get(kind, kind) != "demo" and "demo" not in kind:
            continue
        if pred == "name" or "name" in key or "nombre" in key:
            v = fields_.get("object") or fields_.get("value")
            if v:
                names.append(str(v))
        # the subject of a demo fact is usually the patient themselves ("kelsey has age 34")
        subj = fields_.get("subject")
        if subj and word_key(subj) not in GENERIC_SUBJECT_ALIASES:
            names.append(str(subj))
    return names
POLARITY_MAP = {
    "affirmative": "positive", "yes": "positive", "present": "positive",
    "positive": "positive", "no": "negative", "absent": "negative",
    "negative": "negative", "denied": "negative",
}
MONTHS = {
    "jan": "jan", "january": "jan",
    "feb": "feb", "february": "feb",
    "mar": "mar", "march": "mar",
    "apr": "apr", "april": "apr",
    "may": "may",
    "jun": "jun", "june": "jun",
    "jul": "jul", "july": "jul",
    "aug": "aug", "august": "aug",
    "sep": "sep", "sept": "sep", "september": "sep",
    "oct": "oct", "october": "oct",
    "nov": "nov", "november": "nov",
    "dec": "dec", "december": "dec",
}

def tag(model):
    return re.sub(r"[^0-9A-Za-z_-]+", "_", model)

UNITS = {"milligram": "mg", "milligrams": "mg", "mg.": "mg", "mg": "mg",
         "microgram": "mcg", "micrograms": "mcg", "mcg": "mcg", "ug": "mcg",
         "µg": "mcg", "gram": "g", "grams": "g", "g": "g",
         "milliliter": "ml", "milliliters": "ml", "ml": "ml",
         "liter": "l", "liters": "l", "l": "l",
         "unit": "units", "units": "units", "iu": "units",
         "percent": "%", "%": "%"}
STATUS_MAP = {
    "ongoing": "active", "current": "active", "currently": "active",
    "continues": "active", "continuing": "active", "continue": "active",
    "active": "active", "taking": "active", "takes": "active",
    "present": "present", "reported": "present", "reports": "present",
    "ordered": "ordered", "instructed": "ordered", "recommended": "ordered",
    "prescribed": "ordered", "advised": "ordered", "scheduled": "planned",
    "planned": "planned", "booked": "planned",
    "stopped": "stop", "discontinued": "stop", "ceased": "stop", "stop": "stop",
    "resolved": "resolved",
    "completed": "done", "done": "done", "performed": "done",
    "occurred": "done", "happened": "done",
    "improved": "improved", "improving": "improved", "better": "improved",
    "worse": "worsened", "worsened": "worsened", "worsening": "worsened",
    "increased": "increased", "decreased": "decreased", "reduced": "decreased",
}
KIND_GROUP = {
    "medication": "med", "medication_change": "med",
    "treatment": "instruct", "education": "instruct", "psychotherapy": "instruct",
    "followup_appointment": "followup", "referral": "referral",
    "symptom": "symptom", "diagnosis": "diagnosis",
    "lab_result": "lab", "vital": "vital", "test_ordered": "test",
    "demographic": "demo", "allergy": "allergy",
    "risk": "risk", "safety_plan": "safety",
    "family_history": "famhist", "social_history": "sochist",
    "consent": "consent", "other": "other",
}
STOP = {"the", "a", "an", "of", "in", "on", "at", "for", "to", "with", "and", "or"}
LEMMA = {"swollen": "swell", "swelling": "swell", "ankles": "ankle", "appointments": "appointment",
         "referrals": "referral", "diagnoses": "diagnosis", "medications": "medication",
         "supplements": "supplement", "tests": "test", "results": "result", "doses": "dose",
         "levels": "level", "symptoms": "symptom", "concerns": "concern", "weeks": "week",
         "days": "day", "months": "month", "years": "year", "hours": "hour", "minutes": "minute",
         "times": "time", "side effects": "side effect", "problems": "problem", "notes": "note",
         "logs": "log", "children": "child", "parents": "parent", "adults": "adult",
         "patients": "patient"}

def lem_tokens(s):
    out = []
    for w in s.split(" "):
        if not w:
            continue
        w2 = LEMMA.get(w)
        if w2:
            out.append(w2)
            continue
        # do not singularize after a vowel-ish stem char: keeps proper names
        # ("ramos") and Latin terms ("pectoris", "status") intact
        if len(w) > 3 and w.endswith("s") and w[-2] not in "siou":
            out.append(w[:-1])
        else:
            out.append(w)
    return " ".join(out)

def norm_tok(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[.。]+$", "", s)
    return s

def norm_dateish(s):
    raw = str(s or "").strip().lower()
    m = re.fullmatch(r"([0-9]{1,2})([a-z]{3,9})([0-9]{4})", raw)
    if m and m.group(2) in MONTHS:
        return "{}{}{}".format(int(m.group(1)), MONTHS[m.group(2)], m.group(3))
    m = re.fullmatch(r"([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{2,4})", raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if len(year) == 2:
            year = "19" + year if int(year) > 30 else "20" + year
        names = ["", "jan", "feb", "mar", "apr", "may", "jun",
                 "jul", "aug", "sep", "oct", "nov", "dec"]
        if 1 <= month <= 12:
            return "{}{}{}".format(day, names[month], year)
    return ""

SPELLED_NUM = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,
    "eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,
    "fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,
    "nineteen":19,"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,
    "seventy":70,"eighty":80,"ninety":90,"hundred":100,"thousand":1000,
}

def spelled_to_number(t):
    """'three hundred'→300, 'twenty five'→25; returns None when unparseable."""
    toks = [w for w in t.replace("-", " ").split() if w]
    if not toks or any(w not in SPELLED_NUM for w in toks):
        return None
    total = cur = 0
    for w in toks:
        v = SPELLED_NUM[w]
        if v in (100, 1000):
            cur = max(cur, 1) * v
            if v == 1000:
                total += cur; cur = 0
        else:
            cur += v
    return total + cur


def norm_val(s):
    if s is None:
        return ""
    d = norm_dateish(s)
    if d:
        return d
    t = norm_tok(s)
    # "1 gram"/"25 mg"/"4 mg/0.1ml": gold values with leftover unit words normalize to a bare number
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(milligrams?|grams?|micrograms?|milliliters?"
                 r"|mg|mcg|g|ml|tablets?|tabs?|units?)(?:\s*/\s*[\d.]*\s*(?:ml|l))?$", t)
    if m:
        t = m.group(1)
    else:
        # "two caps": spelled-out number + container word → bare number
        m2 = re.match(r"^([a-z][a-z ]*?)\s+(caps?|capsules?|tabs?|tablets?|pills?)$", t)
        if m2:
            n2 = spelled_to_number(m2.group(1))
            if n2 is not None:
                t = str(n2)
    n = spelled_to_number(t)
    if n is not None:
        return str(n)
    try:
        f = float(t.replace(",", ""))
        if f == int(f):
            return str(int(f))
        return (("%g") % f)
    except Exception:
        return t

def norm_field(name, v):
    if v is None:
        return ""
    if name == "subject":
        t = word_key(v)
        if t in PATIENT_SUBJECT_ALIASES or t in CASE_SUBJECT_ALIASES:
            return "patient"
        return norm_tok(v)
    if name == "unit":
        t = norm_tok(v)
        # "mg/0.1ml" concentration units: keep the mass unit (the number is compared in the value field)
        m = re.match(r"^(mg|mcg|g)\s*/\s*[\d.]*\s*m?l$", t)
        if m:
            t = m.group(1)
        if t not in UNITS and " " in t:
            # "twenty five milligrams": strip number words that leaked into the unit
            # field, then normalize; "po daily": route/frequency are not units — if
            # nothing remains after stripping, treat as having no unit
            toks = [w for w in t.split() if w not in SPELLED_NUM
                    and not re.match(r"^\d+(?:\.\d+)?$", w)]
            if toks and " ".join(toks) in UNITS:
                t = " ".join(toks)
        if t not in UNITS:
            toks = [w for w in t.split()
                    if w not in {"po", "oral", "orally", "iv", "im", "subq", "topical",
                                 "eye", "daily", "nightly", "weekly", "bid", "tid",
                                 "qid", "prn", "qhs", "gtt"}]
            t = " ".join(toks)
        return UNITS.get(t, t)
    if name == "status":
        return STATUS_MAP.get(norm_tok(v), norm_tok(v))
    if name == "value":
        return norm_val(v)
    if name == "time":
        t = norm_tok(v)
        t = re.sub(r"^(in the|at the|the|on|at|in|by)\s+", "", t)
        t = re.sub(r"\b(once|twice|three times|[0-9]+ times)\s+(a|per)\s+day\b",
                   lambda m: m.group(1) + " daily", t)
        t = re.sub(r"\bas needed\b", "prn", t)
        t = re.sub(r"\s+prn$", "", t)
        t = re.sub(r"^prn\s+", "", t) or t
        return t
    if name == "kind":
        return KIND_GROUP.get(norm_tok(v), norm_tok(v))
    if name == "object":
        return norm_object(v)
    if name == "polarity":
        return POLARITY_MAP.get(norm_tok(v), norm_tok(v))
    return norm_tok(v)

CLEAN_DROP = {"mg", "mcg", "g", "ml", "l", "kg", "tablet", "tablets", "capsule",
              "capsules", "pill", "pills", "day", "days", "week", "weeks", "month",
              "months", "year", "years", "hour", "hours", "daily", "weekly", "monthly",
              "once", "twice", "today", "tomorrow", "yesterday", "every", "per", "next",
              "last", "patient", "patients", "doctor", "clinician", "the", "a", "an",
              "not", "no", "without", "feeling", "feel", "feels", "felt", "as", "on",
              "off", "has", "have", "had", "is", "are", "was", "were", "be", "being"}

def clean_tokens(s):
    s = s.replace("'s", " ").replace("\u2019s", " ")
    s = re.sub(r"[-/+]+", " ", s)
    s = re.sub(r"[0-9]+([.,][0-9]+)?\s*[a-z%]*", " ", s)
    out = []
    for t in s.split(" "):
        t = t.strip(" .%")
        if not t or t in CLEAN_DROP:
            continue
        out.append(t)
    return " ".join(out)

PRED_MAP = {"experiences": "has", "experience": "has", "experiencing": "has",
            "reports": "has", "reported": "has", "reporting": "has",
            "notes": "has", "noted": "has", "denies": "has", "denied": "has",
            "endorses": "has", "endorsed": "has", "states": "has", "stated": "has",
            "mentions": "has", "mentioned": "has", "has": "has"}

GENERIC_ANCHORS = {"medication", "medicine", "symptom", "problem", "issue", "condition",
                   "appointment", "followup", "follow up", "lab", "labs", "test",
                   "result", "history", "procedure", "evaluation", "treatment",
                   "therapy", "referral", "pain", "dose change", "medication adherence",
                   "medication effect"}

DRUG_NAMES = {"vyvanse", "effexor", "venlafaxine", "adderall", "ritalin", "lexapro",
              "zoloft", "prozac", "wellbutrin", "bupropion", "metformin",
              "atorvastatin", "amlodipine", "lisinopril", "gabapentin",
              "trazodone", "melatonin", "xanax", "alprazolam", "clonazepam",
              "klonopin", "hydroxyzine", "hydralazine", "celebrex", "celexa", "zyrtec", "zantac", "clomiphene", "clomipramine", "risperidone", "ropinirole", "buspirone", "ozempic", "insulin",
              "abilify", "aripiprazole", "strattera", "atomoxetine", "wegovy",
              "semaglutide", "warfarin", "artificial tears", "artificial tear",
              "aspirin", "cyanocobalamin", "glucotrol xl", "icee hot", "invokana",
              # Dictionary extension 2026-08-27: real drugs/supplements missing from
              # gold med entities (manually screened, dirty gold terms removed;
              # explicitly frozen, auditable, applied uniformly to all models)
              "buprenorphine", "buspar", "clonidine", "concerta", "coq10", "pqq",
              "curcuplex 95", "dhea", "diphenhydramine", "duloxetine", "ertapenem",
              "fluoxetine", "hydrocodone", "ibuprofen", "keppra", "lamictal",
              "levothyroxine", "magnesium glycinate", "magnesium", "methocarbamol",
              "methylene blue", "nac", "n-acetylcysteine", "naloxone", "nebivolol",
              "olanzapine", "pariet", "potassium gluconate", "prenatal",
              "probiotic", "progesterone", "propranolol", "ramelteon",
              "ranolazine", "risperdal", "risperidone", "sennosides", "senna",
              "docusate", "seroquel", "sertraline", "tamsulosin", "tums",
              "tylenol", "vistaril", "vitamin c", "vitamin d", "vitamin b12",
              "zinc", "zepbound", "birth control", "green tea", "omega-3",
              "fish oil", "ferrous sulfate", "ferrous gluconate", "citrulline",
              "hydromorphone", "dilaudid", "apixaban", "hydrocortisone",
              "mirtazapine", "lamotrigine", "divalproex", "escitalopram",
              "quetiapine", "latuda", "fenbendazole", "letrozole", "estradiol",
              "testosterone", "methadone", "invega", "valium", "diazepam"}

ALIAS_RULES = [
    ("demo", "date_of_birth", "date of birth",
     ("date of birth", "birth date", "dob", "dob nac", "fecha de nacimiento")),
    ("demo", "age", "age",
     ("demographic age", "has age", "tiene sesenta", "60 years")),
    ("sleep", "insomnia", "insomnia / sleep difficulty",
     ("insomnia", "difficulty sleeping", "sleep difficulty", "trouble sleeping",
      "not sleeping", "cannot sleep", "can't sleep", "sleepless", "no puedo dormir",
      "dificultad para dormir", "problema de sueno", "problemas de sueno",
      "bad sleep", "poor sleep", "pretty bad sleep")),
    ("activation", "edginess", "edginess / feeling on edge",
     ("on edge", "edgy", "edginess", "edge", "activated", "activation")),
    ("mood_anxiety", "anxiety", "anxiety",
     ("anxiety", "anxious", "panic", "worry", "nervous", "ansiedad",
      "panico", "ataque de panico")),
    ("mood_depression", "depression", "depression / low mood",
     ("depression", "depressed", "low mood", "sadness", "anhedonia",
      "depresion", "tristeza", "major depressive disorder", "depressive disorder")),
    ("withdrawal_sensation", "brain_zaps", "brain zaps",
     ("brain zap", "brain zaps", "electric shock", "zaps")),
    ("cognition_memory", "memory_impairment", "memory impairment",
     ("memory", "forgetfulness", "forgetting", "forgets", "forgot", "recall",
      "memoria", "retentiva", "retengo", "olvido", "olvidar", "olvida")),
    ("cognition_language", "word_finding", "word finding / expressive language",
     ("word finding", "expressive language", "finding words", "language",
      "encontrar las palabras", "encontrar la palabra", "palabra correcta")),
    ("cognition_attention", "attention_concentration", "attention / concentration",
     ("attention", "concentration", "focus", "distract")),
    ("pain", "pain", "pain",
     ("pain", "ache", "dolor", "dolor de espalda")),
    ("cardiovascular", "blood_pressure", "blood pressure",
     ("blood pressure", "hypertension", "hipertension", "presion arterial",
      "alta presion")),
    ("gastrointestinal", "constipation", "constipation",
     ("constipation", "constipacion", "estrenimiento")),
    ("gastrointestinal", "diarrhea", "diarrhea",
     ("diarrhea", "diarrea")),
    ("gastrointestinal", "vomiting", "vomiting",
     ("vomiting", "vomit", "vomito", "vomita")),
    ("neuro_vestibular", "dizziness", "dizziness and giddiness",
     ("dizziness", "giddiness", "mareo", "mareos")),
    ("metabolic", "weight", "weight",
     ("weight", "peso")),
    ("med_adherence", "medication_adherence", "medication adherence",
     ("adherence", "compliance", "taking medication", "missed medication",
      "forgets medication", "stopped taking")),
    ("med_dose_change", "dose_change", "dose change",
     ("dose decrease", "dose decreased", "decrease dose", "reduced dose",
      "dose increase", "increased dose", "changed dose")),
    ("med_effect", "medication_effect", "medication effect",
     ("side effect", "effect", "after medication", "since medication")),
    ("family_event", "death_by_suicide", "death by suicide",
     ("suicide", "died by suicide", "death by suicide")),
    ("social_context", "living_arrangement", "living arrangement",
     ("living arrangement", "lives with", "living with")),
    ("assistive_device", "lasa", "Lasa support device",
     ("lasa for support", "a lasa", "support device")),
]

def ascii_fold(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")

def word_key(s):
    s = ascii_fold(s).lower()
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def slug(s):
    s = word_key(s)
    return re.sub(r"\s+", "_", s).strip("_")

def has_terms(blob, phrase):
    toks = [t for t in word_key(phrase).split(" ") if t]
    if not toks:
        return False
    padded = " " + blob + " "
    return all((" " + t + " ") in padded for t in toks)

def strip_dx_code(s):
    s = norm_tok(s)
    s = re.sub(r"\s*\(([a-z]\d{1,3}(?:\.\d+)?)\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+[a-z]\d{1,3}(?:\.\d+)?\s*$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,;")

OBJECT_CANON_RULES = [
    ("spouse and son", ("husband and son", "her husband and her son", "spouse and son",
                        "live with spouse and son", "lives with spouse and son",
                        "live with her husband and her son")),
    ("dark mood", ("dark mood", "glum and angry")),
    ("sleep difficulty", ("pretty bad sleep", "bad sleep", "poor sleep", "sleep difficulty",
                          "insomnia", "sleep disturbance")),
    ("on edge", ("on edge", "as on edge", "not feeling on as edge", "edgy", "edginess",
                 "bright in teeth", "bright in his teeth")),
    ("anger", ("anger bubble", "random anger", "just being angry", "indignant rage")),
    ("depression", ("depression", "depressed", "depresion", "depressive", "sadness",
                    "really sad", "major depression", "depresion mayor", "depresion cronica")),
    ("anxiety", ("anxiety", "anxious", "panic", "ataque de panico", "panico")),
    ("memory impairment", ("memory", "retentiva", "no retengo", "olvido", "forgetting",
                           "forgetfulness", "recent event", "recent information",
                           "informacion reciente", "forgets")),
    ("word finding", ("word finding", "difficulty finding word", "encontrar las palabra",
                      "encontrar las palabras", "palabra correcta", "palabras correctas",
                      "expresar pensamiento", "expresar pensamientos")),
    ("blood pressure", ("blood pressure", "hypertension", "hipertension", "alta presion",
                        "presion arterial")),
    ("pain", ("pain", "dolor", "back pain", "dolor de espalda")),
    ("diarrhea", ("diarrhea", "diarrea")),
    ("vomiting", ("vomiting", "vomit", "vomito")),
    ("constipation", ("constipation", "constipacion", "estrenimiento")),
    ("dizziness and giddiness", ("dizziness and giddiness", "dizziness", "giddiness", "mareo")),
    ("insomnia", ("insomnia unspecified", "insomnia")),
    ("osteoarthritis", ("osteoarthritis", "osteoartritis")),
    ("polyneuropathy", ("polyneuropathy", "polineuropatia")),
    ("vitamin deficiency", ("vitamin deficiency", "deficiencia vitaminica", "vitamina")),
    ("lasa", ("lasa for support", "a lasa", "lasa")),
]

def norm_object(v):
    raw = strip_dx_code(v)
    d = norm_dateish(raw)
    if d:
        return d
    folded = word_key(raw)
    if not folded:
        return ""
    for canon, phrases in OBJECT_CANON_RULES:
        if any(has_terms(folded, p) for p in phrases):
            return canon
    for name in sorted(DRUG_NAMES, key=len, reverse=True):
        if has_terms(folded, name):
            return name
    return lem_tokens(raw)

def claim_blob(kind, claim):
    f = claim.get("fields") or {}
    parts = [kind or "", claim.get("key", "")]
    parts += [str(f.get(x) or "") for x in FIELDS]
    return word_key(" ".join(parts))

def token_anchor(raw):
    toks = lem_tokens(clean_tokens(word_key(raw))).split()
    toks = [t for t in toks if t and t not in GENERIC_ANCHORS]
    if not toks:
        toks = [t for t in clean_tokens(word_key(raw)).split() if t]
    return " ".join(toks[:8])

def alias_anchor(blob):
    for subtype, anchor, display, phrases in ALIAS_RULES:
        if any(has_terms(blob, p) for p in phrases):
            return subtype, anchor, display
    return None, "", ""

def drug_anchor(blob):
    for name in sorted(DRUG_NAMES, key=len, reverse=True):
        if has_terms(blob, name):
            return name
    return ""

def fallback_subtype(group, blob, anchor):
    if group == "symptom":
        if any(has_terms(blob, x) for x in ("memory", "forget", "word finding",
                                            "attention", "concentration", "cognitive")):
            return "cognition"
        if any(has_terms(blob, x) for x in ("sleep", "insomnia")):
            return "sleep"
        if any(has_terms(blob, x) for x in ("mood", "anger", "irritable", "depression",
                                            "anxiety", "aggressive")):
            return "mood_behavior"
        if any(has_terms(blob, x) for x in ("pain", "ache", "dolor")):
            return "pain"
        return "symptom"
    if group == "med":
        if any(has_terms(blob, x) for x in ("dose", "increase", "decrease", "reduced")):
            return "med_dose_change"
        if any(has_terms(blob, x) for x in ("adherence", "compliance", "taking", "missed")):
            return "med_adherence"
        if any(has_terms(blob, x) for x in ("effect", "side effect")):
            return "med_effect"
        return "medication"
    if group in {"followup", "referral", "test"}:
        return "care_plan"
    if group in {"demo", "famhist", "sochist"}:
        return group
    return anchor.split(" ")[0] if anchor else group

def condition_anchor(f, blob):
    cond = word_key(f.get("condition"))
    if "icd" in cond or cond in {"code", "diagnosis code"}:
        return ""
    if cond in {"adhd"}:
        return ""
    full = (cond + " " + blob).strip()
    change_terms = ("decrease", "decreased", "reduced", "lower", "lowered",
                    "increase", "increased", "higher", "raised",
                    "start", "started", "begin", "began",
                    "stop", "stopped", "discontinue", "discontinued")
    if not cond and not any(has_terms(full, x) for x in change_terms):
        return ""
    med = ""
    for name in sorted(DRUG_NAMES, key=len, reverse=True):
        if has_terms(full, name):
            med = name
            break
    if any(has_terms(full, x) for x in ("decrease", "decreased", "reduced", "lower", "lowered")):
        change = "decrease"
    elif any(has_terms(full, x) for x in ("increase", "increased", "higher", "raised")):
        change = "increase"
    elif any(has_terms(full, x) for x in ("start", "started", "begin", "began")):
        change = "start"
    elif any(has_terms(full, x) for x in ("stop", "stopped", "discontinue", "discontinued")):
        change = "stop"
    else:
        change = ""
    if med and change:
        return med + "_" + change
    if med:
        return med
    anchor = token_anchor(cond)
    ak = slug(anchor)
    if ak in {"direct_result", "direct_result_of_what_happened", "direct_of_what_happened",
              "direct_of_death", "what_happened", "the_death"} or ak.startswith("direct_"):
        return ""
    return slug(anchor) if anchor and len(anchor.split()) <= 6 else ""

def direction_for(kind, f, blob, cond_anchor):
    group = norm_field("kind", kind)
    status = norm_field("status", f.get("status"))
    pred = word_key(f.get("predicate"))
    pol = norm_tok(f.get("polarity"))
    improved_terms = ("improved", "improving", "better", "less", "not as", "no longer")
    worsened_terms = ("worse", "worsened", "worsening", "increased", "more")
    if status == "improved" or any(has_terms(blob, x) for x in improved_terms):
        return "improved"
    if cond_anchor and any(has_terms(blob, x) for x in ("not feeling", "not feel")):
        return "improved"
    if status == "worsened" or any(has_terms(blob, x) for x in worsened_terms):
        return "worsened"
    # NOTE: "without" must NOT force absent — "T2DM without complications" is a
    # present diagnosis whose qualifier contains "without". Only predicate-level
    # denial or explicit "no evidence" marks absence.
    if any(has_terms(pred, x) for x in ("denies", "denied")) or has_terms(blob, "no evidence"):
        return "absent"
    if pol == "negative" and any(has_terms(pred, x) for x in ("not", "no", "denies", "denied")):
        return "absent"
    if status in {"active", "present"}:
        return "active" if group == "med" else "present"
    if status in {"ordered", "planned", "stop", "done", "decreased", "increased"}:
        return status
    if pol == "positive":
        return "present"
    return ""

def canonicalize_claim(kind, claim):
    f = claim.get("fields") or {}
    group = norm_field("kind", kind) or "other"
    blob = claim_blob(kind, claim)
    subtype, anchor, display = alias_anchor(blob)
    if not anchor:
        raw = f.get("object") or f.get("value") or claim.get("key") or (str(f.get("subject") or "") + " " + str(f.get("predicate") or ""))
        anchor = token_anchor(raw) or token_anchor(claim.get("key", "")) or "unspecified"
        subtype = fallback_subtype(group, blob, anchor)
        display = re.sub(r"[_\.]+", " ", str(raw)).strip() or anchor
    drug = drug_anchor(blob)
    generic_med_anchor = slug(anchor) in {"medication_adherence", "dose_change", "medication_effect", "medication"}
    if group == "med" and drug and (generic_med_anchor or subtype in {"med_adherence", "med_dose_change", "med_effect", "medication"}):
        if not slug(anchor).startswith(slug(drug)):
            anchor = drug + " " + anchor
            display = drug + " " + display
    cond = condition_anchor(f, blob)
    if cond:
        cr = condition_root(cond)
        ak = slug(anchor)
        if cr == ak or cr in ak or ak in cr:
            cond = ""
    direction = direction_for(kind, f, blob, cond)
    if cond and group in {"symptom", "med"} and (direction in {"improved", "worsened"} or has_terms(blob, "effect")):
        scope = "med_effect." + subtype
    else:
        scope = group + "." + subtype
    anchor_key = slug(anchor) or "unspecified"
    bits = [scope, anchor_key]
    if direction:
        bits.append(direction)
    if cond:
        bits.append("after_" + cond if any(x in cond for x in ("decrease", "increase", "start", "stop")) else cond)
    canonical_anchor = ".".join(bits)
    return {"group": group, "scope": scope, "subtype": subtype,
            "anchor": anchor, "anchor_key": anchor_key, "display_key": display,
            "direction": direction, "condition_anchor": cond,
            "canonical_anchor": canonical_anchor,
            "bucket": "|".join([scope, anchor_key, direction, cond])}

def direction_conflict(a, b):
    da, db = a.get("direction"), b.get("direction")
    if not da or not db:
        return False
    if {da, db} <= {"present", "done"} and (
        a.get("subtype") in {"family_event"} or b.get("subtype") in {"family_event"} or
        {a.get("group"), b.get("group")} & {"sochist", "famhist", "risk"}
    ):
        return False
    return da != db

def condition_root(c):
    for suffix in ("_decrease", "_increase", "_start", "_stop"):
        if c.endswith(suffix):
            return c[:-len(suffix)]
    return c

def condition_conflict(a, b):
    ca, cb = a.get("condition_anchor"), b.get("condition_anchor")
    return bool(ca and cb and ca != cb and condition_root(ca) != condition_root(cb))

def generic_anchor(c):
    return c.get("anchor_key") in {slug(x) for x in GENERIC_ANCHORS}

def compatible_bucket(a, b):
    if direction_conflict(a, b) or condition_conflict(a, b):
        return False
    if not relevant(a.get("anchor", ""), b.get("anchor", "")):
        return False
    if generic_anchor(a) or generic_anchor(b):
        return a.get("scope") == b.get("scope") and a.get("anchor_key") == b.get("anchor_key")
    if a.get("scope") == b.get("scope") or a.get("subtype") == b.get("subtype"):
        return True
    groups = {a.get("group"), b.get("group")}
    if groups <= {"symptom", "med"} and a.get("condition_anchor") and a.get("condition_anchor") == b.get("condition_anchor"):
        return True
    return False

def identity(kind, f):
    obj = lem_tokens(clean_tokens(norm_tok(f.get("object"))))
    if obj:
        base = obj
    else:
        subj = lem_tokens(clean_tokens(norm_tok(f.get("subject"))))
        pred = clean_tokens(norm_tok(f.get("predicate")))
        base = subj + "/" + PRED_MAP.get(pred, pred)
    return norm_field("kind", kind) + "||" + base

# unit is only semantically valid for dosing/measurement kinds; a unit carried by a
# diagnosis/symptom is noise mixed in by consensus voting
UNIT_KINDS = {"med", "lab", "vital", "test"}

# Relationship words that clearly belong to another person: under patient kinds these
# subjects are kept as-is (e.g. the mother's reporter role in pediatrics)
OTHER_PERSON_SUBJECTS = {
    "mother", "father", "mom", "dad", "sister", "brother", "son", "daughter",
    "aunt", "uncle", "grandmother", "grandfather", "wife", "husband", "partner",
    "family", "parents", "caregiver", "therapist", "clinician", "doctor",
    "nurse", "provider", "madre", "padre", "hermana", "hermano", "familia",
}
PATIENT_KIND_GROUPS = {"diagnosis", "symptom", "med", "lab", "vital", "test",
                       "allergy", "instruct"}
_VAL_UNIT_RE = None


def prune_gold_fields(kind_group, fields):
    """Gold field cleanup (three artifact classes located by red-teaming 2026-08-26):
    1. Non-dosing/measurement kinds carry no unit;
    2. A value that is a full-sentence quote fragment of >=4 words → cleared
       (cannot be normalized, only manufactures false contradictions);
       a value with an embedded unit ("1200 mg") → split into value+unit;
    3. Under patient kinds, a subject that is not a clear other-person relationship
       word → normalized to patient (adjudication semantics: such facts were
       confirmed by arbitration as 'patient facts with a mislabeled subject')."""
    fields = dict(fields)
    if kind_group not in UNIT_KINDS and fields.get("unit"):
        fields["unit"] = None
    v = fields.get("value")
    if v:
        vs = str(v).strip()
        m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|gm|ml|l|%)\s*$", vs, re.I)
        if m:
            fields["value"] = m.group(1)
            if kind_group in UNIT_KINDS and not fields.get("unit"):
                fields["unit"] = m.group(2).lower()
        elif len(vs.split()) >= 4 or (len(vs.split()) >= 3
                and not re.search(r"\d", vs)
                and spelled_to_number(norm_tok(vs)) is None):
            # Do not clear the whole value: extract the number inside to preserve
            # comparability (positive controls showed clearing blinds the scorer);
            # only pure prose with no number is set to empty
            m2 = re.search(r"\d+(?:\.\d+)?", vs)
            if m2 is None:
                n2 = spelled_to_number(norm_tok(vs))
                fields["value"] = str(n2) if n2 is not None else None
            else:
                fields["value"] = m2.group(0)
    subj = fields.get("subject")
    if subj and kind_group in PATIENT_KIND_GROUPS:
        k = word_key(subj)
        if k not in OTHER_PERSON_SUBJECTS and \
           not any(w in OTHER_PERSON_SUBJECTS for w in k.split(" ")):
            fields["subject"] = "patient"
    return fields


def vote_attr(items, attr):
    votes = collections.Counter(i["canon"].get(attr, "") for i in items if i["canon"].get(attr, ""))
    if not votes:
        return ""
    return votes.most_common(1)[0][0]

def subsets(a, b):
    ta = set(x for x in a.split(" ") if x)
    tb = set(x for x in b.split(" ") if x)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta

def relevant(a, b):
    ta = set(x for x in a.split(" ") if x)
    tb = set(x for x in b.split(" ") if x)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter >= 2 and (inter / max(union, 1)) >= 0.55

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", required=True)
    ap.add_argument("--case-meta", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    per_model = collections.OrderedDict()
    salvaged = {}
    for d in sorted(os.listdir(args.gen)):
        dd = os.path.join(args.gen, d)
        if not os.path.isdir(dd):
            continue
        for fn in os.listdir(dd):
            if fn.endswith(".json") and not fn.startswith("_") and "raw" not in fn:
                doc = json.load(open(os.path.join(dd, fn)))
                per_model[d] = doc.get("claims", [])
                salvaged[d] = bool(doc.get("salvaged"))
                break
    all_claims = [c for cl in per_model.values() for c in cl]
    alias_names = derive_patient_names(all_claims)
    # Fallback: the top-1 high-frequency non-generic subject whose name appears
    # verbatim in the transcript → the patient (single-patient session assumption;
    # take only the top one to avoid pulling in the therapist/family)
    case_dir = os.path.dirname(os.path.abspath(args.case_meta))
    ts_key = ""
    for src_name in ("transcript.txt", "additions.txt", "template.txt"):
        p = os.path.join(case_dir, "sources", src_name)
        if os.path.isfile(p):
            ts_key += " " + word_key(open(p, encoding="utf-8").read())
    ts_key += " "
    freq = collections.Counter()
    for c in all_claims:
        s = (c.get("fields") or {}).get("subject")
        if s and word_key(s) not in GENERIC_SUBJECT_ALIASES:
            freq[word_key(s)] += 1
    if freq and ts_key:
        top, n = freq.most_common(1)[0]
        toks = [t for t in top.split(" ") if len(t) > 1]
        if n >= 3 and toks and all((" " + t + " ") in ts_key for t in toks):
            alias_names.append(top)
    set_case_subject_aliases(alias_names)
    order = list(per_model.keys())
    n = len(order)
    if n == 0:
        print("no model outputs under", args.gen)
        raise SystemExit(1)
    missing = [m for m in MODEL_ORDER if not any(tag(m) in d for d in per_model)]

    # Bucket after normalization
    buckets = collections.OrderedDict()
    bucket_info = {}
    claim_map = {}
    for m in order:
        seen = set()
        for idx, c in enumerate(per_model[m]):
            f = c.get("fields") or {}
            canon = canonicalize_claim(c.get("kind"), c)
            key = canon["bucket"]
            sig = json.dumps(f, sort_keys=True, ensure_ascii=False)
            if (key + sig) in seen:
                continue
            seen.add(key + sig)
            buckets.setdefault(key, [])
            bucket_info.setdefault(key, canon)
            item = {"model": m, "idx": idx, "claim": c, "canon": canon}
            buckets[key].append(item)
            claim_map[m + "|" + str(idx)] = item

    # Second-pass merge: under the same subtype / compatible coarse class, anchor
    # token subset or high overlap → merge
    merged = True
    while merged:
        merged = False
        keys = list(buckets.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if a not in buckets or b not in buckets:
                    continue
                if compatible_bucket(bucket_info[a], bucket_info[b]):
                    buckets[a].extend(buckets[b])
                    del buckets[b]
                    del bucket_info[b]
                    merged = True
                    break
            if merged:
                break

    facts = []
    model_claim_fact = {}
    for kid, items in buckets.items():
        support_models = []
        for i in items:
            if i["model"] not in support_models:
                support_models.append(i["model"])
        support = len(support_models)
        gold = {}
        disagreements = []
        stable_disagreements = []
        for fld in FIELDS:
            votes = collections.Counter()
            by_model = collections.defaultdict(collections.Counter)
            for i in items:
                v = norm_field(fld, i["claim"].get("fields", {}).get(fld))
                if v:
                    by_model[i["model"]][v] += 1
            for model_votes in by_model.values():
                votes[model_votes.most_common(1)[0][0]] += 1
            if not votes:
                gold[fld] = None
                continue
            top, cnt = votes.most_common(1)[0]
            gold[fld] = top
            observed = sum(votes.values())
            if len(votes) > 1 and cnt * 2 <= observed:
                item = {"field": fld, "votes": sorted(votes.items(), key=lambda x: -x[1])}
                disagreements.append(item)
                if fld in STABLE_FIELDS:
                    stable_disagreements.append(item)
        kind_by_model = collections.defaultdict(collections.Counter)
        for i in items:
            k = norm_field("kind", i["claim"].get("kind"))
            if k:
                kind_by_model[i["model"]][k] += 1
        kind_votes = collections.Counter()
        for model_votes in kind_by_model.values():
            kind_votes[model_votes.most_common(1)[0][0]] += 1
        if len(kind_votes) > 1:
            disagreements.append({"field": "kind",
                                  "votes": sorted(kind_votes.items(), key=lambda x: -x[1])})
        strict_stable = not disagreements
        field_stable = not stable_disagreements
        if support == n and field_stable:
            status = "certain"
        elif support >= max(2, n // 2):
            status = "low_conf"
        else:
            status = "unique"
        fid = "F%03d" % (len(facts) + 1)
        display_key = vote_attr(items, "display_key") or items[0]["claim"].get("key", "")
        scope = vote_attr(items, "scope")
        anchor_key = vote_attr(items, "anchor_key")
        canonical_kind = vote_attr(items, "group") or norm_field("kind", items[0]["claim"].get("kind"))
        subtype = vote_attr(items, "subtype")
        direction = vote_attr(items, "direction")
        cond_anchor = vote_attr(items, "condition_anchor")
        anchor_bits = [scope or (canonical_kind + "." + subtype if subtype else canonical_kind), anchor_key]
        if direction:
            anchor_bits.append(direction)
        if cond_anchor:
            anchor_bits.append("after_" + cond_anchor if any(x in cond_anchor for x in ("decrease", "increase", "start", "stop")) else cond_anchor)
        canonical_anchor = ".".join(x for x in anchor_bits if x)
        model_keys = sorted(set(i["claim"].get("key", "") for i in items if i["claim"].get("key", "")))
        evidence = []
        for i in items:
            ev = {"model": i["model"], "idx": i["idx"],
                  "quote": i["claim"].get("evidence_quote", ""),
                  "source": i["claim"].get("source", []),
                  "key": i["claim"].get("key", ""),
                  "canonical_anchor": i["canon"].get("canonical_anchor", "")}
            evidence.append(ev)
            model_claim_fact[i["model"] + "|" + str(i["idx"])] = fid
        facts.append({"id": fid, "key": items[0]["claim"].get("key", ""),
                      "display_key": display_key,
                      "canonical_anchor": canonical_anchor,
                      "canonical_kind": canonical_kind,
                      "subtype": subtype,
                      "direction": direction,
                      "condition_anchor": cond_anchor,
                      "model_keys": model_keys,
                      "kind": items[0]["claim"].get("kind", ""),
                      "fields": prune_gold_fields(canonical_kind, gold),
                      "support": "%d/%d" % (support, n),
                      "models": support_models, "status": status,
                      "field_stable": field_stable,
                      "strict_field_stable": strict_stable,
                      "stable_disagreements": stable_disagreements,
                      "disagreements": disagreements, "evidence": evidence})

    # per_model: mark each claim's consensus assignment and whether it diverges
    per_model_out = []
    for m in order:
        rows = []
        for idx, c in enumerate(per_model[m]):
            fid = model_claim_fact.get(m + "|" + str(idx))
            label = "orphan"
            if fid:
                fact = facts[int(fid[1:]) - 1]
                diverge = False
                for fld in FIELDS:
                    v = norm_field(fld, c.get("fields", {}).get(fld))
                    gv = fact["fields"].get(fld) or ""
                    if v and v != gv:
                        diverge = True
                        break
                label = "diverge" if diverge else "agree"
            canon = canonicalize_claim(c.get("kind"), c)
            rows.append({"idx": idx, "key": c.get("key", ""), "kind": c.get("kind", ""),
                         "display_key": canon.get("display_key", ""),
                         "canonical_anchor": canon.get("canonical_anchor", ""),
                         "subtype": canon.get("subtype", ""),
                         "fields": c.get("fields", {}), "evidence_quote": c.get("evidence_quote", ""),
                         "source": c.get("source", []), "fact_id": fid, "label": label})
        per_model_out.append({"model": m, "claims": rows, "salvaged": salvaged.get(m, False)})

    stats = {"n_models": n, "n_facts": len(facts),
             "certain": sum(1 for f in facts if f["status"] == "certain"),
             "strict_certain": sum(1 for f in facts if f["support"] == "%d/%d" % (n, n) and f.get("strict_field_stable")),
             "low_conf": sum(1 for f in facts if f["status"] == "low_conf"),
             "unique": sum(1 for f in facts if f["status"] == "unique"),
             "unanimous": sum(1 for f in facts if f["support"] == "%d/%d" % (n, n)),
             "low_support": sum(1 for f in facts if int(f["support"].split("/")[0]) <= 2)}

    meta = json.load(open(args.case_meta))
    out = {"case": meta.get("case_id", os.path.basename(os.path.dirname(args.gen))),
           "locale": meta.get("locale", ""), "meta": meta,
           "models": order, "n_models": n, "stats": stats,
           "missing_models": missing,
           "facts": facts, "per_model": per_model_out}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False))

if __name__ == "__main__":
    main()
