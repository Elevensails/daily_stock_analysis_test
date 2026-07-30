#!/usr/bin/env python3
"""Rename report files with time-slot labels for deploy_pages.py.
Accepts TIME_SLOT env var for manual triggers (e.g. TIME_SLOT=0930)."""
import os, glob, shutil
from datetime import datetime, timezone, timedelta

# U16：允许直接以脚本方式运行（scripts/ 下）时也能 import src 配置层。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import get_config

now = datetime.now(timezone(timedelta(hours=8)))
tslot = os.environ.get("TIME_SLOT") or get_config().time_slot_default
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
