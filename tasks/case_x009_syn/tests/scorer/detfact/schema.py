import copy

from detfact.common import sha256_doc

FIELD_NAMES = [
    "subject", "predicate", "object", "value", "unit", "time",
    "location", "owner", "status", "polarity", "condition",
]
CLAIM_REQUIRED = {"key", "kind", "fields"}
FACT_REQUIRED = {
    "key", "canonical_anchor", "kind", "identity_fields",
    "check_fields", "coverage_fields", "fields",
}
REPORT_REQUIRED = {
    "protocol_version", "factset", "rules", "evaluator", "verdict",
    "counts", "per_claim", "report_sha256",
}


class ValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_scalar_or_null(value):
    return value is None or isinstance(value, (str, int, float, bool))


def validate_fields(fields, path="fields"):
    if not isinstance(fields, dict):
        raise ValidationError("bad_fields", path + " must be an object")
    missing = [name for name in FIELD_NAMES if name not in fields]
    extra = sorted(set(fields) - set(FIELD_NAMES))
    if missing:
        raise ValidationError("missing_field", path + " missing " + ", ".join(missing))
    if extra:
        raise ValidationError("extra_field", path + " has unknown field " + ", ".join(extra))
    for name in FIELD_NAMES:
        if not _is_scalar_or_null(fields.get(name)):
            raise ValidationError("bad_field_value", path + "." + name + " must be scalar or null")


def validate_claim(claim, path="claim"):
    if not isinstance(claim, dict):
        raise ValidationError("bad_claim", path + " must be an object")
    missing = sorted(CLAIM_REQUIRED - set(claim))
    if missing:
        raise ValidationError("missing_claim_key", path + " missing " + ", ".join(missing))
    if claim.get("key") is not None and not isinstance(claim.get("key"), str):
        raise ValidationError("bad_claim_key", path + ".key must be string or null")
    if claim.get("kind") is not None and not isinstance(claim.get("kind"), str):
        raise ValidationError("bad_claim_kind", path + ".kind must be string or null")
    validate_fields(claim.get("fields"), path + ".fields")
    source = claim.get("source")
    if source is not None and (not isinstance(source, list) or not all(isinstance(x, str) for x in source)):
        raise ValidationError("bad_claim_source", path + ".source must be a string array")


def validate_claims_doc(doc):
    if isinstance(doc, list):
        claims = doc
    elif isinstance(doc, dict):
        claims = doc.get("claims")
    else:
        raise ValidationError("bad_claims_doc", "claims body must be an object or list")
    if not isinstance(claims, list):
        raise ValidationError("bad_claims", "claims must be a list")
    for i, claim in enumerate(claims):
        validate_claim(claim, "claims[{}]".format(i))
    opts = {} if isinstance(doc, list) else doc.get("options", {})
    if opts is not None:
        if not isinstance(opts, dict):
            raise ValidationError("bad_options", "options must be an object")
        check_fields = opts.get("check_fields")
        if check_fields is not None and check_fields not in {"factset", "stable", "all", "none"}:
            raise ValidationError("bad_check_fields", "invalid check_fields option")
        strict = opts.get("strict_extra_fields")
        if strict is not None and not isinstance(strict, bool):
            raise ValidationError("bad_strict_extra_fields", "strict_extra_fields must be boolean")
    return claims


def _validate_string_list(value, path):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValidationError("bad_string_list", path + " must be a string array")


def _computed_factset_sha(doc):
    tmp = copy.deepcopy(doc)
    tmp.setdefault("factset", {})["sha256"] = None
    return sha256_doc(tmp)


def validate_factset(doc, require_hash=True):
    if not isinstance(doc, dict):
        raise ValidationError("bad_factset", "FactSet must be an object")
    if doc.get("protocol_version") != "detfact/factset/v1":
        raise ValidationError("bad_protocol", "unsupported FactSet protocol")
    meta = doc.get("factset")
    if not isinstance(meta, dict):
        raise ValidationError("bad_factset_meta", "factset must be an object")
    for key in ("id", "version"):
        if not isinstance(meta.get(key), str) or not meta.get(key):
            raise ValidationError("bad_factset_meta", "factset." + key + " must be a non-empty string")
    facts = doc.get("facts")
    if not isinstance(facts, list):
        raise ValidationError("bad_facts", "facts must be a list")
    rules = doc.get("rules")
    if not isinstance(rules, dict) or not rules.get("version"):
        raise ValidationError("bad_rules", "rules.version is required")
    for i, fact in enumerate(facts):
        path = "facts[{}]".format(i)
        if not isinstance(fact, dict):
            raise ValidationError("bad_fact", path + " must be an object")
        missing = sorted(FACT_REQUIRED - set(fact))
        if missing:
            raise ValidationError("missing_fact_key", path + " missing " + ", ".join(missing))
        if not isinstance(fact.get("canonical_anchor"), str) or not fact.get("canonical_anchor"):
            raise ValidationError("bad_canonical_anchor", path + ".canonical_anchor must be a non-empty string")
        for name in ("identity_fields", "check_fields", "coverage_fields"):
            _validate_string_list(fact.get(name), path + "." + name)
        validate_fields(fact.get("fields"), path + ".fields")
    declared = meta.get("sha256")
    if require_hash and declared:
        computed = _computed_factset_sha(doc)
        if declared != computed:
            raise ValidationError("factset_hash_mismatch", "factset.sha256 does not match canonical bytes")


def validate_report(doc):
    if not isinstance(doc, dict):
        raise ValidationError("bad_report", "report must be an object")
    missing = sorted(REPORT_REQUIRED - set(doc))
    if missing:
        raise ValidationError("missing_report_key", "report missing " + ", ".join(missing))
    if doc.get("protocol_version") != "detfact/report/v1":
        raise ValidationError("bad_report_protocol", "unsupported report protocol")
    if doc.get("verdict") not in {"pass", "fail"}:
        raise ValidationError("bad_verdict", "verdict must be pass or fail")
    if not isinstance(doc.get("counts"), dict):
        raise ValidationError("bad_counts", "counts must be an object")
    if not isinstance(doc.get("per_claim"), list):
        raise ValidationError("bad_per_claim", "per_claim must be a list")
