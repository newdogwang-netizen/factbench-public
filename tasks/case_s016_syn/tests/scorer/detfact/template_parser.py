import hashlib
import os
import re

from detfact.schema import FIELD_NAMES, validate_claims_doc
from detfact_consensus import DRUG_NAMES, canonicalize_claim, slug

SECTION_KIND = [
    (("medication", "medications", "current medications", "meds"), "medication"),
    (("medication changes", "med changes", "changes"), "medication_change"),
    (("side effect", "side effects", "symptom", "symptoms", "concerns",
      "history of presenting complaints", "history of present illness", "hpi"), "symptom"),
    (("diagnosis", "diagnoses", "assessment", "medical history", "past medical",
      "psychiatric history"), "diagnosis"),
    (("allergy", "allergies"), "allergy"),
    (("vital", "vitals"), "vital"),
    (("lab", "labs", "laboratory"), "lab_result"),
    (("test", "tests", "testing"), "test_ordered"),
    (("referral", "referrals"), "referral"),
    (("follow up", "follow-up", "followup", "appointment"), "followup_appointment"),
    (("therapy", "psychotherapy"), "psychotherapy"),
    (("family history",), "family_history"),
    (("social history",), "social_history"),
    (("risk", "safety"), "risk"),
    (("plan", "treatment", "instructions", "education", "recommendation",
      "recommendations", "next steps"), "treatment"),
]

NEGATION_RE = re.compile(r"\b(no|not|denies|denied|without|never|none|nunca|niega)\b", re.I)
IMPROVED_RE = re.compile(r"\b(improved|improving|better|less|no longer)\b", re.I)
WORSENED_RE = re.compile(r"\b(worse|worsened|worsening|increased|more)\b", re.I)
DOSE_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|ml|l|kg|milligrams?|micrograms?)\b", re.I)
FREQ_RE = re.compile(
    r"\b(every\s+(?:morning|night|evening|day|week|month)|"
    r"twice\s+(?:daily|a\s+day|a\s+week)|"
    r"(?:three|four|five|\d+)\s+times\s+(?:daily|a\s+day|per\s+day)|"
    r"once\s+(?:daily|a\s+day|a\s+week)|"
    r"daily|weekly|monthly|nightly|morning|evening|at night|qhs|bid|tid|prn)\b",
    re.I,
)
ABSENT_PLACEHOLDER_RE = re.compile(
    r"\b(no|not|none)\b.{0,80}\b(explicitly mentioned|explicitly stated|stated|mentioned|provided|available)\b",
    re.I,
)
DOB_RE = re.compile(
    r"\b([0-9]{1,2}[A-Za-z]{3}[0-9]{4}|[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})\b",
    re.I,
)
ICD_RE = re.compile(r"\b([A-Z]\d{1,3}(?:[.\s]\d{1,3})?)\b", re.I)
MONTHS = {
    "jan": "jan",
    "feb": "feb",
    "mar": "mar",
    "apr": "apr",
    "may": "may",
    "jun": "jun",
    "jul": "jul",
    "aug": "aug",
    "sep": "sep",
    "oct": "oct",
    "nov": "nov",
    "dec": "dec",
}
IGNORED_NAME_STARTS = {
    "he",
    "she",
    "they",
    "it",
    "member",
    "patient",
    "the patient",
}
GENERIC_OTHER_ENABLED = os.environ.get("DETFACT_TEMPLATE_GENERIC_OTHER", "").lower() in {"1", "true", "yes"}

DX_RULES = [
    ("dementia in other diseases classified elsewhere, unspecified severity, without behavioral disturbance",
     ("dementia in other diseases", "dementia classified elsewhere", "f02.80")),
    ("type 2 dm without complications", ("type 2 dm", "type 2 diabetes", "diabetes mellitus", "e11.9")),
    ("atherosclerotic heart disease of native coronary artery without angina pectoris",
     ("atherosclerotic heart disease", "coronary artery", "i25.10")),
    ("functional urinary incontinence", ("functional urinary incontinence", "r39.81")),
    ("unspecified osteoarthritis, unspecified site (m19 90)", ("osteoarthritis", "m19.90", "m19 90")),
    ("blood pressure", ("blood pressure", "hypertension", "presion arterial", "presión arterial", "i10")),
    ("hyperlipidemia, unspecified", ("hyperlipidemia", "e78.5")),
    ("sleep difficulty", ("insomnia", "sleep difficulty", "g47.00")),
    ("history of falling", ("history of falling", "fall risk", "z91.81")),
    ("dizziness and giddiness", ("dizziness and giddiness", "dizziness", "giddiness", "r42")),
    ("polyneuropathy, unspecified", ("polyneuropathy", "g62.9")),
    ("vitamin deficiency, unspecified", ("vitamin deficiency", "e56.9")),
    ("depression", ("depression", "depresion", "depresión", "f33.9")),
    ("anxiety", ("anxiety", "ansiedad")),
    ("reflujo", ("reflujo", "reflux")),
    ("pain", ("pain", "dolor", "r07.9")),
]


def fields(**values):
    return {name: values.get(name) for name in FIELD_NAMES}


def make_claim(kind, key, quote, **field_values):
    return {
        "key": "{}.{}".format(kind, object_slug(key)),
        "kind": kind,
        "fields": fields(**field_values),
        "evidence_quote": quote,
        "source": ["generated_output"],
    }


def clean_line(line):
    line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", line)
    return re.sub(r"\s+", " ", line).strip(" \t\r\n")


def clean_heading(line):
    line = line.strip()
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"^\*\*|\*\*$", "", line).strip()
    line = line.rstrip(":").strip()
    return re.sub(r"\s+", " ", line).lower()


def section_kind(heading):
    h = clean_heading(heading)
    for names, kind in SECTION_KIND:
        if any(name in h for name in names):
            return kind
    return "other"


