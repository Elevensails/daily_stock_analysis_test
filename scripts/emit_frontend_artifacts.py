#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emit the frontend content contract from reports/*.md (U2 方案C).

Pure data-emission bridge between the analysis pipeline and the Vite frontend:

    reports/<type>_<HHMM>_<YYYYMMDD>.md
        --(md2html, XSS-safe)-->  web/src/content/fragments/<type>_<HHMM>_<YYYYMMDD>.html
        --(manifest)---------->  web/src/content/manifest.json

Design rules (see U2-architecture.md §2):
  * Reuses ``deploy_pages.md2html`` — the markdown→HTML mapping is NOT reimplemented
    and the XSS escaping behavior is unchanged.
  * Only emits data (manifest + fragments); it never assembles full pages and never
    touches analysis logic / conclusions.
  * The content contract is a **data boundary**: adding a new content module = emit
    one more fragment + append its type to ``fragmentTypes``; the core pipeline
    (emit + Vite injection + deploy) needs no change.
"""
from __future__ import annotations

import os
import re
import sys
import json
import glob
from datetime import datetime, timezone, timedelta

from deploy_pages import md2html, nearest_slot

# 让 scripts/ 下的脚本能 import src.core.validator（repo root 注入 sys.path）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.core.validator import gate_report, JudgeConfig  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CONTENT_DIR = os.path.join(_ROOT, "web", "src", "content")
_FRAG_DIR = os.path.join(_CONTENT_DIR, "fragments")

# 5 个固定时段（与架构/旧 deploy 保持一致）。
SLOTS: "dict[str, tuple[str, str]]" = {
    "0900": ("09:00", "早盘分析"),
    "0930": ("09:30", "开盘追踪"),
    "1200": ("12:00", "午间复盘"),
    "1430": ("14:30", "午盘追踪"),
    "1800": ("18:00", "收盘复盘"),
}

FRAGMENT_TYPES = ["report", "market_review", "vibe"]

# 文件命名 → 类型（沿用既有命名口径，不引入 vibe_report_ 等新前缀）。
TYPE_PATTERNS = {
    "report": re.compile(r"^report_(\d{4})_(\d{8})\.md$"),
    "market_review": re.compile(r"^market_review_(\d{4})_(\d{8})\.md$"),
    "vibe": re.compile(r"^vibe_(\d{4})_(\d{8})\.md$"),
}

# ---- U3 事后校验 gate 配置（环境变量可覆盖，默认开启、不启用 LLM）----
_JUDGE_CONFIG = JudgeConfig(
    enabled=os.environ.get("JUDGE_ENABLED", "1") != "0",
    use_llm=os.environ.get("JUDGE_USE_LLM", "0") == "1",
)


def _load_source_facts(md_path: str) -> "dict | None":
    """加载可选的数字源核对侧车文件 ``<stem>.facts.json``。

    由分析阶段写入（含 is_limit_up / is_limit_down / price 等），存在则
    启用「关键数字与行情源核对」；缺失则跳过该项（启发式检查照常）。
    """
    sidecar = md_path[: md_path.rfind(".")] + ".facts.json"
    if not os.path.exists(sidecar):
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None



def _init_slots() -> "dict[str, dict]":
    """预填充 5 个时段骨架，缺失片段显式置 null（前端据此渲染「待生成」）。"""
    return {
        code: {"label": lbl, "name": nm, "fragments": {t: None for t in FRAGMENT_TYPES}}
        for code, (lbl, nm) in SLOTS.items()
    }


def main() -> int:
    reports_dir = os.environ.get("REPORTS_DIR", os.path.join(_ROOT, "reports"))
    model = os.environ.get("LITELLM_MODEL", "deepseek/deepseek-v4-flash")
    os.makedirs(_FRAG_DIR, exist_ok=True)

    slots = _init_slots()
    md_files = sorted(glob.glob(os.path.join(reports_dir, "*.md")))
    emitted = 0
    rejected = 0

    for f in md_files:
        bn = os.path.basename(f)
        matched_type = None
        m = None
        for t, pat in TYPE_PATTERNS.items():
            mm = pat.match(bn)
            if mm:
                matched_type, m = t, mm
                break
        if not matched_type:
            continue  # 非报告类 md（如配置文件），跳过

        hmm = m.group(1)
        date = m.group(2)
        slot = nearest_slot(hmm)

        try:
            with open(f, "r", encoding="utf-8") as fh:
                md = fh.read()
        except OSError as exc:
            print(f"  skip (read error): {bn}: {exc}")
            continue

        # ---- U3 事后校验 gate：fail 不发布到 GH Pages ----
        source_facts = _load_source_facts(f)
        jres = gate_report(
            md,
            source_facts=source_facts,
            report_kind=matched_type,
            config=_JUDGE_CONFIG,
            context={"file": bn},
        )
        if not jres.passed:
            rejected += 1
            print(
                f"  JUDGE REJECT {bn} (score={jres.score:.2f}): "
                + "; ".join(jres.reasons)
            )
            continue  # 跳过往前端 emit → 不发布

        frag = md2html(md)  # XSS 安全：文本节点均已 html.escape
        fname = f"{matched_type}_{hmm}_{date}.html"
        with open(os.path.join(_FRAG_DIR, fname), "w", encoding="utf-8") as fh:
            fh.write(frag)
        slots[slot]["fragments"][matched_type] = fname
        emitted += 1
        print(f"  emitted {fname}")

    manifest = {
        "schemaVersion": "1.0",
        "generatedAt": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "model": model,
        "site": {
            "title": "A股智能分析 · 决策仪表盘",
            "base": "/daily_stock_analysis_test/",
        },
        "stats": {
            "slotsPerDay": 5,
            "holdings": 4,
            "reportTypes": len(FRAGMENT_TYPES),
            "monthlyCost": "~0",
        },
        "slots": slots,
        "fragmentTypes": FRAGMENT_TYPES,
    }
    with open(os.path.join(_CONTENT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"wrote manifest.json ({emitted} fragments)")
    if rejected:
        print(
            f"JUDGE_REJECTS={rejected}  (已拦截不发布，原因见 logs/judge_rejects.jsonl)"
        )
    print("emit done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
