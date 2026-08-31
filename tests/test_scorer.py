#!/usr/bin/env python3
"""Unit tests for the deterministic scorer (stdlib only).

Run:  python3 -m pytest tests/test_scorer.py
  or: python3 tests/test_scorer.py

No external dependencies. Exercises the core scoring logic:
  - supported / wrong_fact / hallucination / omitted verdicts
  - release-subset unmatched policy
  - omission does not fail by default
  - schema validation (bad claim shape, bad factset hash)
  - safety signals (drug swap, unsupported date, laterality)
  - end-to-end: oracle note scores critical_wrong == 0 on a real task
"""
import copy
import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scorer"))

from detfact_consensus import FIELDS, canonicalize_claim  # noqa: E402
from detfact_scorer import evaluate  # noqa: E402
from detfact.schema import ValidationError, validate_claims_doc  # noqa: E402
from detfact.factstore import FactstoreError, put_factset  # noqa: E402
from detfact.common import read_json  # noqa: E402
from detfact.safety import safety_signals  # noqa: E402
from detfact.template_parser import parse_template_claims  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def claim(kind="symptom", obj="pain", polarity="positive", key="symptom.pain",
          **field_overrides):
    fields = {
        "subject": "patient",
        "predicate": "has",
        "object": obj,
        "value": None,
        "unit": None,
        "time": None,
        "location": None,
        "owner": None,
        "status": "present",
        "polarity": polarity,
        "condition": None,
    }
    fields.update(field_overrides)
    return {
        "key": key,
        "kind": kind,
        "fields": fields,
        "evidence_quote": obj,
        "source": ["transcript"],
    }


def factset_for(base_claim, check_fields=None, **extra):
    """Build a minimal factset containing one fact derived from *base_claim*."""
    anchor = canonicalize_claim(base_claim["kind"], base_claim)["canonical_anchor"]
    doc = {
        "protocol_version": "detfact/factset/v1",
        "factset": {"id": "unit", "version": "1", "sha256": "unit"},
        "rules": {"version": "detfact-rules/0.7"},
        "facts": [{
            "key": base_claim["key"],
            "canonical_anchor": anchor,
            "kind": base_claim["kind"],
            "identity_fields": ["canonical_anchor"],
            "check_fields": check_fields or ["object", "polarity", "subject"],
            "coverage_fields": ["subject", "object", "polarity"],
            "fields": {f: base_claim["fields"].get(f) for f in FIELDS},
            "evidence": [],
        }],
    }
    doc.update(extra)
    return doc


# ---------------------------------------------------------------------------
# Core scoring verdicts
# ---------------------------------------------------------------------------

class ScorerVerdictTest(unittest.TestCase):
    def test_supported_claim_passes(self):
        c = claim()
        report = evaluate(factset_for(c), [c])
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["counts"]["supported"], 1)
        self.assertEqual(report["counts"]["omitted_facts"], 0)

    def test_wrong_fact_when_checked_field_conflicts(self):
        gold = claim()
        bad = claim(polarity="negative")
        report = evaluate(factset_for(gold), [bad])
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["counts"]["wrong_fact"], 1)

    def test_hallucination_when_anchor_is_absent(self):
        gold = claim()
        extra = claim(obj="dizziness", key="symptom.dizziness")
        report = evaluate(factset_for(gold), [extra])
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["counts"]["hallucination"], 1)
        self.assertEqual(report["counts"]["omitted_facts"], 1)

    def test_release_subset_unmatched_claim_is_not_in_factset(self):
        gold = claim()
        extra = claim(obj="dizziness", key="symptom.dizziness")
        factset = factset_for(gold, selection={
            "gold_scope": "high_precision_subset",
            "statuses": ["release"],
        })
        report = evaluate(factset, [extra])
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["counts"]["not_in_factset"], 1)
        self.assertEqual(report["counts"].get("hallucination", 0), 0)
        self.assertEqual(report["rules"]["unmatched_policy"], "not_in_factset")

    def test_unmatched_policy_can_force_hallucination(self):
        gold = claim()
        extra = claim(obj="dizziness", key="symptom.dizziness")
        factset = factset_for(gold, selection={"gold_scope": "high_precision_subset"})
        report = evaluate(factset, [extra], unmatched_policy="hallucination")
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["counts"]["hallucination"], 1)

    def test_omission_does_not_fail_by_default(self):
        gold = claim()
        report = evaluate(factset_for(gold), [])
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["counts"]["omitted_facts"], 1)

    def test_dose_flip_is_wrong_fact(self):
        """Positive-control: flipping a dose value must be caught."""
        gold = claim(kind="med", obj="sertraline", key="med.sertraline",
                     value="100", unit="mg", predicate="takes",
                     status="active")
        bad = claim(kind="med", obj="sertraline", key="med.sertraline",
                    value="50", unit="mg", predicate="takes",
                    status="active")
        report = evaluate(factset_for(gold, check_fields=[
            "object", "polarity", "subject", "value", "unit"]), [bad])
        self.assertEqual(report["counts"].get("wrong_fact", 0), 1)
        self.assertEqual(report["verdict"], "fail")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class SchemaValidationTest(unittest.TestCase):
    def test_claim_shape_is_checked(self):
        good = {"claims": [claim()]}
        # remove a required field → should raise
        del good["claims"][0]["fields"]["object"]
        with self.assertRaises(ValidationError) as cm:
            validate_claims_doc(good)
        self.assertEqual(cm.exception.code, "missing_field")

    def test_factset_hash_is_checked_on_put(self):
        doc = {
            "protocol_version": "detfact/factset/v1",
            "factset": {"id": "t", "version": "1", "sha256": "0" * 64},
            "rules": {"version": "detfact-rules/0.7"},
            "facts": [{
                "key": "k",
                "canonical_anchor": "symptom.patient.pain",
                "kind": "symptom",
                "identity_fields": ["canonical_anchor"],
                "check_fields": ["object"],
                "coverage_fields": ["object"],
                "fields": {f: None for f in FIELDS},
                "evidence": [],
            }],
        }
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FactstoreError) as cm:
                put_factset(root, doc)
        self.assertEqual(cm.exception.code, "factset_hash_mismatch")


