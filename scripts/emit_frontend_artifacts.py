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
from src.core.validator import gate_report, JudgeConfig, append_reject_record  # noqa: E402
from src.core.degrade import DEGRADED_PLACEHOLDER  # noqa: E402  # 唯一文案源（共享约定 #3）
from src.config import get_config  # noqa: E402

import html as _html_mod  # noqa: E402  # 占位标记精确匹配用（md2html 已 escape 文本节点）

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
# market_review 的 HHMM 时段段可选：大盘复盘是日报（run_market_review 存盘名为
# market_review_YYYYMMDD.md，无时段段），需与带时段段的 market_review_HHMM_YYYYMMDD.md
# 同时兼容；无时段段的日报会被映射到全部 5 个时段（见 _resolve_slot_targets）。
TYPE_PATTERNS = {
    "report": re.compile(r"^report_(\d{4})_(\d{8})\.md$"),
    "market_review": re.compile(r"^market_review_(?:(\d{4})_)?(\d{8})\.md$"),
    "vibe": re.compile(r"^vibe_(\d{4})_(\d{8})\.md$"),
}

# ---- U3 事后校验 gate 配置（U16：统一收敛到 config 层，env>yaml>code 三级优先级）----
_cfg = get_config()
_JUDGE_CONFIG = JudgeConfig(
    enabled=_cfg.judge_enabled,
    use_llm=_cfg.judge_use_llm,
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



def _save_repaired(bn: str, text: str) -> None:
    """修复成功发布的报告另存 ``logs/repaired/<bn>.md`` 备查（不放 reports/）。

    置于 ``logs/repaired/`` 而非 ``reports/``，避免下一轮 emit 把它当成新报告重新处理。
    """
    try:
        out_dir = os.path.join(_ROOT, "logs", "repaired")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, bn), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass  # 落盘失败不影响发布主流程


def _save_degraded(bn: str, text: str) -> None:
    """降级发布的报告另存 ``logs/degraded/<bn>.md`` 备查（不放 reports/）。

    与 ``_save_repaired`` 同理：置于 logs/ 下避免下一轮 emit 重新处理；
    单独目录便于审计「哪些报告是降级发布的」。
    """
    try:
        out_dir = os.path.join(_ROOT, "logs", "degraded")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, bn), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass  # 落盘失败不影响发布主流程


# 降级顶部横幅（P0-6：透明原则，读者一眼可知本报告经过降级处理）。
_DEGRADED_BANNER_HTML = (
    '<div class="dsa-degraded-banner">'
    "⚠️ 本报告部分段落因触发合规红线已被安全降级移除，其余内容不受影响。"
    "</div>"
)


def _inject_degraded_banner(frag_html: str) -> str:
    """在片段 HTML 顶部注入降级横幅（纯字符串前置，不解析 DOM）。"""
    return _DEGRADED_BANNER_HTML + "\n" + frag_html


def _render_placeholders(frag_html: str) -> str:
    """把 md2html 输出中的占位标记文本替换为前端占位条 div。

    ``DEGRADED_PLACEHOLDER`` 为唯一文案源；md2html 对文本节点做过
    ``html.escape``，故按 escape 后的形态精确匹配。优先整段替换
    ``<p>…</p>`` 包裹形态（保持块级结构合法），否则替换裸文本。
    """
    escaped = _html_mod.escape(DEGRADED_PLACEHOLDER)
    placeholder_div = (
        '<div class="dsa-degraded-placeholder">' + escaped + "</div>"
    )
    wrapped = f"<p>{escaped}</p>"
    if wrapped in frag_html:
        return frag_html.replace(wrapped, placeholder_div)
    return frag_html.replace(escaped, placeholder_div)


def _assign_fragment_meta(
    slots: "dict[str, dict]",
    slot_targets: "list[str]",
    frag_type: str,
    meta: dict,
    is_daily_fallback: bool = False,
) -> None:
    """把片段元信息（如 degraded 标记）写入目标时段的 ``fragmentMeta``。

    加法字段：仅降级片段写入，正常片段不写（前端 optional chaining 读取，
    旧 manifest / 无 meta 时渲染行为与现状逐像素一致）。
    覆盖规则与 :func:`_assign_fragment` 对齐（daily fallback 不覆盖已填时段）。
    """
    for slot in slot_targets:
        if is_daily_fallback and slots[slot]["fragments"][frag_type] is not None:
            continue
        slots[slot].setdefault("fragmentMeta", {})[frag_type] = dict(meta)


