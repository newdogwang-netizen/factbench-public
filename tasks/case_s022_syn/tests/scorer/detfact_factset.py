#!/usr/bin/env python3
"""Export audit consensus JSON into an immutable detfact candidate FactSet.

This is the bridge from the authoring/audit stage to the deterministic scorer
stage. It does not decide clinical truth; it packages already-reviewed or
selected candidate facts into the format the scorer consumes.
"""
import argparse
import glob
import hashlib
import json
import os

from detfact_consensus import FIELDS, STABLE_FIELDS

PROTOCOL_VERSION = "detfact/factset/v1"
RULES_VERSION = "detfact-rules/0.7"
# fields compared for wrong_fact whenever the gold fact carries a value for them,
# in addition to the always-checked stable fields
EXTRA_CHECK_FIELDS = ["value", "unit", "time", "status"]
DEFAULT_COVERAGE_FIELDS = [
    "subject", "object", "value", "unit", "time", "location",
    "owner", "status", "polarity", "condition",
]


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


def nonempty_fields(fields, allowed):
    return [f for f in allowed if fields.get(f) not in (None, "")]


def fact_from_audit_row(row):
    fields = row.get("fields") or {}
    canonical_anchor = row.get("canonical_anchor") or row.get("key")
    coverage_fields = nonempty_fields(fields, DEFAULT_COVERAGE_FIELDS)
    evidence = []
    for ev in row.get("evidence", []):
        evidence.append({
            "model": ev.get("model"),
            "key": ev.get("key"),
            "source": ev.get("source") or [],
            "quote": ev.get("quote") or "",
        })
    return {
        "key": row.get("display_key") or row.get("key") or canonical_anchor,
        "canonical_anchor": canonical_anchor,
        "kind": row.get("canonical_kind") or row.get("kind"),
        "subtype": row.get("subtype"),
        "identity_fields": ["canonical_anchor"],
        "check_fields": sorted(set(STABLE_FIELDS) | {
            f for f in EXTRA_CHECK_FIELDS if fields.get(f) not in (None, "")
        }),
        "coverage_fields": coverage_fields,
        "fields": {f: fields.get(f) for f in FIELDS},
        "evidence": evidence,
        "provenance": {
            "audit_id": row.get("id"),
            "support": row.get("support"),
            "models": row.get("models") or [],
            "status": row.get("status"),
            "field_stable": bool(row.get("field_stable")),
            "strict_field_stable": bool(row.get("strict_field_stable")),
        },
    }


def make_factset(audit_doc, source_path=None, statuses=None, version="1", reviewed=False):
    statuses = set(statuses or ["certain"])
    include_all = "all" in statuses
    facts = []
    for row in audit_doc.get("facts", []):
        if not include_all and row.get("status") not in statuses:
            continue
        facts.append(fact_from_audit_row(row))

    source_hash = file_sha256(source_path) if source_path else None
    case_id = audit_doc.get("case") or "unknown-case"
    doc = {
        "protocol_version": PROTOCOL_VERSION,
        "factset": {
            "id": case_id,
            "version": str(version),
            "sha256": None,
            "reviewed": bool(reviewed),
        },
        "source": {
            "kind": "audit_consensus",
            "case": case_id,
            "locale": audit_doc.get("locale"),
            "path": source_path,
            "sha256": source_hash,
            "stats": audit_doc.get("stats") or {},
        },
        "selection": {
            "statuses": sorted(statuses),
            "fact_count": len(facts),
            "note": "candidate factset; promote to reviewed only after human adjudication",
        },
        "rules": {
            "version": RULES_VERSION,
            "identity": "canonical_anchor exact equality",
            "field_normalization": "detfact_consensus.norm_field",
        },
        "facts": facts,
    }
    tmp = json.loads(json.dumps(doc, ensure_ascii=False))
    tmp["factset"]["sha256"] = None
    doc["factset"]["sha256"] = sha256_doc(tmp)
    return doc


def write_immutable(path, doc, force=False):
    body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
        if old != body and not force:
            raise SystemExit("refusing to overwrite different factset: " + path + " (use --force for candidate rebuilds)")
        if old == body:
            return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="audit_site/data/*.json", help="audit JSON path or glob")
    ap.add_argument("--out-dir", default="factsets_candidate")
    ap.add_argument("--version", default="1")
    ap.add_argument("--statuses", default="certain",
                    help="comma-separated audit statuses to include, or all")
    ap.add_argument("--reviewed", action="store_true",
                    help="mark as human reviewed; do not use for raw model consensus")
    ap.add_argument("--force", action="store_true",
                    help="allow rebuilding candidate files with changed bytes")
    args = ap.parse_args()

    paths = sorted(glob.glob(args.data)) or ([args.data] if os.path.isfile(args.data) else [])
    if not paths:
        raise SystemExit("no audit JSON matched: " + args.data)
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    for path in paths:
        audit = json.load(open(path))
        doc = make_factset(audit, source_path=path, statuses=statuses,
                           version=args.version, reviewed=args.reviewed)
        out = os.path.join(args.out_dir, doc["factset"]["id"], "v" + str(args.version) + ".json")
        changed = write_immutable(out, doc, force=args.force)
        print(json.dumps({
            "factset": doc["factset"]["id"],
            "version": doc["factset"]["version"],
            "facts": len(doc["facts"]),
            "sha256": doc["factset"]["sha256"],
            "path": out,
            "changed": changed,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
