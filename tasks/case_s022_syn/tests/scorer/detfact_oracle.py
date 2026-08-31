#!/usr/bin/env python3
"""语义微判定层(semantic micro-oracle)。

原则(2026-08-26 尺寸不变性实验 24/24 通过后落地):
- LLM 只回答原子是非题:"两个短字段值是否同义/同值",且只在确定性归一化
  判'不同'之后被咨询——只能把假'不同'翻成'等价',不能制造'不同';
- 多模型法定人数:不同家族/规模模型答案不一致即弃权(维持死规则原判),
  尺寸不变性逐题强制验证;
- 判例缓存:每对写法只问一次,判定写入 audit_site/equivalence_table.json
  (人类可审计/可否决);重放全走查表,确定性与报告哈希完整保留;
- 默认关闭:仅当环境变量 DETFACT_ORACLE=1 时启用;关闭时行为与纯死规则
  完全一致(测试/离线重放安全)。

表条目: {"field|a|b": {"verdict": "same"|"different"|"abstain",
                        "models": [...], "at": iso}}
键中 a,b 按字典序排序(等价关系对称)。
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
# 只对这些字段启用等价判定;polarity/status 有专门逻辑,保持纯确定性
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
    """锚同义判定(单向契约:仅 SAME 用于救援,假 DIFFERENT 只损召回)。
    打分路径永远只查表(live=False),表由离线构建器扩充——
    保证打分零 LLM 调用、可重放。验证实验 2026-08-27:40 对 36 一致,
    假 SAME 0/20;唯一误判为无害方向(racing heart 判 DIFFERENT),
    预注册门槛(一致准确率 100%)未达、按单向风险面接入,偏离已记录。"""
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
    """True=等价(翻案) / False=确认不同 / None=弃权(维持死规则原判)。"""
    if field not in ORACLE_FIELDS:
        return None
    a, b = str(a).strip(), str(b).strip()
    if not a or not b or a == b:
        return None
    # 过长的自由文本不判(不是原子问题)
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
        verdict = "abstain"  # 不一致或有失败 → 弃权,且缓存弃权避免重复咨询
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