def _resolve_llm_judge():
    """P1-2 预留：解析真实 LLM judge callable。

    本轮不强制激活真实 DeepSeek judge，返回 ``None``；后续可在 ``JUDGE_USE_LLM=1``
    时在此注入真实 judge（签名 ``(text, *, report_kind, source_facts) -> dict``），
    供 repair 重写作为可选修正信号源。
    """
    return None


def _resolve_slot_targets(hmm: "str | None") -> "list[str]":
    """把文件名中的时段段解析为要写入的 slot 列表。

    * ``hmm`` 存在（如 ``"0930"``）→ 按既有逻辑归入最近的单一时段；
    * ``hmm`` 为 ``None``（无时段段的日报，如 ``market_review_YYYYMMDD.md``）→
      映射到全部 5 个时段，使每个时段卡片都能展示这份日报。
    """
    if hmm is None:
        return list(SLOTS.keys())
    return [nearest_slot(hmm)]


def _init_slots() -> "dict[str, dict]":
    """预填充 5 个时段骨架，缺失片段显式置 null（前端据此渲染「待生成」）。"""
    return {
        code: {"label": lbl, "name": nm, "fragments": {t: None for t in FRAGMENT_TYPES}}
        for code, (lbl, nm) in SLOTS.items()
    }


def _assign_fragment(
    slots: "dict[str, dict]",
    slot_targets: "list[str]",
    frag_type: str,
    fname: str,
    is_daily_fallback: bool = False,
) -> None:
    """把片段文件名写入目标时段的 manifest 骨架。

    ``is_daily_fallback=True``（无时段段的大盘日报映射到全部时段）时不覆盖
    已由带时段段报告填充的时段，保证既有分时段命名的优先级不变。
    """
    for slot in slot_targets:
        if is_daily_fallback and slots[slot]["fragments"][frag_type] is not None:
            continue
        slots[slot]["fragments"][frag_type] = fname


