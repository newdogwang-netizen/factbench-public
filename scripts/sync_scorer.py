#!/usr/bin/env python3
"""Propagate the top-level scorer/ into every task's tests/scorer/ snapshot.

Run after any scorer change, then `make check && make mutation-check`.
"""
import glob
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "scorer")

for td in sorted(glob.glob(os.path.join(ROOT, "tasks", "*"))):
    dst = os.path.join(td, "tests", "scorer")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("synced", os.path.basename(td))