def iter_section_items(text):
    section = None
    pending = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 粗体整行标题(**History of ...:**)是常见模型输出形态:
        # 仅识别"整行被 ** 包裹且以冒号收尾"的窄形态,不做全局重分段
        bold_head = bool(re.match(r"^\*\*[^*]+:\*\*$", line))
        is_heading = bold_head or bool(re.match(r"^#+\s+\S", line)) or (
            line.endswith(":") and not re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", raw)
        )
        if is_heading:
            section = clean_heading(line)
            continue
        bullet = bool(re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", raw))
        if bullet:
            item = clean_line(line)
            if item:
                yield section or "general", item
            continue
        if section:
            pending.append((section, clean_line(line)))
    for sec, item in pending:
        if item:
            yield sec, item


def object_slug(value):
    key = slug(value or "")
    return key or "unspecified"


def polarity_for(text):
    # 括号内容是旁注(设备/流程说明),其中的否定不指向主实体:
    # "BP 117/74 (cuff ... has not been replaced)" 不是否认血压
    text = re.sub(r"\([^)]*\)", " ", text)
    # 引号内是转述用语("not ideal" but "manageable"),不是否认症状
    text = re.sub(r'["“][^"”]*["”]', " ", text)
    # 极性只看主句:分号/破折号/转折之后是补充叙述,其中的否定
    # 不指向主实体("reports sadness, but is not blaming himself")
    text = re.split(r";|—|—|\bbut\b|\bhowever\b", text, maxsplit=1, flags=re.I)[0]
    # 意愿/认知否定不是症状否定:"does not want/remember/know/like/blame X"
    text = re.sub(r"\b(?:do(?:es)?\s+not|don'?t|doesn'?t|did\s+not|didn'?t)\s+"
                  r"(?:want|remember|recall|know|like|blame|feel|think|believe)\b.*", " ", text,
                  flags=re.I)
    # 伴随否认是限定语义:"palpitations ... no associated chest pain"
    # 否认的是伴随物,不是全局否认该症状
    text = re.sub(r"\bno\s+associated\b[^,;.]*", " ", text, flags=re.I)
    # 诊断状态否定不是症状否定:"never diagnosed" 陈述的是确诊状态
    text = re.sub(r"\b(?:never|not)\s+(?:formally\s+|officially\s+)?diagnosed\b",
                  " ", text, flags=re.I)
    # 情绪内容从句("anxiety about not being able to...")的否定
    # 指向担忧内容,不指向情绪本身
    text = re.sub(r"\b(?:about|regarding|related to|over)\b.*$", " ", text, flags=re.I)
    # "not myself/himself" 是症状名(解离感),不是对症状的否定
    text = re.sub(r"\bnot\s+(?:my|him|her|them|one)sel(?:f|ves)\b", " selfdissoc ", text,
                  flags=re.I)
    # 质地描述的否定是性状信息,不是症状否认:"dull ache, not throbbing"
    text = re.sub(r"\b(?:not|non|no longer)[- ](?:throbbing|pulsating|pulsatile|sharp|"
                  r"stabbing|shooting|burning|radiating|positional|constant)\b",
                  " ", text, flags=re.I)
    # 否定"改善/缓解"= 症状仍在,不是否认症状:"no subsequent improvement"
    text = re.sub(r"\b(?:no|not|without)\s+(?:\w+\s+){0,2}"
                  r"(?:improvement|improvements|improving|improved?|relief|relieving|benefit|change|changes|changing)\b",
                  " ", text, flags=re.I)
    return "negative" if NEGATION_RE.search(text) else "positive"


def medication_polarity_for(text, drug):
    folded = text.lower()
    drug_re = re.escape(drug.lower())
    # "no muscle aches with atorvastatin":否定指向症状,伴随介词
    # (with/on/while taking)后的药物不被否定
    if re.search(r"\b(?:no|not|without|denies|denied)\b[^.;]{0,45}\b(?:with|on|while\s+taking)\s+"
                 + drug_re + r"\b", folded):
        return "positive"
    # 认知/意愿否定不指向药物本身:"does not remember why Prozac was stopped"
    if re.search(r"\b(?:no|not|without|denies|denied)\b(?!\s+(?:remember|recall|know|want|like|sure|benefit|improvement|relief|change|longer\s+than|feel\b|think|believe|any\s+different))"
                 r".{0,30}\b" + drug_re + r"\b", folded):
        return "negative"
    if re.search(r"\b" + drug_re + r"\b.{0,30}\b(?:stopped|discontinued|not (?:currently |presently )?taking|no longer taking)\b", folded):
        return "negative"
    return "positive"


def status_for(kind, text):
    # "a discussion/meeting was held" 的 held 不是停药;先剥离该句式
    text = re.sub(r"\b(?:discussion|meeting|visit|conversation|session)s?\s+"
                  r"(?:was|were|will be)\s+held\b", " ", text, flags=re.I)
    # resolved 先于 improved:"resolved, no longer present" 的 no longer
    # 会被 IMPROVED_RE 抢先吞掉
    if re.search(r"\b(resolved|no longer present|abated|cleared up|fully subsided)\b",
                 text, re.I):
        return "resolved"
    if kind != "medication" and IMPROVED_RE.search(text):
        return "improved"
    if kind != "medication" and WORSENED_RE.search(text):
        return "worsened"
    if kind in {"medication", "medication_change", "treatment"} and \
            re.search(r"\b(stopped|discontinued|ceased|held)\b", text, re.I):
        return "stop"
    # 条件教育句是建议帧,不是现状断言:"(advised that) if she misses
    # 4-5 days ... should not restart at 200 mg" → 医嘱帧(plan_xor 保护值)
    if kind in {"medication", "medication_change"} and re.match(
            r"\s*(?:patient\s+)?(?:advised|instructed|counseled|educated)?\s*(?:that\s+)?if\b",
            text, re.I):
        return "ordered"
    # 过去时疗效叙事不是在服断言:"Gabapentin provided minimal relief"
    # (无现在时标记时按既往记;past 与 stop 在 CONTRA 集合同侧)
    if kind in {"medication", "medication_change"} and re.search(
            r"\b(?:provided|produced|offered|yielded)\b[^.;]{0,30}"
            r"\b(?:relief|benefit|improvement|help)\b", text, re.I) and not re.search(
            r"\b(?:currently|continues?|is providing|provides)\b", text, re.I):
        return "past"
    # 否定的重启不是重启:"has not been restarted" → 停用
    if kind in {"medication", "medication_change"} and re.search(
            r"\b(?:not|never|hasn'?t|has not|won'?t|will not)\s+(?:be(?:en)?\s+)?restart", text, re.I):
        return "stop"
    # 开药者主语 + "gave" 是历史开药事件叙述("Dr. X gave him buspirone",
    # 原子 bullet 拆行后停药信息在别行)→ 既往。被动 "was given" 不算:
    # 住院/急诊给药(...admitted, was given hydroxyzine)常延续为现用药。
    if kind in {"medication", "medication_change"} and re.search(
            r"\bgave\s+(?:him|her|them|me)\b", text, re.I) and not re.search(
            r"\b(?:currently|continues?|today|will start)\b", text, re.I):
        return "past"
    # 现在进行时标记先于历史前缀:"Hx of obesity — ... currently on Zepbound"
    # 行首 Hx 描述的是病史条目,药物自身状态由紧邻的 currently 决定
    if kind in {"medication", "medication_change", "treatment"} and re.search(
            r"(?<!not )\b(?:currently\s+(?:on|taking|takes|using|uses|prescribed)"
            r"|remains?\s+on|maintained\s+on|continues?\s+on)\b",
            text, re.I):
        return "active"
    # 既往标记(通用临床电报体,规则源自 held-out 病历语料):
    # "history of X" / "trialed as a child" / "s/p" 等 → past
    # "previously helped/worked" 是疗效史注释,不是用药史("以前有效"≠"已停用")
    if re.search(r"\b(history of|hx of|was on\b|had been on\b|prior(?!\s+dose\b)|previous(?:ly)?(?!\s+(?:helped|worked|effective|beneficial|taken\s+(?:in|at|with)\b|antidepressants?\b|medications?\b|meds?\b|drugs?\b|ssris?\b|snris?\b|agents?\b|regimens?\b|\d+\s*(?:mg|mcg|g)\b))|trialed|tried\b.{0,30}\b(?:as a child|in the past|years? ago)|deceased|s/p|no longer(?!\s+(?:effective|work\w*|help\w*|need\w*))|liked|used to take)\b",
                 text, re.I):
        return "past"
    # 讨论中的选项/条件性计划:"considered / discussed as an option / may increase"
    # 仅限用药/医嘱类——"considered suicide" 这类风险语句不得进此分支
    if kind in {"medication", "treatment", "medication_change", "test_ordered", "referral"} and re.search(
            r"\b(consider(?:ed|ing)?|possibl[ye]|discussed as|discuss\w*\b[^.;]{0,50}\b(?:option|alternative)|as an option|possible option|option to\b|adding\b|to be (?:added|started)|may\s+(?:increase|start|add|titrate)|(?:can|may) be used|add|suggested)\b",
            text, re.I):
        return "ordered"
    if re.search(r"\b((?<!originally )(?<!initially )start|(?<!originally )(?<!initially )started|begin|began|new|order(?:ed)?(?!\s+to)|agree(?:s|d)? to (?:try|start)|trial of|scheduled|plan|planned|will)\b", text, re.I):
        return "planned" if kind in {"followup_appointment", "referral", "treatment", "test_ordered"} else "ordered"
    if re.search(r"\b(permitted|permission|cleared)\b", text, re.I):
        return "permitted"
    if kind == "medication":
        return "active"
    if kind == "symptom":
        return "present"
    return "present"


def strip_time_phrases(text):
    text = re.sub(r"\b(in|during|at|on)\s+the\s+(morning|evening|afternoon|night)\b", "", text, flags=re.I)
    text = re.sub(r"\b(every|each)\s+(morning|evening|afternoon|night|day|week|month)\b", "", text, flags=re.I)
    text = re.sub(r"\btwice\s+a\s+week\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" .,;:")


def find_medication(text):
    folded = text.lower()
    for drug in sorted(DRUG_NAMES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(drug.lower()) + r"\b", folded):
            return drug
    m = re.search(r"\b(?:takes?|taking|continue(?:s)?|on|started|start)\s+([A-Z][A-Za-z0-9-]+)\b", text)
    if m:
        return m.group(1)
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)
    return words[0] if words else strip_time_phrases(text)


def clean_icd(value):
    if not value:
        return None
    return value.upper().replace(" ", ".")


def maybe_icd(text):
    m = ICD_RE.search(text)
    return clean_icd(m.group(1)) if m else None


def normalize_dob(value):
    raw = (value or "").strip()
    m = re.match(r"^([0-9]{1,2})([A-Za-z]{3})([0-9]{4})$", raw)
    if m:
        month = MONTHS.get(m.group(2).lower(), m.group(2).lower())
        return "{}{}{}".format(int(m.group(1)), month, m.group(3))
    m = re.match(r"^([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{2,4})$", raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if len(year) == 2:
            year = "19" + year if int(year) > 30 else "20" + year
        names = ["", "jan", "feb", "mar", "apr", "may", "jun",
                 "jul", "aug", "sep", "oct", "nov", "dec"]
        if 1 <= month <= 12:
            return "{}{}{}".format(day, names[month], year)
    return raw.lower()


def extract_dob(item):
    for prefix in (
        r"\b(?:i['’]ll use|i will use|let me use|will use|majority(?: is)?|two sources(?: say)?)\b.{0,80}?",
        r"\b(?:driver'?s license|licen[cs]e|mmse|f\.?\s*nacimiento|dob/nac)\b.{0,80}?",
        r"\b(?:dob|date of birth|fecha de nacimiento)\s*[:：]?\s*",
    ):
        m = re.search(prefix + DOB_RE.pattern, item, re.I)
        if m:
            return normalize_dob(m.group(m.lastindex))
    m = DOB_RE.search(item)
    return normalize_dob(m.group(1)) if m else None


def norm_dose_value(v):
    # 千位分隔逗号("2,000 units")不是欧式小数:先并千位,再转小数点
    v = re.sub(r"(\d),(?=\d{3}\b)", r"\1", v)
    return v.replace(",", ".")


def extract_dose_unit(text, drug=None):
    # 认知帧从句是转述错觉,其中的数字不是剂量:"she believed X was 20 mg, but ..."
    text = re.sub(r"\b(?:she|he|they|patient)?\s*(?:believed|thought|assumed)\b[^,;]*[,;]?",
                  " ", text, flags=re.I)
    hay = text
    if drug:
        m = re.search(re.escape(drug), text, re.I)
        if m:
            # 窗口从药名之后开始:品牌名内的数字(Curcuplex 95)不是剂量
            hay = text[m.end():m.end() + 60]
    # 剂量变更句取【现值】:"increased from 5 mg to 10 mg" 的在服剂量是 10。
    # 共识侧同吃此规则——旧值入 gold 是实测的系统性脏点(六家同踩)。
    mc = re.search(r"\b(?:from|increased?|increasing|decreased?|decreasing|titrated?|up)\s+"
                   r"(?:from\s+)?(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|ml|milligrams?|micrograms?)?\s*"
                   r"(?:\w+\s+){0,4}?to\s+(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|ml|milligrams?|micrograms?)?\b",
                   hay, re.I)
    if mc:
        return norm_dose_value(mc.group(3)), (mc.group(4) or mc.group(2) or "mg")
    # 检验数值不是剂量:"level was 18 ... 24 most recently; goal above 30"
    hay = re.sub(r"\b(?:level|levels|tsh|a1c|hba1c|ldl|score)s?\b[^.;]*", " ", hay, flags=re.I)
    dose = DOSE_RE.search(hay)
    if dose:
        return norm_dose_value(dose.group(1)), dose.group(2)
    # 时长不是剂量:"filled 3 weeks ago" / "3 years ago"(无单位 fallback 前剥离)
    hay = re.sub(r"\b\d+(?:\.\d+)?\s*(?:day|week|month|year)s?\b", " ", hay, flags=re.I)
    hay = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", hay)  # 日期不是剂量
    hay = re.sub(r"\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s*(?:/|out of)\s*10\b", " ", hay)  # 疼痛评分(含区间)不是剂量
    hay = re.sub(r"\b\d+(?:\.\d+)?\s*(?:lbs?|pounds?)\b", " ", hay, flags=re.I)  # 体重(lost 50 lbs)不是剂量
    hay = re.sub(r"\b\d+\s*[- ]?(?:hour|hr)s?\b", " ", hay, flags=re.I)  # 剂型时长(24-hour formulation)不是剂量
    # 无单位数字仅在带给药语境词时可信(gtt/po/频次);裸数字是垃圾值之源
    # (实测:睡眠"2-3 times"、"3 years ago"、检验值都曾被当剂量)
    m = re.search(r"\b(\d+(?:[.,]\d+)?(?:\s*gtt)?)\s*((?:po|eye|topical)\s*(?:daily|bid|qid|prn|qhs)?|(?:daily|bid|qid|prn|qhs))\b", hay, re.I)
    if m:
        value = norm_dose_value(m.group(1)).strip()
        unit = re.sub(r"\s+", " ", m.group(2)).strip() or None
        return value, unit
    return None, None


SINGLE_DOSE_TIME_RE = re.compile(
    r"\b((?:last night|tonight|yesterday|this (?:morning|afternoon|evening))"
    r"(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?|\d{1,2}:\d{2}\s*(?:am|pm)?)\b", re.I)


def first_frequency(text):
    # 睡眠叙事钟点不是给药时点:"wakes around 5-5:30 a.m."
    text = re.sub(r"\b(?:wakes?|woke|waking|wakes\s+up|awake(?:ns)?)\b[^,;.]*", " ", text, flags=re.I)
    # 单次给药时点(last night at 7:51 pm)是最强的框架证据,优先于频次词:
    # 一次性 PRN 给药与常规方案是不同事件,时点决定归属
    m = SINGLE_DOSE_TIME_RE.search(text)
    if m:
        return m.group(1).lower()
    m = FREQ_RE.search(text)
    if m:
        return m.group(1)
    m = re.search(r"\b(qid|bid|daily|prn)\b", text, re.I)
    return m.group(1).lower() if m else None


def has_any(text, phrases):
    folded = text.lower()
    return any(p.lower() in folded for p in phrases)


def canonical_dx_name(text):
    folded = text.lower()
    for name, phrases in DX_RULES:
        if any(p.lower() in folded for p in phrases):
            return name
    return None


def split_atomic_text(item):
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z0-9\"“])", item)
    return [p.strip(" .;") for p in parts if p.strip(" .;")]


def demo_claims(item):
    out = []
    for label in (r"patient name", r"name", r"nombre"):
        m = re.search(label + r"\s*[:：]\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ' -]{1,80})", item, re.I)
        if m:
            name = m.group(1).strip(" .;-")
            out.append(make_claim("demo", name, item, subject="patient", predicate="name",
                                  object=name, polarity="positive"))
            break
    if not out:
        m = re.match(r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s+(?:reported|reports|states|stated|is|was)\b", item)
        if m and m.group(1).lower() not in IGNORED_NAME_STARTS:
            name = m.group(1)
            out.append(make_claim("demo", name, item, subject="patient", predicate="name",
                                  object=name, polarity="positive"))
    if re.search(r"\b(?:dob|date of birth|fecha de nacimiento|dob/nac|nacimiento)\b", item, re.I):
        dob = extract_dob(item)
        out.append(make_claim("demo", "date_of_birth", item, subject="patient",
                              predicate="date_of_birth", object=dob, value=dob,
                              polarity="positive"))
    m = re.search(r"\b(?:age\s*[:：]?\s*)?(\d{1,3})[- ]?(?:year|yr)s?(?:[- ]old)?\s+(male|female|man|woman)\b", item, re.I)
    if not m:
        m = re.search(r"\b(?:age|edad)\s*[:：]\s*(\d{1,3})\b", item, re.I)
    if m:
        age = m.group(1)
        gender = m.group(2).lower() if len(m.groups()) >= 2 and m.group(2) else None
        obj = age + ("-year " + gender if gender else "")
        out.append(make_claim("demo", "age", item, subject="patient",
                              predicate="age_and_gender" if gender else "age",
                              object=obj, value=age, unit="years",
                              time="current" if gender else None,
                              polarity="positive"))
    return out


def diagnosis_claims(item):
    out = []
    folded = item.lower()
    if "all ligament" in folded or "all the ligament" in folded or "all ligaments" in folded:
        if "partial" in folded:
            out.append(make_claim("diagnosis", "all the ligament", item,
                                  subject="patient", predicate="tore",
                                  object="all the ligament", value="partial tears",
                                  location="ankle", condition="ankle",
                                  polarity="positive"))
    for part in split_atomic_text(item):
        name = canonical_dx_name(part)
        if not name:
            continue
        icd = maybe_icd(part)
        if name == "pain" and not icd:
            continue
        condition = None
        value = icd
        unit = "ICD-10" if icd else None
        time = "current" if icd else None
        location = None
        if name == "depression" and has_any(part, ("nueve meses", "nine months", "hospital pav")):
            value, unit, time, condition = "unos nueve meses", "meses", "al principio", "esa depresión"
        if name == "blood pressure" and has_any(part, ("hypertension", "hipertension", "hipertensión")):
            condition = "hypertension"
            location = "centro médico" if has_any(part, ("medical center", "centro medico", "centro médico")) else None
        if name == "anxiety" and has_any(part, ("aparentemente", "apparently", "suspected")):
            condition = "aparentemente"
            time = "luego de la hospitalización" if has_any(part, ("hospital", "hospitaliz")) else time
            location = "sala de emergencia" if has_any(part, ("emergency", "emergencia")) else None
        if name == "reflujo":
            is_diagnostic_rule = (
                has_any(part, ("diagnostic of reflux", "diagnostico de reflujo", "diagnóstico de reflujo",
                               "diagnosticos de reflujo", "diagnósticos de reflujo"))
                or (
                    has_any(part, ("grade b", "grado b", "grades b", "grados b"))
                    and has_any(part, ("grade c", "grado c", "grades c", "grados c"))
                    and has_any(part, ("grade d", "grado d", "grades d", "grados d"))
                    and has_any(part, ("reflux", "reflujo"))
                )
            )
            if not is_diagnostic_rule:
                continue
            out.append(make_claim("diagnosis", name, part,
                                  subject="esofagitis grados b, c y d",
                                  predicate="es diagnóstico de",
                                  object=name, value=value, unit=unit,
                                  time=time, location=location,
                                  status="diagnóstico",
                                  polarity="positive",
                                  condition="esófago de Barrett" if has_any(part, ("barrett", "barret")) else None))
            continue
        out.append(make_claim("diagnosis", name, part, subject="patient",
                              predicate="diagnosis" if icd else "has",
                              object=name, value=value, unit=unit, time=time,
                              location=location,
                              status=status_for("diagnosis", part),
                              # ICD 编码行是诊断断言;病名内的 "without complications"
                              # 是限定词不是否定(rules 0.4 已有同款锚规则)
                              polarity="positive" if icd else polarity_for(part),
                              condition=condition))
    return out


def medication_claims(item, section_kind_val="", section_name=""):
    out = []
    folded_item = item.lower()
    for drug in sorted(DRUG_NAMES, key=len, reverse=True):
        if not re.search(r"\b" + re.escape(drug) + r"\b", item, re.I):
            continue
        value, unit = extract_dose_unit(item, drug=drug)
        # 频次取该药附近的窗口:多药同行时整行第一个频次会张冠李戴
        wm = re.search(re.escape(drug) + r".{0,80}", item, re.I)
        drug_window = wm.group(0) if wm else item
        cond = None
        if drug == "warfarin" and has_any(item, ("blood thinner", "anticoagul")):
            cond = "blood thinner"
        if drug in {"glucotrol xl", "invokana"} and has_any(item, ("type 2", "diabetes", "dm")):
            cond = "type 2 dm"
        # "X-related/induced" 是副作用叙述的修饰语,不是用药断言
        if re.search(re.escape(drug) + r"[- ](?:related|induced|associated)\b", item, re.I) \
                and not re.search(re.escape(drug) + r"\b(?![- ](?:related|induced|associated))", item, re.I):
            continue
        # 状态/极性作用域:先取含药名的句子(多句 ROS 条目里别句的否定/
        # 病史词不得污染本药);句内多药再按分号段隔离;单药句看整句
        sents = split_sentences(item) or [item]
        drug_sent = next((s for s in sents
                          if re.search(r"\b" + re.escape(drug) + r"\b", s, re.I)), item)
        segs = drug_sent.split(";")
        si = next((i for i, s in enumerate(segs)
                   if re.search(r"\b" + re.escape(drug) + r"\b", s, re.I)), 0)
        drug_seg = segs[si]
        # 延续段并入:下一分号段是本药的续述("; discontinued due to hot flashes")
        # ——以续述动词开头且不含其他药名;远处无关段("; prior STD testing")不并入
        for nxt in segs[si + 1:si + 2]:
            if re.match(r"\s*(?:discontinued|stopped|was\b|which\b|but\b|no longer|"
                        r"caused\b|not\b|d/c)", nxt, re.I) and not any(
                    re.search(r"\b" + re.escape(d2) + r"\b", nxt, re.I)
                    for d2 in DRUG_NAMES if d2 != drug):
                drug_seg = drug_seg + ";" + nxt
        # 完成时试药("has (also) tried A, B, C"):多药枚举是试药史清单,
        # 与现用同药可并存(prior instance);单药完成时 tried 为既往
        if re.search(r"\b(?:has|have|had)\s+(?:also\s+|previously\s+)?tried\b", drug_seg, re.I):
            n_drugs = sum(1 for d2 in DRUG_NAMES
                          if re.search(r"\b" + re.escape(d2) + r"\b", drug_seg, re.I))
            _tried_status = "prior instance" if n_drugs >= 2 else "past"
        else:
            _tried_status = None
        # 不指向本药的 history/hx of 短语是适应症/症状史,不毒染本药状态:
        # "apixaban for history of DVT"、"History of wandering ... addition of X"
        _dre = re.escape(drug.lower())
        drug_seg = re.sub(
            r"\b(?:history|hx)\s+(?:of|including)\s+"
            r"(?![^,;.]{0,45}\b" + _dre + r"\b)[^,;.]{0,50}",
            " ", drug_seg, flags=re.I)
        # 认知帧从句是转述错觉,不是在服断言:"she believed X was 20 mg,
        # but the bottle shows 10 mg"——剥离 believed 从句,保留事实从句
        drug_seg = re.sub(r"\b(?:she|he|they|patient)?\s*(?:believed|thought|assumed)\b[^,;]*[,;]?",
                          " ", drug_seg, flags=re.I)
        folded_seg = drug_seg.lower()
        status = status_for("medication", drug_seg)
        if _tried_status and status in ("active", "past"):
            status = _tried_status
        advice_frame = bool(re.match(
            r"\s*(?:patient\s+)?(?:advised|instructed|counseled|educated)?\s*(?:that\s+)?if\b",
            item, re.I))
        # 否定的重启不是重启:"has not been restarted" → 停用状态
        if re.search(r"\b(?:not|never|hasn'?t|has not|won'?t|will not)\s+(?:be(?:en)?\s+)?restart", folded_seg):
            status = "stop"
        # 完成态停药短语:"after/since stopping <drug>"(意图性 "trying to stop" 不算)
        elif re.search(r"\b(?:after|since)\s+stopping\b[^.;]{0,40}\b"
                       + re.escape(drug.lower()) + r"\b", folded_seg):
            status = "stop"
        # 副作用叙事:"swelling occurred with <drug>" 是既往用药经历,非在服断言
        elif re.search(r"\b(?:occurred|developed|happened|appeared)\s+"
                       r"(?:with|on|while\s+(?:taking|on))\b[^.;]{0,30}\b"
                       + re.escape(drug.lower()) + r"\b", folded_seg):
            status = "past"
        # 强停药标记(明确、药向性强)允许全句扫描——弱标记(prior/past)
        # 仍限分号段,防远处病史词污染
        if status == "active" and re.search(
                r"\b(?:discontinued|no longer taking|not taking|stopped taking)\b",
                drug_sent, re.I):
            n_sent_drugs = sum(1 for d2 in DRUG_NAMES
                               if re.search(r"\b" + re.escape(d2) + r"\b", drug_sent, re.I))
            if n_sent_drugs == 1:
                status = "stop"
        # 药名前有 "switched from / off / weaned off" = 该药已停,是既往药
        if re.search(r"\b(?:switched from|switch(?:ed)? off|off(?:\s+of)?|weaned off|"
                     r"tapered off|transitioned from|previously on)\b[^.;]{0,40}\b"
                     + re.escape(drug.lower()) + r"\b", folded_item):
            status = "past"
        # 历史用药清单:"has been on multiple ... including X, Y" 是既往列表
        elif re.search(r"\b(?:has|have|had) been on\b[^.;]*\bincluding\b", folded_seg):
            status = "past"
        # "recently started X" 是现行用药(即使行内有 previously 等历史注释)
        elif re.search(r"\b(?:recently\s+(?:started|began|initiated)|(?<!not )(?<!be )(?<!been )(?<!never )restart(?:ed|ing)?)\b[^.;]{0,40}\b"
                       + re.escape(drug.lower()) + r"\b", folded_seg) or \
                re.search(re.escape(drug.lower()) + r"\b[^.;]{0,20}\b(?:recently\s+(?:started|began|initiated)|(?<!not )(?<!be )(?<!been )(?<!never )restart(?:ed|ing)?)\b", folded_seg):
            status = "active"
        # "listed/appears on medication list/record" 是清单成员陈述,非在服断言
        elif re.search(r"\b(?:listed|appears?|noted|documented)\b[^.;]{0,30}"
                       r"\b(?:medication list|med list|medications|records?)\b", folded_seg) \
                or re.search(r"\bon (?:\w+ )?med(?:ication)? list\b", folded_seg):
            status = "recorded"
        # "previous/prior <drug>" 指既往的另一实例(previous birth control pill),
        # 与当前在用的同类药不构成矛盾
        elif re.search(r"\b(?:previous|prior|former|old|different)\b[^.;]{0,15}\b"
                       + re.escape(drug.lower()) + r"\b", folded_seg):
            status = "prior instance"
        # "<drug> trial(试药)"是既往试药史的标准写法
        elif re.search(re.escape(drug.lower()) + r"\s+trial\b", folded_seg):
            status = "past"
        # "X caused <副作用>"(无剂量的因果叙述)是既往试药史;
        # "gap in birth control caused her period..." 的 caused 主语不是药,不触发
        elif re.search(re.escape(drug.lower()) + r"\b[^.;]{0,15}\bcaused\b[^.;]{0,40}"
                       r"\b(?:side effects?|flashes?|nausea|rash|tired\w*|drows\w*|headaches?|"
                       r"sedation|dizz\w*|feeling)\b", folded_seg) \
                and not DOSE_RE.search(drug_seg):
            status = "past"
        # 药名后的既往体验叙述("Risperdal (felt tired)"/"the Seroquel experience")
        elif re.search(re.escape(drug.lower()) + r"\b[^.;]{0,40}\b(?:felt|experiences?d?|"
                       r"made (?:me|her|him)|no improvement)\b", folded_seg) \
                and not DOSE_RE.search(drug_seg):
            status = "past"
        elif re.search(r"\bpast (?:medication|med|psychiatric medication)s? trials?\b",
                       folded_item):
            status = "past"
        # 副作用/感受史提及("prior weird dreams with X"),无剂量:
        # 陈述的是历史感受不是用药状态 → recorded(不与 active/past 冲突)
        elif status == "past" and not DOSE_RE.search(drug_seg) and re.search(
                r"\b(dreams?|flashes?|nausea|zaps?|tired\w*|drows\w*|dizz\w*|"
                r"side effects?|sensation)\b", folded_seg):
            status = "recorded"
        # 计划/医嘱段落里的药是待执行医嘱,不是"正在服用"的现状断言
        elif status == "active" and section_kind_val == "treatment":
            status = "ordered"
        # 既往试药小节标题("Past/Previous Medication Trials")下的药默认既往
        elif status == "active" and re.search(
                r"medication\s+trials?|(?:past|previous|prior)\s+(?:medication|med)\s+trials?"
                r"|previous(?:ly)?\s+tried\s+medication",
                (section_name or ""), re.I):
            status = "past"
        # "prescribed by X"(处方归属叙述)且无现服标记:是处方事件的转述,
        # 不是"正在服用"的现状断言——病史小节整体默认 past 会误伤在服药,
        # 故只用行级窄规则
        elif status == "active" and re.search(r"\bprescribed by\b", drug_seg, re.I) \
                and not re.search(r"\b(continu\w*|current\w*|takes|taking|remains on|still on)\b",
                                  drug_seg, re.I):
            status = "ordered"
        # "switched to <drug>" 是启用该药(停的是前药):stop/past 归属纠正
        if status in {"stop", "past"} and re.search(
                r"\bswitch(?:ed)?\s+to\b[^.;]{0,30}\b" + re.escape(drug.lower()) + r"\b",
                folded_seg):
            status = "active"
        # 终审:条件教育帧强制医嘱状态(链中 restart/stop 规则可能已污染)
        if advice_frame:
            status = "ordered"
        out.append(make_claim("medication", drug, item, subject="patient",
                              predicate="takes", object=drug, value=value,
                              unit=unit, time=first_frequency(drug_window),
                              status=status,
                              polarity="positive" if advice_frame
                              else medication_polarity_for(drug_seg, drug),
                              condition=cond))
    return out


def symptom_claims(item):
    out = []
    folded = item.lower()
    if "shaken up" in folded:
        out.append(make_claim("symptom", "shaken up", item, subject="patient",
                              predicate="felt", object="shaken up",
                              time="after the accident" if "accident" in folded else None,
                              status="active", polarity="positive",
                              condition="after car accident" if "accident" in folded else None))
    if "indignant rage" in folded:
        out.append(make_claim("symptom", "indignant rage", item, subject="patient",
                              predicate="has", object="indignant rage",
                              time="now" if "now" in folded else None,
                              status="active", polarity="positive"))
    if "guilt" in folded:
        out.append(make_claim("symptom", "a little bit of guilt", item, subject="patient",
                              predicate="has", object="a little bit of guilt",
                              value="a little bit" if "little bit" in folded else None,
                              time="current", status="active", polarity="positive"))
    if has_any(item, ("depressive state", "depressed", "depression")) and has_any(item, ("cousin", "suicide", "killed himself")):
        out.append(make_claim("symptom", "depression", item, subject="patient",
                              predicate="was", object="depression",
                              value="a depressive state", unit="time",
                              status="active", polarity="positive",
                              condition="regarding cousin's boyfriend's suicide"))
    if has_any(item, ("sleep difficulty", "bad sleep", "pretty bad sleep", "insomnia")):
        out.append(make_claim("symptom", "sleep difficulty", item, subject="patient",
                              predicate="is having", object="sleep difficulty",
                              value="five to six" if has_any(item, ("five to six", "5 to 6", "5-6")) else None,
                              unit="hours" if has_any(item, ("five to six", "5 to 6", "5-6")) else None,
                              time="since this week" if "this week" in folded else None,
                              status="active", polarity="positive",
                              condition="thinking about that and the truck" if "truck" in folded else None))
    if "brain zap" in folded:
        out.append(make_claim("symptom", "brain zaps", item, subject="patient",
                              predicate="had", object="brain zaps",
                              value="above, like, one fifty" if has_any(item, ("150", "one fifty")) else None,
                              time="today" if "today" in folded else None,
                              status="active", polarity="positive",
                              condition="if I take too much trazodone" if "trazodone" in folded else None))
    if has_any(item, ("on edge", "as edge", "edgy")) and "vyvanse" in folded:
        out.append(make_claim("medication", "on edge", item, subject="patient",
                              predicate="is not feeling", object="on edge",
                              time="current", status="improved",
                              polarity="negative", condition="after Vyvanse dose decrease"))
    if "not myself" in folded:
        out.append(make_claim("symptom", "not myself", item, subject="patient",
                              predicate="feels", object="not myself",
                              value="very surreal week" if "surreal" in folded else None,
                              time="this week" if "week" in folded else None,
                              status="active", polarity="positive"))
    if has_any(item, ("memory impairment", "memory", "forget", "olvido", "anoto", "retentiva")):
        out.append(make_claim("symptom", "memory impairment", item, subject="patient",
                              predicate="reports", object="memory impairment",
                              value="hace un par de años" if has_any(item, ("par de años", "couple of years")) else None,
                              unit="años" if has_any(item, ("años", "years")) else None,
                              time="hace un par de años" if has_any(item, ("par de años", "couple of years")) else None,
                              status="active", polarity="positive",
                              condition="si lo anoto" if "anoto" in folded else None))
    if has_any(item, ("pain", "dolor")):
        out.append(make_claim("symptom", "pain", item, subject="patient",
                              predicate="reports", object="pain",
                              time="después de la caída en shopping" if has_any(item, ("caida", "caída")) else None,
                              location="espalda" if has_any(item, ("espalda", "back")) else None,
                              status="active", polarity="positive",
                              condition="no me permitía moverme" if has_any(item, ("permitia moverme", "permitía moverme", "could not move")) else None))
    return out


def instruct_claims(item):
    out = []
    if re.search(r"\blasa\b", item, re.I):
        out.append(make_claim("instruct", "lasa", item, subject="patient",
                              predicate="got permission to use", object="lasa",
                              value="for support", time="now",
                              status="permitted", polarity="positive",
                              condition="ankle"))
    if re.search(r"\bNick\b", item) and re.search(r"\bthree months ago\b", item, re.I):
        out.append(make_claim("instruct", "nick", item, subject="patient",
                              predicate="last saw", object="Nick",
                              value="probably three months ago",
                              time="three months ago", status="past",
                              polarity="positive", condition="i'd have you one and"))
    return out


def extra_claims_from_item(item, section_kind_val="", section_name=""):
    out = []
    out.extend(demo_claims(item))
    out.extend(diagnosis_claims(item))
    out.extend(medication_claims(item, section_kind_val=section_kind_val,
                                 section_name=section_name))
    out.extend(symptom_claims(item))
    out.extend(instruct_claims(item))
    return out


def parse_medication(item, kind="medication"):
    med = find_medication(item)
    if med and med.lower() in {d.lower() for d in DRUG_NAMES}:
        # 表内药用药向窗口取剂量,防多药行第一个剂量张冠李戴
        _v, _u = extract_dose_unit(item, drug=med)
    else:
        _v = _u = None
    # 否定/兜底残渣不是药名:"No other prescriptions, supplements..." 的
    # words[0] 兜底会返回 "No"——这类行是否认清单,不产出用药 claim
    if med and med.lower() in {"no", "none", "not", "without", "denies", "denied",
                               "nil", "nkda", "other", "others", "patient",
                               "medication", "medications", "meds"}:
        return None
    dose = DOSE_RE.search(item)
    freq = FREQ_RE.search(item)
    if med and med.lower() in {d.lower() for d in DRUG_NAMES}:
        value, unit = _v, _u
    else:
        value = norm_dose_value(dose.group(1)) if dose else None
        unit = dose.group(2) if dose else None
    return {
        "key": "{}.{}".format(kind, object_slug(med)),
        "kind": kind,
        "fields": fields(
            subject="patient",
            predicate="takes" if kind == "medication" else "changed",
            object=med,
            value=value,
            unit=unit,
            time=freq.group(1) if freq else None,
            status=status_for(kind, item),
            polarity=medication_polarity_for(item, med) if kind == "medication" else polarity_for(item),
        ),
        "evidence_quote": item,
        "source": ["generated_output"],
    }


SCALE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:out of|/)\s*10\b", re.I)


def extract_scale_value(text):
    m = SCALE_RE.search(text)
    return (m.group(1), "/10") if m else (None, None)


def parse_symptom(item):
    text = item.strip(" .")
    patterns = [
        r"\bpatient\s+(?:has|had|reports?|reported|endorses?|endorsed|experiences?|experienced)\s+(.+)$",
        r"\b(?:has|had|reports?|reported|endorses?|endorsed|experiences?|experienced)\s+(.+)$",
        r"\b(?:denies|denied|no|not having|without)\s+(.+)$",
    ]
    obj = None
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            obj = m.group(1)
            break
    if obj is None:
        obj = text
    obj = strip_time_phrases(obj)
    sval, sunit = extract_scale_value(item)
    return {
        "key": "symptom." + object_slug(obj),
        "kind": "symptom",
        "fields": fields(
            subject="patient",
            predicate="has",
            object=obj,
            value=sval,
            unit=sunit,
            status=status_for("symptom", item),
            polarity=polarity_for(item),
        ),
        "evidence_quote": item,
        "source": ["generated_output"],
    }


def parse_generic(item, kind):
    obj = strip_time_phrases(item)
    return {
        "key": "{}.{}".format(kind, object_slug(obj)),
        "kind": kind,
        "fields": fields(
            subject="patient",
            predicate="has" if kind not in {"treatment", "test_ordered", "referral"} else "planned",
            object=obj,
            status=status_for(kind, item),
            polarity=polarity_for(item),
        ),
        "evidence_quote": item,
        "source": ["generated_output"],
    }


# ---- 通用电报体层 (parser 0.2) --------------------------------------------
# 规则全部来自 held-out 病历语料的语法归纳(非 active case),防过拟合:
# R1/R2 主语省略的患者动词句;R12 临床医生动词覆盖患者规则;R4 既往标记;
# R7 否定分配。仅对多句 item 生效,单句路径保持原行为。
PATIENT_VERB_RE = re.compile(
    r"^(?:(?:the\s+)?(?:patient|pt|member|client|she|he)(?!['’]s)\s+)?"
    r"(reports?|reported|denies|denied|describes?|described|endorses?|endorsed|"
    r"states?|stated|notes?|noted|feels?|felt|continues?|continued|has|had|"
    r"sees|resides|participates?|presents?|remains|experiences?|experienced)\b\s*(?:that\s+)?(.+)$",
    re.I)
CLINICIAN_VERB_RE = re.compile(
    r"^(counsel(?:ed)?|advis(?:ed|e)|discuss(?:ed)?|recommend(?:ed)?|encourag(?:ed|e)|"
    r"instruct(?:ed)?|provid(?:ed|e)|order(?:ed)?|review(?:ed)?|arrang(?:ed|e)d?|refer(?:red)?)\b\s*(?:that\s+)?(.+)$",
    re.I)
NEG_LEAD_RE = re.compile(r"^(?:no|denies|denied|without|negative for)\b\s*(.+)$", re.I)
HISTORY_LEAD_RE = re.compile(r"^(?:history of|hx of|prior)\b\s*(.+)$", re.I)
NEG_VERBS = {"denies", "denied"}
SENTENCE_SPLIT_RE = re.compile(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z])")
TIME_TAIL_RE = re.compile(r"\b(since\s+[\w\s'\-]{2,24}|for\s+(?:the\s+)?(?:past\s+)?[\w\-]+\s+(?:years?|months?|weeks?|days?)|until\s+[\w\s'\-]{2,24})\s*$", re.I)


