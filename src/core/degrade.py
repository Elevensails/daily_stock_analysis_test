"""U3 修改策略 — 安全降级发布（safe degrade）。

repair loop 轮数耗尽仍 fail 时的保底交付通道：
  1. 按 ``violation_segments`` 剥离 **paragraph 级** 标红段；
  2. 在被剥离处插入占位标记 :data:`DEGRADED_PLACEHOLDER`（透明原则）；
  3. 对降级稿再跑完整 ``validate()``（同 config，不绕过任何 check）；
  4. 护栏触发 / 复检仍 fail → ``ok=False``（调用方回退 reject）。

护栏（杜绝「剥到只剩标题」的空壳降级）：
  * 无 segments / 无可定位 paragraph 级段 → 回退；
  * 存在 document 级违规段（``paragraph_index=None``，如 LLM judge
    整体低分）→ 无法定位剥离面，回退；
  * 剥离字符比例 > ``max_removed_ratio``（默认 50%）→ 回退。

本模块**仅依赖标准库 + src.core.validator**，可被 tests 零网络导入。
占位标记文案 :data:`DEGRADED_PLACEHOLDER` 是全链路唯一文案源（共享约定 #3）：
emit 端 ``_render_placeholders()`` 按此常量精确匹配转前端占位条 div。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.core.validator import JudgeConfig, ViolationSegment, split_paragraphs, validate

__all__ = [
    "DEGRADED_PLACEHOLDER",
    "DegradeResult",
    "assemble_degraded",
]

# 占位标记：markdown 层独占一段插入；唯一文案源，emit/前端按此精确匹配。
DEGRADED_PLACEHOLDER: str = "【本段因触发合规红线已安全降级移除】"


@dataclass
class DegradeResult:
    """一次安全降级组装的结果。"""

    ok: bool                      # 可安全降级发布（护栏未触发且复检通过）
    degraded_text: str            # 降级稿全文（ok=False 时为空串或中间稿，不应发布）
    removed_segments: list = field(default_factory=list)  # 实际剥离段（dict 形态子集）
    removed_ratio: float = 0.0    # 剥离字符比例（0..1）
    fallback_reason: str = ""     # ok=False 时的回退原因（人读）


def _seg_to_dict(seg: "ViolationSegment | dict") -> dict:
    """把 segment 归一化为 §3.1 契约 dict（兼容 dataclass 与 jsonl 回读 dict）。"""
    if isinstance(seg, ViolationSegment):
        return seg.to_dict()
    if isinstance(seg, dict):
        # 已是契约形态（含 location 嵌套）或扁平形态，统一补出 location
        if "location" in seg and isinstance(seg.get("location"), dict):
            return seg
        return {
            "check": seg.get("check", ""),
            "severity": seg.get("severity", "warn"),
            "reason": seg.get("reason", ""),
            "quote": seg.get("quote"),
            "location": {
                "granularity": seg.get("granularity", "paragraph"),
                "paragraph_index": seg.get("paragraph_index"),
                "line_start": seg.get("line_start"),
                "line_end": seg.get("line_end"),
                # P1.5 跨段配对字段透传（dataclass 路径经 to_dict() 已含）
                "related_paragraph_index": seg.get("related_paragraph_index"),
                "pairing": seg.get("pairing"),
            },
        }
    return {
        "check": "", "severity": "warn", "reason": "", "quote": None,
        "location": {"granularity": "document", "paragraph_index": None,
                     "line_start": None, "line_end": None},
    }


def _seg_location(seg_dict: dict) -> "tuple[str, int | None]":
    """从契约 dict 中取 (granularity, paragraph_index)。"""
    loc = seg_dict.get("location") or {}
    return (
        str(loc.get("granularity") or "document"),
        loc.get("paragraph_index"),
    )


def assemble_degraded(
    text: str,
    violation_segments: list,
    *,
    source_facts: Optional[dict] = None,
    report_kind: str = "stock",
    config: Optional[JudgeConfig] = None,
    max_removed_ratio: float = 0.5,
) -> DegradeResult:
    """按违规段剥离整段 → 插占位标记 → 复检，产出 :class:`DegradeResult`。

    参数
    ----
    text: 降级底稿（通常为 repair loop 最后一轮重写稿）
    violation_segments: 最新一轮 validate 的结构化违规段
        （:class:`ViolationSegment` 或契约 dict 均可，仅剥离 paragraph 级）
    source_facts: 行情源侧车（复检时沿用，保证完整 gate 不绕过）
    report_kind: stock | market | vibe（复检与日志可读性）
    config: 与首轮相同的 :class:`JudgeConfig`（复检不绕过）
    max_removed_ratio: 剥离字符比例护栏上限（默认 0.5）
    """
    seg_dicts = [_seg_to_dict(s) for s in (violation_segments or [])]

    # 护栏 1：无 segments → 无剥离面，回退
    if not seg_dicts:
        return DegradeResult(
            ok=False, degraded_text="", removed_segments=[],
            removed_ratio=0.0, fallback_reason="no_violation_segments",
        )

    # 护栏 2：存在 document 级违规（不可定位剥离面）→ 回退
    for sd in seg_dicts:
        granularity, para_idx = _seg_location(sd)
        if granularity != "paragraph" or para_idx is None:
            return DegradeResult(
                ok=False, degraded_text="", removed_segments=[],
                removed_ratio=0.0,
                fallback_reason="document_level_violation_not_removable",
            )

    paragraphs = split_paragraphs(text)
    total_chars = sum(len(p.text) for p in paragraphs)
    if not paragraphs or total_chars == 0:
        return DegradeResult(
            ok=False, degraded_text="", removed_segments=[],
            removed_ratio=0.0, fallback_reason="empty_text",
        )

    # 收集在段落范围内的剥离目标
    remove_indices: set[int] = set()
    removed_segments: list[dict] = []
    for sd in seg_dicts:
        _, para_idx = _seg_location(sd)
        if isinstance(para_idx, int) and 0 <= para_idx < len(paragraphs):
            if para_idx not in remove_indices:
                remove_indices.add(para_idx)
            removed_segments.append(sd)

    # 护栏 3：所有段指针均越界（文本已变、定位失效）→ 回退
    if not remove_indices:
        return DegradeResult(
            ok=False, degraded_text="", removed_segments=[],
            removed_ratio=0.0, fallback_reason="no_locatable_segments",
        )

    # 护栏 4：剥离比例超上限 → 回退（防空壳降级）
    removed_chars = sum(len(paragraphs[i].text) for i in remove_indices)
    removed_ratio = removed_chars / total_chars
    if removed_ratio > max_removed_ratio:
        return DegradeResult(
            ok=False, degraded_text="", removed_segments=removed_segments,
            removed_ratio=round(removed_ratio, 3),
            fallback_reason=(
                f"removed_ratio {removed_ratio:.2f} exceeds "
                f"max_removed_ratio {max_removed_ratio:.2f}"
            ),
        )

    # 组装降级稿：被剥离段整段替换为占位标记（独占一段）
    out_blocks: list[str] = []
    for p in paragraphs:
        if p.index in remove_indices:
            out_blocks.append(DEGRADED_PLACEHOLDER)
        else:
            out_blocks.append(p.text)
    degraded_text = "\n\n".join(out_blocks)

    # 对降级稿再跑完整 validate（同 config / source_facts，不绕过）
    recheck = validate(
        degraded_text,
        source_facts=source_facts,
        report_kind=report_kind,
        config=config,
    )
    if not recheck.passed:
        return DegradeResult(
            ok=False, degraded_text=degraded_text,
            removed_segments=removed_segments,
            removed_ratio=round(removed_ratio, 3),
            fallback_reason="revalidate_failed: " + "; ".join(recheck.reasons),
        )

    return DegradeResult(
        ok=True, degraded_text=degraded_text,
        removed_segments=removed_segments,
        removed_ratio=round(removed_ratio, 3),
        fallback_reason="",
    )
