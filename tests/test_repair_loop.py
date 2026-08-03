"""U3 防幻觉闭环 — repair loop 回归测试（AC1 / AC3 / AC4 / AC5 / AC6）。

用 ``unittest.mock`` 注入 ``llm_call``，零网络、零重型 import（不 import analyzer）。
覆盖：
  - AC1：幻觉样本经 repair 后发布（方向写反 / 红线两种样本）
  - AC3：正常样本不进 repair loop（llm_call 不被调用，rounds=0，emit）
  - AC4：超轮数回退 reject（rounds==max_rounds，jsonl 写 final_action=reject）
  - AC5：llm_call 抛异常安全降级（不抛异常，final_action=reject）
  - AC6：完整 gate 不可绕过（部分修复仍 fail → 下一轮才修好，rounds=2）
"""
from __future__ import annotations

import json
from pathlib import Path

from src.core.repair import FINAL_EMIT_DEGRADED, repair_report, RepairResult
from src.core.validator import (
    ValidationResult,
    append_reject_record,
    gate_report,
    validate,
)


# ---- 样本 ----
# 方向写反（market_review）：称涨停但出现负收益 → 自相矛盾 fail
DIRECTION_BAD = "今日大盘强势涨停，涨幅 -3.5%，封板后毫无抛压。"
DIRECTION_FIXED = "今日大盘小幅下跌 3.5%，量能温和，观望为主。"

# 红线违规（report）：买入建议+价位 + 保收益 → 红线 fail
REDLINE_BAD = "建议现价买入，目标价 15.80 元，保收益无风险，稳赚不赔。"
REDLINE_FIXED = "短期以观望为主，等待方向选择，以上不构成投资建议。"

