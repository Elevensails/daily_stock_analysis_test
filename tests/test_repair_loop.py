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

from src.core.repair import repair_report, RepairResult
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
    assert res.final_action == "emit"
    assert res.rounds >= 1
    assert res.rewritten is True
    assert res.final_text == DIRECTION_FIXED
    # reasons 应进入重写 prompt（grounding 信号送达模型）
    assert any("称涨停但出现负收益" in u for _, u in calls)


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
