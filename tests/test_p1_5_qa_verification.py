# -*- coding: utf-8 -*-
"""P1.5 self_consistency 跨段配对定位 — 独立 QA 验证套件（Edward / software-qa-engineer）。

本套件为 QA 角色对工程师交付代码的 **独立验证**，依据：
  * 架构师《系统设计 — P1.5 self_consistency 跨段配对定位》（权威技术规格）
  * PRD《P1.5 增量 PRD》的验收目标（G1/G2/G3、§3.2 需求池）

权威行为约定（来自系统设计 Part A §5.1 / 共享知识 #1）：
  - 每条跨段矛盾只产出 **1 条** paragraph 级 segment：
      * ``paragraph_index`` = evidence 段（含矛盾数字，待剥离）
      * ``related_paragraph_index`` = claim 段（含 涨停/跌停 标记，仅上下文，degrade **不剥离**）
      * ``pairing`` ∈ {limit_up_vs_negative_pct, limit_down_vs_positive_pct}
  - self_consistency **不再回退 document 级**（根因修复）

⚠️ 与 PRD 的差异（已在测试报告中向 team-lead 报备，非代码缺陷）：
  PRD §3.1 / §4 / §3.2-P0 文字描述为「矛盾双方各产一条段」「idxs == {0,1}」，
  与架构师裁决（仅剥 evidence、保留 claim 上下文）冲突；且 PRD §4 的 2 段极简
  探针若只剥 evidence 段，移除比会 >50% 命中护栏回退 reject。故本套件断言
  **架构师实现的真实行为**，并以 `test_prd_probe_expectation_doc` 显式记录该差异。
"""
from __future__ import annotations

from src.core.degrade import DEGRADED_PLACEHOLDER, assemble_degraded
from src.core.repair import FINAL_EMIT_DEGRADED, FINAL_REJECT, repair_report
from src.core.validator import (
    JudgeConfig,
    ValidationResult,
    ViolationSegment,
    locate_violations,
    split_paragraphs,
    validate,
)


# ---------------------------------------------------------------------------
# 样本文本（paragraph 顺序以 0 起始）
# ---------------------------------------------------------------------------

# 涨停 claim(段0) + 负收益 evidence(段1) —— 对应 PRD §4 探针形态
LIMIT_UP_NEG_MINIMAL = (
    "该股今日涨停，封板坚决，资金强势介入。\n\n"
    "从分时结构看，收盘下跌 -0.12%，主力小幅流出。"
)