# 正常样本（首轮即通过，不进 loop）
NORMAL = (
    "# 600036 招商银行 复盘\n\n"
    "今日收盘报 38.12 元，微跌 0.34%，成交额 18.6 亿。\n"
    "北向资金小幅净流出，短期以观望为主。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)


def _refuses_call(system, user):
    raise AssertionError("llm_call 不应被调用（AC3 守卫）")


def test_ac1_direction_repaired_and_emitted():
    calls = []

    def fake_llm(system, user):
        calls.append((system, user))
        return DIRECTION_FIXED

    res = repair_report(
        DIRECTION_BAD,
        reasons=["[自相矛盾] 称涨停但出现负收益 -3.5%"],
        report_kind="market_review",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert isinstance(res, RepairResult)
    assert res.passed is True
    # market_review 的 self_consistency 现为 warn 级（不拦截），不再进 repair loop
    assert res.final_action == "emit"
    assert res.rounds == 0
    assert res.rewritten is False


def test_ac1_redline_repaired_and_emitted():
    def fake_llm(system, user):
        return REDLINE_FIXED

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位", "[红线] 含保收益 / 必涨必跌等绝对化承诺"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.passed is True
    assert res.final_action == "emit"
    assert res.rounds >= 1
    assert res.final_text == REDLINE_FIXED


def test_ac3_normal_sample_skips_loop():
    from unittest.mock import Mock

    fake_llm = Mock(side_effect=_refuses_call)
    res = repair_report(
        NORMAL,
        reasons=[],
        report_kind="stock",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.passed is True
    assert res.final_action == "emit"
    assert res.rounds == 0
    assert res.rewritten is False
    fake_llm.assert_not_called()


def test_ac4_exhausted_rounds_reject():
    # mock 始终返回仍违规文本（红线未修）
    def fake_llm(system, user):
        return REDLINE_BAD

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.passed is False
    assert res.final_action == "reject"
    assert res.rounds == 2  # 轮数耗尽 == max_rounds
    assert res.final_text == REDLINE_BAD  # 永不 emit 不合格版


def test_ac4_jsonl_writes_final_action_reject(tmp_path: Path):
    log = str(tmp_path / "judge_rejects.jsonl")
    result = validate(REDLINE_BAD, report_kind="report")
    assert result.passed is False
    append_reject_record(
        log,
        "report",
        result,
        context={"file": "x.md"},
        final_action="reject",
        repair_rounds=2,
        rewritten=True,
        repair_reasons=["[红线] 含具体买入建议与价位"],
    )
    lines = Path(log).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["final_action"] == "reject"
    assert rec["repair_rounds"] == 2
    assert rec["rewritten"] is True
    assert rec["repair_reasons"]


def test_ac5_llm_exception_safe_degrade():
    def fake_llm(system, user):
        raise RuntimeError("model unavailable")

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    # 不抛异常，安全降级为 reject
    assert res.passed is False
    assert res.final_action == "reject"
    assert res.final_text == REDLINE_BAD


def test_ac6_partial_fix_still_fails_then_fixed():
    # 第 1 次：只修好红线但方向仍写反（仍 fail）；第 2 次：完全修好（pass）
    seq = [
        "该股今日涨停，但收跌 -5.0%。",  # 红线已去，但方向自相矛盾 → fail
        "该股今日小幅下跌 5.0%，量能温和。",  # 干净 → pass
    ]
    calls = {"n": 0}

    def fake_llm(system, user):
        i = calls["n"]
        calls["n"] += 1
        return seq[i % len(seq)]

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.passed is True
    assert res.final_action == "emit"
    assert res.rounds == 2  # 部分修复未误 emit，走了第二轮


def test_gate_report_log_false_does_not_write(tmp_path: Path):
    # 向后兼容：log=False 仅校验不写 jsonl（emit 自行控制终态）
    log = str(tmp_path / "judge_rejects.jsonl")
    res = gate_report(
        REDLINE_BAD,
        report_kind="report",
        log_path=log,
        context={"file": "x.md"},
        log=False,
    )
    assert res.passed is False
    assert not Path(log).exists()


# ---------------------------------------------------------------------------
# U3 修改策略新增（P0-2 定向改段 / P0-3 最新原因回传 / P1-1 温度透传）。
# 既有 19 个用例原样保留，以上均为加法扩展。
# ---------------------------------------------------------------------------

# 多段样本：仅第 2 段（0 起始段号）踩红线，其余段干净
MULTI_BAD = (
    "# 600036 招商银行 复盘\n\n"
    "今日收盘报 38.12 元，微跌 0.34%，成交额 18.6 亿。\n\n"
    "建议现价买入，目标价 15.80 元，稳赚不赔。\n\n"
    "北向资金小幅净流出，短期以观望为主。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)


def test_p02_targeted_prompt_contains_segment_pointer():
    """P0-2：重写 prompt 必须携带结构化段指针（段号 + 原文引用）。"""
    prompts: list[str] = []

    def fake_llm(system, user):
        prompts.append(user)
        return REDLINE_FIXED

    res = repair_report(
        MULTI_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.passed is True
    assert res.final_action == "emit"
    assert prompts, "llm_call 未被调用"
    # 段指针：定位到第 2 段（0 起始段号）
    assert "第 2 段" in prompts[0]
    # 原文引用送达模型（定向改段依据）
    assert "买入" in prompts[0]


def test_p02_explicit_segments_param_used():
    """P0-2：显式传入 violation_segments 时应直接采用（emit 桥接通道）。"""
    from src.core.validator import ViolationSegment

    prompts: list[str] = []

    def fake_llm(system, user):
        prompts.append(user)
        return REDLINE_FIXED

    seg = ViolationSegment(
        check="red_line", severity="critical",
        reason="含具体买入建议与价位", quote="建议现价买入",
        granularity="paragraph", paragraph_index=2, line_start=5, line_end=5,
    )
    res = repair_report(
        MULTI_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        violation_segments=[seg],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.final_action == "emit"
    assert "第 2 段" in prompts[0]
    assert "建议现价买入" in prompts[0]


def test_p03_latest_reasons_fed_to_next_round():
    """P0-3：第 N 轮 prompt 必须使用第 N-1 轮 validate 的最新原因（非首轮原因）。"""
    prompts: list[str] = []
    seq = [
        "该股今日涨停，但收跌 -5.0%。",      # 第 1 轮产物：红线已修，但引入自相矛盾
        "该股今日小幅下跌 5.0%，量能温和。",  # 第 2 轮产物：干净
    ]
    calls = {"n": 0}

    def fake_llm(system, user):
        prompts.append(user)
        i = calls["n"]
        calls["n"] += 1
        return seq[i % len(seq)]

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
    )
    assert res.passed is True
    assert res.rounds == 2
    assert len(prompts) == 2
    # 第 1 轮 prompt：喂首轮红线原因
    assert "含具体买入建议与价位" in prompts[0]
    # 第 2 轮 prompt：喂最新一轮的自相矛盾原因，且不再重复已修复的红线原因
    assert "自相矛盾" in prompts[1]
    assert "含具体买入建议与价位" not in prompts[1]
    # 第 2 轮的待修订正文应为第 1 轮修订稿（渐进修复，非从原文重来）
    assert seq[0] in prompts[1]


def test_p03_last_reasons_recorded_on_reject():
    """P0-3：终态结果须携带末轮最新原因（last_reasons），供 jsonl 回查。"""
    def fake_llm(system, user):
        return "该股今日涨停，但收跌 -5.0%。"  # 始终自相矛盾

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        llm_call=fake_llm,
        safe_degrade_enabled=False,  # 关闭降级，验证纯 reject 路径
    )
    assert res.final_action == "reject"
    assert res.last_reasons, "last_reasons 不应为空"
    assert any("自相矛盾" in r for r in res.last_reasons)


def test_p11_temperature_and_model_passthrough():
    """P1-1：llm_call 声明 temperature/model 关键字时必须真正透传。"""
    seen: dict = {}

    def fake_llm(system, user, *, temperature=None, model=None):
        seen["temperature"] = temperature
        seen["model"] = model
        return REDLINE_FIXED

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        model="deepseek/deepseek-v4-flash",
        max_rounds=2,
        temperature=0.55,
        llm_call=fake_llm,
    )
    assert res.final_action == "emit"
    assert seen["temperature"] == 0.55
    assert seen["model"] == "deepseek/deepseek-v4-flash"


def test_p11_two_param_mock_still_compatible():
    """P1-1 兼容性守卫：纯两参 (system, user) mock 不得被透传破坏。"""
    def fake_llm(system, user):  # 无 temperature/model 关键字
        return REDLINE_FIXED

    res = repair_report(
        REDLINE_BAD,
        reasons=["[红线] 含具体买入建议与价位"],
        report_kind="report",
        max_rounds=2,
        temperature=0.9,  # 即便显式给温度，也不应传给两参 mock
        llm_call=fake_llm,
    )
    assert res.final_action == "emit"


# ---------------------------------------------------------------------------
# P1.5 self_consistency 跨段配对定位（增量扩展，不改动既有 19 用例）。
# claim 段 0 含涨停，evidence 段 2 含负收益 → 跨段矛盾。
# ---------------------------------------------------------------------------
SELF_CONSISTENCY_CROSS = (
    "# 600036 招商银行 复盘\n\n"
    "该股今日强势涨停，封板后全天无抛压，资金高度认可。\n\n"
    "技术面看量能温和放大，换手充分，短期趋势仍强。\n\n"
    "该股今日收跌 -0.12%，量能萎缩，短期承压回落。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)


def test_p15_cross_paragraph_self_consistency_terminal():
    """P1.5：跨段 self_consistency 经 repair（模型不修）→ 降级发布 emit_degraded。

    断言：终态为 emit_degraded（剥离 evidence 段，关联 claim 保留）；
    降级稿过完整 gate；prompt 含关联段提示；degraded_segments 携带 pairing。
    """
    prompts: list[str] = []

    def fake_llm(system, user):
        prompts.append(user)
        return SELF_CONSISTENCY_CROSS  # 模型始终不修，用于观察终态

    res = repair_report(
        SELF_CONSISTENCY_CROSS,
        reasons=["[自相矛盾] 称涨停但出现负收益 -0.12%"],
        report_kind="stock",
        max_rounds=2,
        llm_call=fake_llm,
        safe_degrade_enabled=True,
    )
    assert res.final_action == FINAL_EMIT_DEGRADED
    assert res.passed is True
    assert "收跌 -0.12%" not in res.final_text
    assert "强势涨停" in res.final_text  # 关联 claim 段保留
    # 降级稿必须过完整 gate（永不 emit 不合格版）
    assert validate(res.final_text, report_kind="stock").passed is True
    # 终态段清单携带配对信息（jsonl 审计）
    assert res.degraded_segments
    assert res.degraded_segments[0]["location"]["pairing"] == "limit_up_vs_negative_pct"
    # prompt 含关联段提示（模型定向修订时可一并核对矛盾两端）
    assert any("关联段" in p for p in prompts)


def test_append_reject_record_extra_fields(tmp_path: Path):
    log = str(tmp_path / "judge_rejects.jsonl")
    result: ValidationResult = validate(NORMAL, report_kind="stock")
    assert result.passed is True
    append_reject_record(
        log,
        "stock",
        result,
        context={"file": "ok.md"},
        final_action="emit",
        repair_rounds=1,
        rewritten=True,
        repair_reasons=["[红线] 修正"],
    )
    rec = json.loads(Path(log).read_text(encoding="utf-8").strip())
    assert rec["final_action"] == "emit"
    assert rec["repair_rounds"] == 1
    assert rec["rewritten"] is True
    assert rec["repair_reasons"] == ["[红线] 修正"]
    # 旧字段保持兼容
    assert rec["kind"] == "stock"
    assert "reasons" in rec and "checks" in rec
