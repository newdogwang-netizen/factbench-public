# Write a factually consistent clinical note

`/app/transcript.txt` is the transcript of a **fictional** doctor-patient
consultation. `/app/template.txt` is a note template.

Write a clinical note and save it as **`note.md`** in the working directory:

- Use ONLY facts explicitly stated in the transcript.
- Follow the template sections where relevant; omit unsupported sections.
- One atomic fact per bullet where possible.

Scoring is deterministic (rule-based clinical parser + tiered fact matcher):
coverage of must-cover facts; zero critical errors (wrong dose / frequency /
negation / medication status); fabricated out-of-source dosed medications
are flagged. Pass rule: critical_wrong == 0 and coverage >= a per-task
oracle-calibrated minimum (see tests/verify.py MIN_COVERAGE).