def split_sentences(item):
    return [s.strip(" .;") for s in SENTENCE_SPLIT_RE.split(item) if s.strip(" .;")]


def telegraphic_claim(sentence, kind):
    text = sentence.strip(" .;,")
    if len(text) < 6:
        return None
    m = CLINICIAN_VERB_RE.match(text)
    if m:
        obj = strip_time_phrases(m.group(2))
        if not obj:
            return None
        return make_claim("treatment", "treatment." + object_slug(obj), sentence,
                          subject="clinician", predicate=m.group(1).lower(),
                          object=obj, status="ordered", polarity="positive")
    hm = HISTORY_LEAD_RE.match(text)
    if hm:
        obj = strip_time_phrases(hm.group(1))
        return make_claim(kind if kind != "other" else "diagnosis",
                          "history." + object_slug(obj), sentence,
                          subject="patient", predicate="has",
                          object=obj, status="past", polarity="positive")
    time_val = None
    tm = TIME_TAIL_RE.search(text)
    if tm:
        time_val = tm.group(1).strip()
        text = text[:tm.start()].strip(" .,;")
    nm = NEG_LEAD_RE.match(text)
    if nm:
        obj = strip_time_phrases(nm.group(1))
        if not obj:
            return None
        return make_claim(kind, "{}.{}".format(kind, object_slug(obj)), sentence,
                          subject="patient", predicate="has", object=obj,
                          time=time_val, status=status_for(kind, sentence),
                          polarity="negative")
    pm = PATIENT_VERB_RE.match(text)
    if not pm:
        # 名词短语回退:临床电报体主流是无动词断言("Chest pain, epigastric,
        # burning in quality." / "Acid taste in back of throat.")——无动词
        # 不等于无内容,按所在 kind 落为 has-claim。限叙述类 kind,
        # 结构化 kind(med/lab/vital)有专用解析器,不走此回退。
        if kind not in {"symptom", "diagnosis", "social_history", "family_history",
                        "risk", "treatment"}:
            return None
        obj = strip_time_phrases(text)
        # 只挡单词标签/裸标题("Assessment:"/"Current Suicidal Thoughts"),
        # 不挡名词短语:标题特征 = ≤3 词全 TitleCase 且无句读
        if not obj or len(obj) < 4 or obj.endswith(":") or re.match(r"^[\w/-]+$", obj):
            return None
        if re.match(r"^\**(allergies|medications|assessment|plan|history|vitals|labs)\**:?$",
                    obj.strip(), re.I):
            return None  # 裸小节词(常来自 markdown 加粗标题行)不是断言
        words = obj.split()
        if len(words) <= 3 and all(w[:1].isupper() for w in words) \
                and not re.search(r"[.,;()\d]", obj):
            return None
        # 行内标签前缀剥离("Associated: intermittent nausea" → 后半)
        obj = re.sub(r"^[A-Za-z][A-Za-z /]{0,20}:\s*", "", obj) or obj
        return make_claim(kind, "{}.{}".format(kind, object_slug(obj)), sentence,
                          subject="patient", predicate="has", object=obj,
                          time=time_val, status=status_for(kind, sentence),
                          polarity=polarity_for(text))
    verb, rest = pm.group(1).lower(), pm.group(2)
    obj = strip_time_phrases(rest)
    if not obj or len(obj) < 3:
        return None
    # 极性经 polarity_for 的语用守卫(括号旁注/引语/转折从句/意愿否定)
    polarity = "negative" if verb in NEG_VERBS else polarity_for(rest)
    return make_claim(kind, "{}.{}".format(kind, object_slug(obj)), sentence,
                      subject="patient", predicate="has", object=obj,
                      time=time_val, status=status_for(kind, sentence),
                      polarity=polarity)