def main() -> int:
    cfg = get_config()
    reports_dir = os.environ.get("REPORTS_DIR") or cfg.reports_dir
    if not os.path.isabs(reports_dir):
        reports_dir = os.path.join(_ROOT, reports_dir)
    model = os.environ.get("LITELLM_MODEL", cfg.default_litellm_model)
    # U3 repair loop 配置（U16：统一收敛到 config 层，env>yaml>code 三级优先级）
    repair_max = cfg.repair_max_rounds  # 已是 env>yaml>code 且 clamp 到 [0,3]
    repair_model = os.environ.get("REPAIR_MODEL") or cfg.repair_model or cfg.default_litellm_model
    repair_temp = cfg.repair_temperature
    os.makedirs(_FRAG_DIR, exist_ok=True)

    # lazy import：repair 核心 + analyzer 重写封装（避免顶层强拉 analyzer 巨型模块）
    repair_fn = None
    rewrite_fn = None
    try:
        from src.core.repair import repair_report
        from src.analyzer import call_rewrite_llm

        repair_fn = repair_report
        rewrite_fn = call_rewrite_llm
    except Exception as exc:  # repair 不可用 → 回退原 reject 行为
        print(
            f"  WARN: repair loop unavailable ({exc}); "
            "failing reports will be rejected as before"
        )

    slots = _init_slots()
    md_files = sorted(glob.glob(os.path.join(reports_dir, "*.md")))
    emitted = 0
    rejected = 0

    log_path = os.path.join(_ROOT, "logs", "judge_rejects.jsonl")

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

        hmm = m.group(1)  # market_review 日报（无时段段）时为 None
        date = m.group(2)
        slot_targets = _resolve_slot_targets(hmm)
        # 片段文件名与源 md 同 stem：有时段段 → <type>_<HHMM>_<date>.html；
        # 无时段段（大盘日报）→ <type>_<date>.html（归档页按类型前缀解析，兼容）。
        stem = f"{matched_type}_{hmm}_{date}" if hmm else f"{matched_type}_{date}"

        try:
            with open(f, "r", encoding="utf-8") as fh:
                md = fh.read()
        except OSError as exc:
            print(f"  skip (read error): {bn}: {exc}")
            continue

        # ---- U3 事后校验 gate：首轮仅校验、不写 jsonl（log=False）----
        source_facts = _load_source_facts(f)
        jres = gate_report(
            md,
            source_facts=source_facts,
            report_kind=matched_type,
            config=_JUDGE_CONFIG,
            context={"file": bn},
            log=False,
        )

        if jres.passed:
            # 首轮通过：照旧 emit，不进 repair loop（repair_rounds=0）
            frag = md2html(md)  # XSS 安全：文本节点均已 html.escape
            fname = f"{stem}.html"
            with open(os.path.join(_FRAG_DIR, fname), "w", encoding="utf-8") as fh:
                fh.write(frag)
            _assign_fragment(
                slots, slot_targets, matched_type, fname, is_daily_fallback=hmm is None
            )
            emitted += 1
            print(f"  emitted {fname}")
            continue

        # ---- 首轮 fail：进入 repair loop（若 repair 可用）----
        if repair_fn is not None and rewrite_fn is not None:
            # P1-2：LLM judge 注入通道（本轮不强制激活真实 judge，默认值 None）
            llm_judge = _resolve_llm_judge() if _JUDGE_CONFIG.use_llm else None
            rres = repair_fn(
                md,
                reasons=jres.reasons,
                violation_segments=jres.violation_segments,
                source_facts=source_facts,
                report_kind=matched_type,
                model=repair_model,
                max_rounds=repair_max,
                llm_call=rewrite_fn,
                config=_JUDGE_CONFIG,
                llm_judge=llm_judge,
                temperature=repair_temp,
                safe_degrade_enabled=cfg.safe_degrade_enabled,
            )
            if rres.final_action == "emit":
                # 修复成功：落盘 logs/repaired/<bn>.md 备查（不放 reports/）
                _save_repaired(bn, rres.final_text)
                frag = md2html(rres.final_text)
                fname = f"{stem}.html"
                with open(os.path.join(_FRAG_DIR, fname), "w", encoding="utf-8") as fh:
                    fh.write(frag)
                _assign_fragment(
                    slots, slot_targets, matched_type, fname, is_daily_fallback=hmm is None
                )
                emitted += 1
                print(f"  emitted (repaired, rounds={rres.rounds}) {fname}")
                append_reject_record(
                    log_path,
                    matched_type,
                    jres,
                    context={"file": bn},
                    final_action="emit",
                    repair_rounds=rres.rounds,
                    rewritten=rres.rewritten,
                    repair_reasons=rres.repair_reasons,
                    last_reasons=rres.last_reasons,
                )
                continue
            if rres.final_action == "emit_degraded":
                # P0-5：安全降级发布——降级稿已通过完整 gate 复检，可发布。
                # 顶部横幅 + 占位条（P0-6 透明原则），manifest 写 fragmentMeta。
                _save_degraded(bn, rres.final_text)
                frag = md2html(rres.final_text)  # XSS 安全路径不变
                frag = _render_placeholders(frag)
                frag = _inject_degraded_banner(frag)
                fname = f"{stem}.html"
                with open(os.path.join(_FRAG_DIR, fname), "w", encoding="utf-8") as fh:
                    fh.write(frag)
                # meta 须在 fragment 赋值之前写入（daily fallback 覆盖规则对齐）
                _assign_fragment_meta(
                    slots, slot_targets, matched_type,
                    {
                        "degraded": True,
                        "removedSegments": len(rres.degraded_segments),
                    },
                    is_daily_fallback=hmm is None,
                )
                _assign_fragment(
                    slots, slot_targets, matched_type, fname, is_daily_fallback=hmm is None
                )
                emitted += 1
                print(
                    f"  emitted (DEGRADED, rounds={rres.rounds}, "
                    f"removed={len(rres.degraded_segments)}) {fname}"
                )
                append_reject_record(
                    log_path,
                    matched_type,
                    jres,
                    context={"file": bn},
                    final_action="emit_degraded",
                    repair_rounds=rres.rounds,
                    rewritten=rres.rewritten,
                    repair_reasons=rres.repair_reasons,
                    last_reasons=rres.last_reasons,
                    degraded_segments=rres.degraded_segments,
                )
                continue
            # repair 超限且不可降级：回退 reject（写 final_action=reject 终态记录）
            rejected += 1
            print(
                f"  JUDGE REJECT (after repair, rounds={rres.rounds}) {bn}: "
                + "; ".join(jres.reasons)
            )
            append_reject_record(
                log_path,
                matched_type,
                jres,
                context={"file": bn},
                final_action="reject",
                repair_rounds=rres.rounds,
                rewritten=rres.rewritten,
                repair_reasons=rres.repair_reasons,
                last_reasons=rres.last_reasons,
            )
            continue

        # ---- repair 不可用：回退原 reject 行为（仅写一条 reject 记录）----
        rejected += 1
        print(
            f"  JUDGE REJECT {bn} (score={jres.score:.2f}): "
            + "; ".join(jres.reasons)
        )
        append_reject_record(
            log_path,
            matched_type,
            jres,
            context={"file": bn},
            final_action="reject",
            repair_rounds=0,
            rewritten=False,
            repair_reasons=jres.reasons,
        )
        continue

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
