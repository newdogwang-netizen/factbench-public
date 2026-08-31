# Security Policy

## Reporting a Vulnerability

If you discover a security issue in FactBench, please **do not** open a
public issue. Instead, report it privately:

- GitHub: use the "Report a vulnerability" option under
  **Security → Security advisories** on the repository.
- Email: security@heidi.health

Include a description of the issue, steps to reproduce, and any relevant
patches. We will acknowledge within 72 hours and aim to publish a fix
within 30 days.

## Scope

FactBench is a **benchmark tool**, not a medical device or clinical
decision-support system. Security issues in the scoring engine, task
infrastructure, or scoring service fall within scope. Misclassification
of clinical facts by the deterministic parser is a **benchmark-quality
issue** (report via standard GitHub issues), not a security vulnerability.

## Data

All tasks in this repository are **fully synthetic**. No real patient
data is present. If you believe any task contains real patient data,
report it immediately — it will be removed within 24 hours of
confirmation.
