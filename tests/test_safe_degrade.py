# -*- coding: utf-8 -*-
"""U3 修改策略 — 安全降级发布（P0-4 / P0-5 / P0-7）单元测试。

覆盖：
  - 违规段定位层（P0-1）：validator 产出结构化 violation_segments 契约
  - assemble_degraded：正常剥离 + 占位标记 + 复检通过
  - 护栏：无 segments / document 级 / 段指针越界 / 剥离比例超限 → 回退
  - 复检仍 fail → ok=False（调用方回退 reject）
  - repair loop 集成：轮耗尽 → emit_degraded；开关关 → 与旧行为一致 reject
  - final_action 三终态枚举唯一定义点（共享约定 #1）

全部离线、零网络、零新增第三方依赖。
"""
from __future__ import annotations

from src.core.degrade import (
    DEGRADED_PLACEHOLDER,
    DegradeResult,
    assemble_degraded,
)
from src.core.repair import (
    FINAL_EMIT,
    FINAL_EMIT_DEGRADED,
    FINAL_REJECT,
    repair_report,
)
from src.core.validator import (
    ViolationSegment,
    split_paragraphs,
    validate,
)


# ---- 样本 ----
# 多段文本：仅第 2 段（0 起始段号）踩红线，其余段干净且体量足够（剥离比 < 50%）
MULTI_BAD = (
    "# 600036 招商银行 复盘\n\n"
    "今日收盘报 38.12 元，微跌 0.34%，成交额 18.6 亿，量能温和。\n\n"
    "建议现价买入，目标价 15.80 元，稳赚不赔。\n\n"
    "北向资金小幅净流出，主力资金观望情绪浓，短期以观望为主。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)

# 单段红线文本（剥离比 100% → 护栏 4 触发）
SINGLE_BAD = "建议现价买入，目标价 15.80 元，保收益无风险，稳赚不赔。"


def _redline_segments(text: str) -> "list[ViolationSegment]":
    """从 validate 结果取结构化违规段（真实定位层产物，非手工造）。"""
    res = validate(text, report_kind="report")
    assert res.passed is False
    return list(res.violation_segments)


# ---------------------------------------------------------------------------
# 1. 定位层契约（P0-1）：violation_segments schema §3.1
# ---------------------------------------------------------------------------
class TestViolationSegments:
    def test_fail_result_carries_segments(self):
        segs = _redline_segments(MULTI_BAD)
        assert segs, "fail 结果必须携带 violation_segments"
        for s in segs:
            assert isinstance(s, ViolationSegment)

    def test_redline_located_to_correct_paragraph(self):
        segs = _redline_segments(MULTI_BAD)
        red = [s for s in segs if s.check == "red_line"]
        assert red, "红线违规必须产出 segment"
        assert all(s.granularity == "paragraph" for s in red)
        assert all(s.paragraph_index == 2 for s in red), "红线应定位到第 2 段（0 起始）"

    def test_to_dict_matches_contract_schema(self):
        seg = _redline_segments(MULTI_BAD)[0]
        d = seg.to_dict()
        assert set(d.keys()) == {"check", "severity", "reason", "quote", "location"}
        # P1.5 契约扩展：location 加法携带 related_paragraph_index / pairing
        assert set(d["location"].keys()) == {
            "granularity", "paragraph_index", "line_start", "line_end",
            "related_paragraph_index", "pairing",
        }

    def test_passed_result_has_empty_segments(self):
        ok = validate(
            "# 复盘\n\n今日收盘微跌 0.34%，短期观望。\n\n> 不构成投资建议。",
            report_kind="stock",
        )
        assert ok.passed is True
        assert ok.violation_segments == []

    def test_split_paragraphs_zero_based_index(self):
        paras = split_paragraphs(MULTI_BAD)
        assert [p.index for p in paras] == list(range(len(paras)))
        assert paras[0].line_start == 1  # 行号 1-based


# ---------------------------------------------------------------------------
# 2. assemble_degraded：正常剥离路径
# ---------------------------------------------------------------------------
class TestAssembleDegraded:
    def test_strip_redline_paragraph_and_insert_placeholder(self):
        segs = _redline_segments(MULTI_BAD)
        dres = assemble_degraded(MULTI_BAD, segs, report_kind="report")
        assert isinstance(dres, DegradeResult)
        assert dres.ok is True
        # 违规段被整段剥离
        assert "建议现价买入" not in dres.degraded_text
        assert "稳赚不赔" not in dres.degraded_text
        # 占位标记原位插入（唯一文案源）
        assert DEGRADED_PLACEHOLDER in dres.degraded_text
        # 未标红段逐字保留
        assert "今日收盘报 38.12 元" in dres.degraded_text
        assert "北向资金小幅净流出" in dres.degraded_text
        # 剥离比例在护栏内
        assert 0.0 < dres.removed_ratio <= 0.5
        assert dres.removed_segments

    def test_degraded_text_passes_full_revalidate(self):
        segs = _redline_segments(MULTI_BAD)
        dres = assemble_degraded(MULTI_BAD, segs, report_kind="report")
        assert dres.ok is True
        # 复检独立验证：降级稿必须过完整 gate
        recheck = validate(dres.degraded_text, report_kind="report")
        assert recheck.passed is True

    def test_accepts_contract_dict_segments(self):
        """兼容 jsonl 回读的契约 dict 形态（非 dataclass）。"""
        segs = [s.to_dict() for s in _redline_segments(MULTI_BAD)]
        dres = assemble_degraded(MULTI_BAD, segs, report_kind="report")
        assert dres.ok is True
        assert DEGRADED_PLACEHOLDER in dres.degraded_text


# ---------------------------------------------------------------------------
# 3. 护栏：四类回退路径全覆盖
# ---------------------------------------------------------------------------
class TestDegradeGuardrails:
    def test_no_segments_falls_back(self):
        dres = assemble_degraded(MULTI_BAD, [], report_kind="report")
        assert dres.ok is False
        assert dres.fallback_reason == "no_violation_segments"

    def test_document_level_violation_falls_back(self):
        doc_seg = ViolationSegment(
            check="llm_judge", severity="critical",
            reason="整体质量低分", quote=None,
            granularity="document", paragraph_index=None,
        )
        dres = assemble_degraded(MULTI_BAD, [doc_seg], report_kind="report")
        assert dres.ok is False
        assert dres.fallback_reason == "document_level_violation_not_removable"

    def test_out_of_range_pointer_falls_back(self):
        seg = ViolationSegment(
            check="red_line", severity="critical", reason="x",
            granularity="paragraph", paragraph_index=99,
        )
        dres = assemble_degraded(MULTI_BAD, [seg], report_kind="report")
        assert dres.ok is False
        assert dres.fallback_reason == "no_locatable_segments"

    def test_removed_ratio_over_limit_falls_back(self):
        """单段全违规 → 剥离比 100% > 50% → 防空壳护栏触发。"""
        segs = _redline_segments(SINGLE_BAD)
        assert segs and all(
            s.granularity == "paragraph" for s in segs
        )
        dres = assemble_degraded(SINGLE_BAD, segs, report_kind="report")
        assert dres.ok is False
        assert "removed_ratio" in dres.fallback_reason

    def test_revalidate_failure_falls_back(self):
        """剥离后仍有残余违规（segment 只指向其一）→ 复检 fail → 回退。"""
        two_bad = (
            "# 复盘\n\n"
            "第一部分行情平稳，量能温和，观望为主，资金情绪中性偏暖。\n\n"
            "建议现价买入，目标价 15.80 元。\n\n"
            "该股今日涨停，但收跌 -5.0%，走势自相矛盾持续全天。\n\n"
            "尾盘缩量整理，短期继续跟踪，不构成投资建议。"
        )
        only_redline = ViolationSegment(
            check="red_line", severity="critical",
            reason="含具体买入建议与价位", quote="建议现价买入",
            granularity="paragraph", paragraph_index=2,
        )
        dres = assemble_degraded(two_bad, [only_redline], report_kind="report")
        assert dres.ok is False
        assert dres.fallback_reason.startswith("revalidate_failed")


# ---------------------------------------------------------------------------
# 4. repair loop 集成：emit_degraded 终态 + P0-7 开关
# ---------------------------------------------------------------------------
class TestRepairLoopDegradeIntegration:
    def test_exhausted_rounds_emit_degraded_when_enabled(self):
        """轮耗尽仍 fail + 可定位段 → emit_degraded（P0-5 主路径）。"""
        def stubborn_llm(system, user):
            return MULTI_BAD  # 模型始终不修

        res = repair_report(
            MULTI_BAD,
            reasons=["[红线] 含具体买入建议与价位"],
            report_kind="report",
            max_rounds=2,
            llm_call=stubborn_llm,
            safe_degrade_enabled=True,
        )
        assert res.passed is True
        assert res.final_action == FINAL_EMIT_DEGRADED
        assert res.rounds == 2
        assert DEGRADED_PLACEHOLDER in res.final_text
        assert "建议现价买入" not in res.final_text
        assert res.degraded_segments, "终态须携带剥离段清单（jsonl 审计）"
        # 降级稿必须过完整 gate（永不 emit 不合格版）
        assert validate(res.final_text, report_kind="report").passed is True

    def test_switch_off_keeps_legacy_reject(self):
        """P0-7：safe_degrade_enabled=False 时行为与旧版逐字段一致。"""
        def stubborn_llm(system, user):
            return MULTI_BAD

        res = repair_report(
            MULTI_BAD,
            reasons=["[红线] 含具体买入建议与价位"],
            report_kind="report",
            max_rounds=2,
            llm_call=stubborn_llm,
            safe_degrade_enabled=False,
        )
        assert res.passed is False
        assert res.final_action == FINAL_REJECT
        assert res.rounds == 2
        assert res.final_text == MULTI_BAD  # 原文回传，永不 emit 不合格版
        assert res.degraded_segments == []

    def test_undegradable_sample_falls_back_reject(self):
        """开关开但护栏触发（单段全违规）→ 仍回退 reject。"""
        def stubborn_llm(system, user):
            return SINGLE_BAD

        res = repair_report(
            SINGLE_BAD,
            reasons=["[红线] 含具体买入建议与价位"],
            report_kind="report",
            max_rounds=2,
            llm_call=stubborn_llm,
            safe_degrade_enabled=True,
        )
        assert res.passed is False
        assert res.final_action == FINAL_REJECT
        assert res.final_text == SINGLE_BAD

    def test_llm_exception_tries_degrade_first(self):
        """llm 抛错 + 开关开 + 可定位段 → 降级发布（保底交付优于 reject）。"""
        def broken_llm(system, user):
            raise RuntimeError("model unavailable")

        res = repair_report(
            MULTI_BAD,
            reasons=["[红线] 含具体买入建议与价位"],
            report_kind="report",
            max_rounds=2,
            llm_call=broken_llm,
            safe_degrade_enabled=True,
        )
        # 不抛异常；MULTI_BAD 可降级 → emit_degraded
        assert res.final_action == FINAL_EMIT_DEGRADED
        assert DEGRADED_PLACEHOLDER in res.final_text

    def test_final_action_enum_single_definition(self):
        """共享约定 #1：三终态枚举字面量唯一定义点在 repair.py。"""
        assert FINAL_EMIT == "emit"
        assert FINAL_EMIT_DEGRADED == "emit_degraded"
        assert FINAL_REJECT == "reject"


# ---------------------------------------------------------------------------
# 5. P1.5 self_consistency 跨段配对定位：根因修复（跨段矛盾不再空白报告）
# ---------------------------------------------------------------------------
# 涨停 claim 在段 0，负收益 evidence 在段 2（跨段矛盾，旧逻辑回退 document 级）
SELF_CONSISTENCY_CROSS = (
    "# 600036 招商银行 复盘\n\n"
    "该股今日强势涨停，封板后全天无抛压，资金高度认可。\n\n"
    "技术面看量能温和放大，换手充分，短期趋势仍强。\n\n"
    "该股今日收跌 -0.12%，量能萎缩，短期承压回落。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)


class TestP15SelfConsistencyCrossParagraph:
    def test_cross_paragraph_no_document_fallback(self):
        """P1.5：跨段矛盾产出 paragraph 级段，degrade 不再触发护栏 2 回退。"""
        res = validate(SELF_CONSISTENCY_CROSS, report_kind="stock")
        assert res.passed is False
        sc = [s for s in res.violation_segments if s.check == "self_consistency"]
        assert sc, "self_consistency 必须产出结构化段"
        assert all(s.granularity == "paragraph" for s in sc)
        assert all(s.paragraph_index is not None for s in sc)
        # degrade 不再因 document 级触发护栏 2（历史根因：空白报告）
        dres = assemble_degraded(SELF_CONSISTENCY_CROSS, sc, report_kind="stock")
        assert dres.ok is True
        assert dres.fallback_reason != "document_level_violation_not_removable"