# 多段 realistic 样本：涨停 claim / 技术面 / 负收益 evidence
LIMIT_UP_NEG_FULL = (
    "# 600036 招商银行 复盘\n\n"
    "该股今日强势涨停，封板后全天无抛压，资金高度认可。\n\n"
    "技术面看量能温和放大，换手充分，短期趋势仍强。\n\n"
    "该股今日收跌 -0.12%，量能萎缩，短期承压回落。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)

# 跌停 claim / 正收益 evidence
LIMIT_DOWN_POS_FULL = (
    "# 600036 招商银行 复盘\n\n"
    "该股今日直接打到跌停，恐慌盘涌出，空头主导全天。\n\n"
    "技术面看量能温和放大，换手充分，短期趋势仍弱。\n\n"
    "该股今日却逆势上涨 +3.45%，资金抢筹明显，分歧加大。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)

# 同段内自相矛盾（P1：单段既含涨停又含负收益）→ 段内复现路径，非跨段配对
INTRA_PARA_CONTRADICTION = (
    "该股今日强势涨停，封板坚决，但收盘竟然下跌 -0.12%，资金出现明显分歧。"
)

# 多对跨段矛盾（P2）：每只股票各自的 claim 紧邻其 evidence，避免 _nearest_claim 跨股误配
MULTI_PAIR = (
    "# 双股复盘\n\n"
    "股票A今日强势涨停，封板坚决，机构大幅买入。\n\n"
    "股票A收盘下跌 -0.50%，主力小幅流出，短期承压。\n\n"
    "股票A技术面量能温和，趋势仍强，尾盘稳定。\n\n"
    "股票B今日涨停，封板后无抛压，资金高度认可。\n\n"
    "股票B尾盘跳水跌 -1.20%，分歧明显加大，抛压加重。\n\n"
    "股票B技术面换手充分，形态完好，资金承接较强。"
)

# 干净报告：无任何自相矛盾
CLEAN_REPORT = (
    "# 复盘\n\n"
    "该股今日放量上涨 3.20%，资金持续流入，趋势向好。\n\n"
    "技术面量价齐升，换手充分，短线仍有上行动能。"
)

# 仅 red_line 违规、无自相矛盾
RED_LINE_ONLY = "建议买入该股，目标价 12.5 元，止损 11.0 元，仓位三成。"

# 中性文本（仅用于验证 llm_judge document 级回退保留）
NEUTRAL = "今日市场整体震荡，成交一般，观望情绪较浓。"


def _sc_segments(text, **kw) -> "tuple[ValidationResult, list[ViolationSegment]]":
    """返回 (ValidationResult, self_consistency 结构化违规段列表)。"""
    res = validate(
        text,
        report_kind=kw.get("report_kind", "stock"),
        config=kw.get("config"),
        llm_judge=kw.get("llm_judge"),
    )
    segs = [s for s in res.violation_segments if s.check == "self_consistency"]
    return res, segs


class TestLocateSelfConsistencyUnit:
    """单元层：_locate_self_consistency / _nearest_claim 纯函数行为。"""

    def test_limit_up_neg_single_evidence_segment(self):
        paras = split_paragraphs(LIMIT_UP_NEG_MINIMAL)
        segs = _locate_self_consistency_whitebox(paras)
        assert len(segs) == 1, "每条跨段矛盾只产 1 条 evidence 段"
        s = segs[0]
        assert s.paragraph_index == 1, "paragraph_index 应为 evidence 段"
        assert s.related_paragraph_index == 0, "related 应为 claim 段（上下文）"
        assert s.related_paragraph_index != s.paragraph_index
        assert s.pairing == "limit_up_vs_negative_pct"
        assert s.granularity == "paragraph"

    def test_limit_down_pos_single_evidence_segment(self):
        paras = split_paragraphs(LIMIT_DOWN_POS_FULL)
        segs = _locate_self_consistency_whitebox(paras)
        # 段号：0 标题 /1 跌停claim /2 技术面 /3 正收益evidence /4 引用
        ev = [s for s in segs if s.pairing == "limit_down_vs_positive_pct"]
        assert ev, "跌停+正收益应产出配对段"
        s = ev[0]
        assert s.paragraph_index == 3
        assert s.related_paragraph_index == 1
        assert s.pairing == "limit_down_vs_positive_pct"

    def test_nearest_claim_picks_own_stock(self):
        # MULTI_PAIR 中 A 的 evidence(段2) 最近 claim 为段1，B 的 evidence(段5) 最近 claim 为段4
        paras = split_paragraphs(MULTI_PAIR)
        segs = _locate_self_consistency_whitebox(paras)
        by_ev = {s.paragraph_index: s for s in segs}
        assert by_ev[2].related_paragraph_index == 1, "A evidence 应配对 A claim"
        assert by_ev[5].related_paragraph_index == 4, "B evidence 应配对 B claim"


def _locate_self_consistency_whitebox(paragraphs):
    """白盒调用私有定位器（测试内部可见，节奏与实现一致）。"""
    from src.core.validator import _locate_self_consistency
    return _locate_self_consistency(paragraphs)


class TestValidatorIntegration:
    """集成层：validate() 产出的 violation_segments 满足 P0/P1。"""

    def test_limit_up_neg_paragraph_level_no_document(self):
        res, segs = _sc_segments(LIMIT_UP_NEG_FULL)
        assert res.passed is False, "全文自洽性应 fail"
        assert segs, "应产出 self_consistency 段"
        assert all(s.granularity == "paragraph" for s in segs)
        assert all(s.paragraph_index is not None for s in segs)
        # 权威行为：仅 evidence 段（段3）被定位，claim（段1）仅作 related 上下文
        assert {s.paragraph_index for s in segs} == {3}
        s = segs[0]
        assert s.related_paragraph_index == 1
        assert s.pairing == "limit_up_vs_negative_pct"
        # 关键不变量：不存在任何 document 级 self_consistency 段
        doc_segs = [
            s for s in res.violation_segments
            if s.check == "self_consistency" and s.granularity == "document"
        ]
        assert not doc_segs, "self_consistency 不得回退 document 级"

    def test_intra_para_contradiction_uses_inpara_path(self):
        """P1：单段内自相矛盾 → 段内复现路径，related/pairing 为空（非跨段配对）。"""
        res, segs = _sc_segments(INTRA_PARA_CONTRADICTION)
        assert res.passed is False
        assert segs
        assert len(segs) == 1
        s = segs[0]
        assert s.granularity == "paragraph"
        assert s.paragraph_index == 0
        assert s.related_paragraph_index is None, "段内矛盾无关联段"
        assert s.pairing is None, "段内矛盾无 pairing"
        assert "涨停" in s.quote and "-0.12%" in s.quote

    def test_clean_report_has_no_self_consistency(self):
        res, segs = _sc_segments(CLEAN_REPORT)
        assert res.passed is True
        assert segs == [], "干净报告不应有 self_consistency 段"


class TestOtherChecksUnaffected:
    """P1：其他 check 的定位/回退逻辑不受 self_consistency 改造影响。"""

    def test_red_line_locates_paragraph_level(self):
        res = validate(RED_LINE_ONLY, report_kind="stock")
        assert res.passed is False
        rl = [s for s in res.violation_segments if s.check == "red_line"]
        assert rl, "red_line 应产出段"
        assert all(s.granularity == "paragraph" for s in rl)
        # 无 self_consistency（样本无涨停/负收益矛盾）
        assert not [s for s in res.violation_segments if s.check == "self_consistency"]

    def test_llm_judge_document_fallback_preserved(self):
        """非 self_consistency 的 check（llm_judge）仍走 document 级回退。"""
        def fake_judge(text, *, report_kind, source_facts):
            return {"score": 0.2, "reasons": ["judge low"]}

        cfg = JudgeConfig(enabled=True, use_llm=True, min_score=0.5)
        res = validate(NEUTRAL, config=cfg, llm_judge=fake_judge)
        assert res.passed is False
        llm_segs = [s for s in res.violation_segments if s.check == "llm_judge"]
        assert llm_segs, "llm_judge 应产出段"
        assert all(s.granularity == "document" for s in llm_segs)
        # 且 self_consistency 在含矛盾文本中仍是 paragraph 级（对照）
        res2, sc = _sc_segments(LIMIT_UP_NEG_FULL)
        assert sc and all(s.granularity == "paragraph" for s in sc)


class TestDegradeStripsOnlyEvidence:
    """G2：degrade 仅剥离 evidence 段（paragraph_index），保留 claim 上下文。"""

    def test_limit_up_neg_degrade_ok(self):
        _, segs = _sc_segments(LIMIT_UP_NEG_FULL)
        dres = assemble_degraded(LIMIT_UP_NEG_FULL, segs, report_kind="stock")
        assert dres.ok is True
        assert dres.fallback_reason != "document_level_violation_not_removable"
        assert "收跌 -0.12%" not in dres.degraded_text, "evidence 段应被剥离"
        assert "强势涨停" in dres.degraded_text, "claim 上下文段应保留"
        assert DEGRADED_PLACEHOLDER in dres.degraded_text
        assert dres.removed_ratio < 0.5
        # 降级稿复检通过
        assert validate(dres.degraded_text, report_kind="stock").passed is True

    def test_limit_down_pos_degrade_ok(self):
        _, segs = _sc_segments(LIMIT_DOWN_POS_FULL)
        dres = assemble_degraded(LIMIT_DOWN_POS_FULL, segs, report_kind="stock")
        assert dres.ok is True
        assert "逆势上涨 +3.45%" not in dres.degraded_text
        assert "打到跌停" in dres.degraded_text, "claim 上下文段应保留"
        assert validate(dres.degraded_text, report_kind="stock").passed is True

    def test_degraded_segments_carry_pairing_context(self):
        _, segs = _sc_segments(LIMIT_UP_NEG_FULL)
        dres = assemble_degraded(LIMIT_UP_NEG_FULL, segs, report_kind="stock")
        assert dres.ok is True
        assert dres.removed_segments
        loc = dres.removed_segments[0]["location"]
        assert loc["related_paragraph_index"] == 1
        assert loc["pairing"] == "limit_up_vs_negative_pct"


class TestMultiplePairsP2:
    """P2：多对跨段矛盾全部配对定位，降级剥离后复检通过（受 50% 护栏约束）。"""

    def test_multi_pair_located_and_degraded(self):
        res, segs = _sc_segments(MULTI_PAIR)
        assert res.passed is False
        assert len(segs) == 2, "两对矛盾应产 2 条 evidence 段"
        idxs = {s.paragraph_index for s in segs}
        assert idxs == {2, 5}, "evidence 段应被定位（A段2 / B段5）"
        assert all(s.related_paragraph_index is not None for s in segs)
        assert all(s.pairing == "limit_up_vs_negative_pct" for s in segs)
        assert all(s.granularity == "paragraph" for s in segs)

        dres = assemble_degraded(MULTI_PAIR, segs, report_kind="stock")
        assert dres.ok is True, f"多对降级应 ok，fallback={dres.fallback_reason}"
        assert dres.removed_ratio < 0.5
        # 两段矛盾数字均被移除，涨停 claim 保留 → 复检通过
        assert "-0.50%" not in dres.degraded_text
        assert "-1.20%" not in dres.degraded_text
        assert "股票A今日强势涨停" in dres.degraded_text
        assert validate(dres.degraded_text, report_kind="stock").passed is True


class TestEndToEndRepair:
    """G3：端到端 repair_report 跨段矛盾 → emit_degraded（非 reject）。"""

    def test_repair_emit_degraded_when_llm_no_fix(self):
        def no_fix_llm(system, user):
            return LIMIT_UP_NEG_FULL  # 模型始终不修，保留原矛盾

        res = repair_report(
            LIMIT_UP_NEG_FULL,
            reasons=["[自相矛盾] 称涨停但出现负收益 -0.12%"],
            report_kind="stock",
            max_rounds=2,
            llm_call=no_fix_llm,
            safe_degrade_enabled=True,
        )
        assert res.final_action == FINAL_EMIT_DEGRADED, "应降级发布而非 reject"
        assert "-0.12%" not in res.final_text, "降级稿应已移除矛盾数字"
        assert res.passed is True

    def test_repair_reject_when_degrade_disabled(self):
        def no_fix_llm(system, user):
            return LIMIT_UP_NEG_FULL

        res = repair_report(
            LIMIT_UP_NEG_FULL,
            reasons=["[自相矛盾] 称涨停但出现负收益 -0.12%"],
            report_kind="stock",
            max_rounds=2,
            llm_call=no_fix_llm,
            safe_degrade_enabled=False,
        )
        assert res.final_action == FINAL_REJECT, "关闭降级开关应回退 reject"


class TestContractSchema:
    """Schema 契约：to_dict 透传 related_paragraph_index / pairing（向后兼容）。"""

    def test_violation_segment_to_dict_carrier(self):
        seg = ViolationSegment(
            check="self_consistency", severity="critical",
            reason="跨段矛盾", quote="x", granularity="paragraph",
            paragraph_index=3, line_start=10, line_end=12,
            related_paragraph_index=1, pairing="limit_up_vs_negative_pct",
        )
        d = seg.to_dict()
        assert d["location"]["paragraph_index"] == 3
        assert d["location"]["related_paragraph_index"] == 1
        assert d["location"]["pairing"] == "limit_up_vs_negative_pct"

    def test_locate_violations_returns_consistent_segments(self):
        res, segs = _sc_segments(LIMIT_UP_NEG_FULL)
        # locate_violations 直接调用应得到一致结果
        from src.core.validator import _check_internal_consistency
        checks = [_check_internal_consistency(LIMIT_UP_NEG_FULL)]
        direct = locate_violations(LIMIT_UP_NEG_FULL, checks)
        direct_sc = [s for s in direct if s.check == "self_consistency"]
        assert {s.paragraph_index for s in direct_sc} == {s.paragraph_index for s in segs}


class TestPrdProbeExpectationDoc:
    """记录 PRD §4 探针与实现行为的差异（非测试失败，纯文档化断言真实行为）。"""

    def test_prd_probe_expectation_doc(self):
        res, segs = _sc_segments(LIMIT_UP_NEG_MINIMAL)
        assert res.passed is False
        # PRD §4 写 `idxs == {0, 1}`（双方都定位）；实现按架构师裁决只定位 evidence 段：
        # 仅 paragraph_index == {1}。下方断言真实行为，并显式说明与 PRD 文字不符。
        assert {s.paragraph_index for s in segs} == {1}, (
            "实现仅剥 evidence 段(段1)；PRD §4 的 idxs=={0,1} 与架构师裁决冲突"
        )
        # 另：该 2 段极简样本若只剥 evidence 段，移除比 >50% 会命中护栏回退 reject，
        # 故 PRD §4 的 `assert dres.ok` 在此极简样本上同样不成立——需用 realistic 多段样本。
        dres = assemble_degraded(LIMIT_UP_NEG_MINIMAL, segs, report_kind="stock")
        assert dres.ok is False, "极简样本移除比 >50%，预期护栏回退（验证护栏生效）"
        assert dres.fallback_reason.startswith("removed_ratio")