# ---------------------------------------------------------------------------
# Safety signals
# ---------------------------------------------------------------------------

class SafetySignalTest(unittest.TestCase):
    """Safety signals are advisory — they report, not auto-fail."""

    def _claim_with_quote(self, quote, kind="symptom", obj=None):
        fields = {f: None for f in FIELDS}
        if obj:
            fields["object"] = obj
        return {
            "key": "test.1",
            "kind": kind,
            "fields": fields,
            "evidence_quote": quote,
            "source": ["transcript"],
        }

    def test_medication_near_miss_detected(self):
        """A drug in the note absent from source but close to a source drug."""
        claims = [self._claim_with_quote("hydralazine 25 mg",
                                         kind="medication", obj="hydralazine")]
        source = "Patient takes hydroxyzine 25 mg daily."
        signals = safety_signals(claims, source,
                                 drug_names=["hydroxyzine", "hydralazine"])
        types = {s["type"] for s in signals}
        self.assertIn("medication_near_miss", types)

    def test_unsupported_date_detected(self):
        claims = [self._claim_with_quote("Prescribed on March 14th")]
        source = "Started the new medication."
        signals = safety_signals(claims, source)
        types = {s["type"] for s in signals}
        self.assertIn("unsupported_date", types)

    def test_unsupported_laterality_detected(self):
        claims = [self._claim_with_quote("Pain in the left knee")]
        source = "Pain in the knee."
        signals = safety_signals(claims, source)
        types = {s["type"] for s in signals}
        self.assertIn("unsupported_laterality", types)

    def test_clean_note_has_no_safety_signals(self):
        claims = [self._claim_with_quote("sertraline 100 mg",
                                         kind="medication", obj="sertraline")]
        source = "Patient takes sertraline 100 mg daily."
        signals = safety_signals(claims, source,
                                 drug_names=["sertraline"])
        self.assertEqual(signals, [])


# ---------------------------------------------------------------------------
# End-to-end: oracle note on a real task
# ---------------------------------------------------------------------------

class EndToEndTest(unittest.TestCase):
    def test_oracle_passes_on_every_task(self):
        """The reference note for each task must score critical_wrong == 0."""
        task_dirs = sorted(glob.glob(os.path.join(ROOT, "tasks", "case_*")))
        self.assertGreater(len(task_dirs), 0, "no tasks found")
        for td in task_dirs:
            name = os.path.basename(td)
            with open(os.path.join(td, "tests", "factset.json")) as f:
                fs = json.load(f)
            with open(os.path.join(td, "solution", "note.md"),
                      encoding="utf-8") as f:
                note = f.read()
            claims = parse_template_claims(note)["claims"]
            report = evaluate(fs, claims)
            mne = {f["key"] for f in fs["facts"]
                   if (f.get("salience") or {}).get("must_not_err")}
            crit = sum(1 for r in report["per_claim"]
                       if r.get("verdict") == "wrong_fact"
                       and r.get("matched_fact_key") in mne)
            self.assertEqual(crit, 0, f"{name}: oracle has {crit} critical errors")

    def test_empty_note_does_not_pass(self):
        """An empty note must not pass (coverage 0, no supported claims)."""
        td = sorted(glob.glob(os.path.join(ROOT, "tasks", "case_*")))[0]
        with open(os.path.join(td, "tests", "factset.json")) as f:
            fs = json.load(f)
        report = evaluate(fs, [])
        self.assertEqual(report["counts"].get("supported", 0), 0)
        self.assertGreater(report["counts"].get("omitted_facts", 0), 0)


if __name__ == "__main__":
    unittest.main()
