# -*- coding: utf-8 -*-
"""P1.5 self_consistency 跨段配对定位 — 单元测试。

覆盖（增量，零网络、零新增第三方依赖）：
  - 涨停 + 负收益 / 跌停 + 正收益 两种跨段矛盾方向
  - validate 产出 paragraph 级段（granularity=paragraph，非 document）
  - related_paragraph_index 非空、pairing 取值正确
  - _nearest_claim 选最近 claim 段（而非段 0）
  - 定位层不再回退 document 级（修正 P1.5 根因：跨段矛盾 → 空白报告）
  - degrade 仅剥离 evidence 段（paragraph_index），关联段（related）保留 → emit_degraded
  - repaired prompt 渲染「关联段为矛盾来源」提示模型一并核对
"""
from __future__ import annotations

from src.core.degrade import DEGRADED_PLACEHOLDER, assemble_degraded
from src.core.repair import repair_report
from src.core.validator import validate


# ---- 样本：跨段矛盾（claim 段与 evidence 段分属不同段落）----

# 涨停 claim 在段 0，负收益 evidence 在段 2（跨段矛盾）
LIMIT_UP_NEG = (
    "# 600036 招商银行 复盘\n\n"
    "该股今日强势涨停，封板后全天无抛压，资金高度认可。\n\n"
    "技术面看量能温和放大，换手充分，短期趋势仍强。\n\n"
    "该股今日收跌 -0.12%，量能萎缩，短期承压回落。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)

# 跌停 claim 在段 0，正收益 evidence 在段 2（跨段矛盾）
LIMIT_DOWN_POS = (
    "# 600036 招商银行 复盘\n\n"
    "该股今日直接打到跌停，恐慌盘涌出，空头主导全天。\n\n"
    "技术面看量能温和放大，换手充分，短期趋势仍弱。\n\n"
    "该股今日却逆势上涨 +3.45%，资金抢筹明显，分歧加大。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)

# claim 段在段 3、evidence 在段 1，验证 _nearest_claim 选最近而非段 0
LIMIT_UP_NEG_SPLIT = (
    "# 600036 招商银行 复盘\n\n"
    "早盘该股小幅高开，资金试探性流入。\n\n"
    "盘中该股一度下探 -0.88%，量能跟随萎缩。\n\n"
    "午后回补缺口，尾盘翻红，技术形态修复。\n\n"
    "盘后龙虎榜显示该股强势涨停，封板坚决，机构大幅买入。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)


def _self_consistency_segments(text: str):
    """从 validate 结果取 self_consistency 结构化违规段（真实定位层产物）。"""
    res = validate(text, report_kind="stock")
    assert res.passed is False
    return [s for s in res.violation_segments if s.check == "self_consistency"]


class TestCrossParagraphSelfConsistency:
    def test_limit_up_vs_negative_pct(self):
        segs = _self_consistency_segments(LIMIT_UP_NEG)
        assert segs, "涨停+负收益跨段矛盾必须产出 self_consistency 段"
        assert len(segs) == 1
        s = segs[0]
        assert s.granularity == "paragraph"
        # 段号 0 起：# 标题(0)、涨停 claim(1)、技术面(2)、负收益 evidence(3)、引用(4)
        assert s.paragraph_index == 3, "待剥离段应为 evidence 段（负收益 -0.12%）"
        assert s.related_paragraph_index == 1, "矛盾来源段应为 claim 段（涨停）"
        assert s.related_paragraph_index != s.paragraph_index
        assert s.pairing == "limit_up_vs_negative_pct"

    def test_limit_down_vs_positive_pct(self):
        segs = _self_consistency_segments(LIMIT_DOWN_POS)
        assert segs, "跌停+正收益跨段矛盾必须产出 self_consistency 段"
        assert len(segs) == 1
        s = segs[0]
        assert s.granularity == "paragraph"
        assert s.paragraph_index == 3, "待剥离段应为 evidence 段（正收益 +3.45%）"
        assert s.related_paragraph_index == 1, "矛盾来源段应为 claim 段（跌停）"
        assert s.pairing == "limit_down_vs_positive_pct"

    def test_nearest_claim_selected(self):
        # 段号 0 起：# 标题(0)、早盘(1)、下探 evidence(2)、午后(3)、涨停 claim(4)、引用(5)
        segs = _self_consistency_segments(LIMIT_UP_NEG_SPLIT)
        assert segs
        s = segs[0]
        assert s.paragraph_index == 2, "evidence 段应为含 -0.88% 的段 2"
        assert s.related_paragraph_index == 4, "应配对最近的 claim 段（段 4）而非段 0"
        assert s.pairing == "limit_up_vs_negative_pct"

    def test_no_document_level_fallback(self):
        """P1.5 根因修复：跨段矛盾不再回退 document 级。"""
        for sample in (LIMIT_UP_NEG, LIMIT_DOWN_POS, LIMIT_UP_NEG_SPLIT):
            segs = _self_consistency_segments(sample)
            assert segs, "每种跨段矛盾都应产出 self_consistency 段"
            for s in segs:
                assert s.granularity == "paragraph"
                assert s.paragraph_index is not None
                assert s.related_paragraph_index is not None


class TestDegradeStripsOnlyEvidence:
    def test_limit_up_neg_degrade_ok(self):
        segs = _self_consistency_segments(LIMIT_UP_NEG)
        dres = assemble_degraded(LIMIT_UP_NEG, segs, report_kind="stock")
        assert dres.ok is True
        assert dres.fallback_reason != "document_level_violation_not_removable"
        # 仅剥离 evidence 段（含 -0.12%），关联 claim（涨停）保留
        assert "收跌 -0.12%" not in dres.degraded_text
        assert "强势涨停" in dres.degraded_text, "关联 claim 段不应被剥离"
        assert DEGRADED_PLACEHOLDER in dres.degraded_text
        assert dres.removed_ratio < 0.5
        # 降级稿复检通过（涨停 claim 在，负收益 evidence 已移除）
        assert validate(dres.degraded_text, report_kind="stock").passed is True

    def test_limit_down_pos_degrade_ok(self):
        segs = _self_consistency_segments(LIMIT_DOWN_POS)
        dres = assemble_degraded(LIMIT_DOWN_POS, segs, report_kind="stock")
        assert dres.ok is True
        assert "逆势上涨 +3.45%" not in dres.degraded_text
        assert "打到跌停" in dres.degraded_text, "关联 claim 段不应被剥离"
        assert validate(dres.degraded_text, report_kind="stock").passed is True

    def test_degraded_segments_carry_pairing(self):
        """jsonl 审计：removed_segments 透传 related_paragraph_index / pairing。"""
        segs = _self_consistency_segments(LIMIT_UP_NEG)
        dres = assemble_degraded(LIMIT_UP_NEG, segs, report_kind="stock")
        assert dres.ok is True
        assert dres.removed_segments
        loc = dres.removed_segments[0]["location"]
        assert loc["related_paragraph_index"] == 1
        assert loc["pairing"] == "limit_up_vs_negative_pct"


class TestRepairPromptRelatedHint:
    def test_prompt_contains_related_segment_hint(self):
        prompts: list[str] = []
        # 模型始终不修（保留原矛盾文本），用于观察 prompt 内容与终态
        def fake_llm(system, user):
            prompts.append(user)
            return LIMIT_UP_NEG

        res = repair_report(
            LIMIT_UP_NEG,
            reasons=["[自相矛盾] 称涨停但出现负收益 -0.12%"],
            report_kind="stock",
            max_rounds=2,
            llm_call=fake_llm,
            safe_degrade_enabled=True,
        )
        assert prompts, "llm_call 未被调用"
        # 关联段提示送达模型（P1.5：使其定向修订时可定位矛盾两端）
        assert "关联段" in prompts[0]
        assert "矛盾来源" in prompts[0]
        assert "第 3 段" in prompts[0], "evidence 段指针应出现"
        # 终态应为降级发布（模型不修，剥离 evidence → emit_degraded）
        assert res.final_action in ("emit", "emit_degraded")
