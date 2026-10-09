#!/usr/bin/env python3
"""First-time review-history scan: every project's MRs of the past three months
into sentinel.db, then follow-ups, AI classification, git blame and scores.

    python3 scan_history.py                # all projects, scoring window (90 days)
    python3 scan_history.py --days 120
    python3 scan_history.py --project developer/py_backend/pocketsso
    python3 scan_history.py --dry-run      # count only, write nothing

Same as `python3 -m history scan …` (see history/scan.py). The scheduled
`python3 -m history run` does it by itself on a db that was never scanned.
"""
import sys

from history.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["scan", *sys.argv[1:]]))
