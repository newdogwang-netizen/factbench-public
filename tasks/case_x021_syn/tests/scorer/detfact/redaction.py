import copy


def _redact_reason(reason):
    reason = copy.deepcopy(reason)
    fields = reason.get("fields")
    if isinstance(fields, list):
        redacted = []
        for item in fields:
            if not isinstance(item, dict):
                continue
            redacted.append({
                "field": item.get("field"),
                "claim": item.get("claim"),
                "fact": "<redacted>" if "fact" in item else None,
            })
        reason["fields"] = redacted
    return reason


def redact_report(report):
    """Remove holdout-revealing details from an evaluation report."""
    out = copy.deepcopy(report)
    out["gold_redacted"] = True
    out.pop("per_fact", None)
    for row in out.get("per_claim", []):
        if row.get("matched_fact_key"):
            row["matched_fact_key"] = "<redacted>"
        row["reasons"] = [_redact_reason(r) for r in row.get("reasons", [])]
    return out
