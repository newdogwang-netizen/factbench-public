#!/bin/bash
# Deterministic verifier — no network, no LLM. Writes /logs/verifier/reward.txt.
mkdir -p /logs/verifier
python3 /tests/verify.py
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