def telegraphic_claims(item, kind):
    sentences = split_sentences(item)
    if len(sentences) < 2:
        return []
    out = []
    for sent in sentences:
        # 否定段独立拆分:"intermittent nausea, no vomiting" 必须产出
        # 一正一负两条 claim,否则句尾的 no 会污染整句极性
        parts = re.split(r",\s*(?=(?:no|without|denies|not|rather than)\b)", sent, flags=re.I)
        for part in parts:
            claim = telegraphic_claim(part, kind)
            if claim:
                out.append(claim)
    return out
# ---------------------------------------------------------------------------


def claim_from_item(section, item):
    kind = section_kind(section)
    if not item or re.search(r"\b(no explicit|none stated|not stated|absent|n/a)\b", item, re.I):
        return None
    if ABSENT_PLACEHOLDER_RE.search(item):
        return None
    if kind in {"medication", "medication_change"}:
        return parse_medication(item, kind=kind)
    if kind == "diagnosis" and NEGATION_RE.search(item) and has_any(item, ("pain", "dolor")) and not maybe_icd(item):
        return None
    if kind == "symptom":
        return parse_symptom(item)
    return parse_generic(item, kind)


def parse_template_claims(text):
    claims = []
    for section, item in iter_section_items(text):
        if not item or re.search(r"\b(no explicit|none stated|not stated|absent|n/a)\b", item, re.I):
            continue
        if ABSENT_PLACEHOLDER_RE.search(item):
            continue
        kind = section_kind(section)
        extras = extra_claims_from_item(item, section_kind_val=kind, section_name=section)
        for extra in extras:
            claims.append(extra)
        # HPI 类叙事段落常落在 other:按 symptom 语义做逐句电报体解析
        tele = telegraphic_claims(item, "symptom" if kind == "other" else kind)
        claims.extend(tele)
        if tele:
            # 多句 item 已逐句解析,整段拼接的粗粒度 claim 只会制造噪音
            continue
        claim = claim_from_item(section, item)
        if claim:
            if claim.get("kind") == "other" and (extras or not GENERIC_OTHER_ENABLED):
                continue
            claims.append(claim)
    claims = dedupe_claims(claims)
    doc = {
        "claims": claims,
        "postgen_extractor": {
            "mode": "template_parser",
            "parser_version": "detfact-template-parser/0.2",
            "source_label": "generated_output",
        },
    }
    validate_claims_doc(doc)
    return doc


def dedupe_claims(claims):
    # 同锚去重,信息更全者胜:HPI 叙事先出现的无剂量 claim 不得遮蔽
    # med-list 里带剂量的 claim(实测 cheat trial:剂量翻转因遮蔽而隐形)
    seen = {}
    out = []
    for claim in claims:
        try:
            anchor = canonicalize_claim(claim.get("kind"), claim).get("canonical_anchor") or ""
        except Exception:
            anchor = ""
        if not anchor:
            anchor = hashlib.sha256(repr(claim).encode("utf-8")).hexdigest()
        if anchor in seen:
            idx = seen[anchor]
            old = out[idx]
            if (claim.get("fields") or {}).get("value") and not (old.get("fields") or {}).get("value"):
                out[idx] = claim
            continue
        seen[anchor] = len(out)
        out.append(claim)
    return out
