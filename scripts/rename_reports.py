#!/usr/bin/env python3
"""Rename report files with time-slot labels for deploy_pages.py.
Accepts TIME_SLOT env var for manual triggers (e.g. TIME_SLOT=0930)."""
import os, glob, shutil
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone(timedelta(hours=8)))
tslot = os.environ.get("TIME_SLOT", now.strftime("%H%M"))
today = now.strftime("%Y%m%d")

print(f"Renaming with tslot={tslot}, date={today}")

for pattern in ["reports/report_*.md", "reports/market_review_*.md"]:
    for f in glob.glob(pattern):
        if f"_{tslot}_" in f:
            continue
        prefix = "report" if "/report_" in f else "market_review"
        dst = f"reports/{prefix}_{tslot}_{today}.md"
        shutil.copy(f, dst)
        print(f"renamed: {os.path.basename(f)} -> {os.path.basename(dst)}")

print("rename done")
