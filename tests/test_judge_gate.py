"""U3 事后校验 gate 测试。

覆盖验收标准：
  - 含幻觉样本（编造涨停 + 错误数字 + 保收益）→ judge 判 fail 且 gate 拦截
  - 正常样本 → pass 且放行（不写 jsonl）
  - source_facts 数字核对（涨停与行情源矛盾）
  - 红线（保收益 / 必涨）
  - LLM-as-judge 钩子（fake）低分 → fail
  - 关闭 judge（enabled=False）→ 一律放行
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.core.validator import (
    JudgeConfig,
    ValidationResult,
    gate_report,
    validate,
)

# ---- 样本 ----
HALLUCINATION = """# 600036 招商银行 复盘

今日该股强势**涨停**，涨幅高达 +12.5%，封板后毫无抛压。
技术面完美，资金大幅流入。基于当前形态，**必涨**，建议现价买入，
目标价 15.80 元，保收益无风险，稳赚不赔。
明日大概率继续一字板。"""

NORMAL = """# 600036 招商银行 复盘

今日收盘报 38.12 元，微跌 0.34%，成交额 18.6 亿，换手率 0.42%。
北向资金小幅净流出，行业板块整体偏弱。技术面处于 20 日线附近震荡，
量能温和。短期以观望为主，等待方向选择。

> 以上分析基于公开数据，不构成投资建议。"""

LIMIT_CONFLICT = (
    "个股今日未触及涨停，但报告称已封板涨停，涨幅 +10.02%。"
)


def _tmp_jsonl(tmp_path: Path) -> str:
    return str(tmp_path / "judge_rejects.jsonl")


def test_hallucination_judge_fails():
    res = validate(HALLUCINATION)
    assert isinstance(res, ValidationResult)
    assert res.passed is False
    assert res.score < 0.5
    # 至少命中红线 + 不可能数值 + 未举证中的多项
    assert any(not c.passed for c in res.checks)


def test_hallucination_gate_intercepts(tmp_path: Path):
    log = _tmp_jsonl(tmp_path)
    res = gate_report(
        HALLUCINATION, report_kind="stock", log_path=log, context={"file": "x.md"}
    )
    assert res.passed is False
    # gate 拦截：写 jsonl
    lines = Path(log).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "stock"
    assert rec["passed"] is False
    assert isinstance(rec["reasons"], list) and rec["reasons"]


def test_normal_passes_and_not_logged(tmp_path: Path):
    log = _tmp_jsonl(tmp_path)
    res = gate_report(NORMAL, report_kind="stock", log_path=log)
    assert res.passed is True
    assert res.score >= 0.5
    # 正常样本不写 jsonl
    assert not Path(log).exists()


def test_numeric_source_conflict():
    # 行情源显示未涨停，但报告称涨停 → 数字核对 fail
    res = validate(
        LIMIT_CONFLICT,
        source_facts={"is_limit_up": False},
        report_kind="stock",
    )
    assert res.passed is False
    assert any(c.name == "numeric_source" and not c.passed for c in res.checks)


def test_red_line_guarantee():
    res = validate("该股必涨，保收益无风险，稳赚不赔。", report_kind="stock")
    assert res.passed is False
    assert any(c.name == "red_line" and not c.passed for c in res.checks)


def test_impossible_value():
    res = validate("今日涨幅 +12.5%，远超主板限制。", report_kind="stock")
    iv = [c for c in res.checks if c.name == "impossible_move"]
    assert iv and not iv[0].passed


def test_llm_judge_hook_low_score():
    def fake_llm(text, *, report_kind=None, source_facts=None):
        return {"score": 0.1, "reasons": ["LLM 判定内容不可信"]}

    res = validate(
        NORMAL,
        report_kind="stock",
        config=JudgeConfig(use_llm=True),
        llm_judge=fake_llm,
    )
    assert res.passed is False
    assert any(c.name == "llm_judge" for c in res.checks)


def test_llm_judge_hook_high_score_passes():
    def fake_llm(text, *, report_kind=None, source_facts=None):
        return {"score": 0.95, "reasons": ["LLM 判定内容可信"]}

    # 正常样本 + 高 LLM 分，且无红线 → 通过
    res = validate(
        NORMAL,
        report_kind="stock",
        config=JudgeConfig(use_llm=True),
        llm_judge=fake_llm,
    )
    assert res.passed is True


def test_disabled_config_passes_everything():
    cfg = JudgeConfig(enabled=False)
    res = validate(HALLUCINATION, config=cfg)
    assert res.passed is True
    assert res.reasons == ["judge disabled"]


def test_market_and_vibe_kinds_routable():
    # 同一 hallucination 逻辑对大盘 / vibe 同样生效（kind 仅用于日志）
    for kind in ("market", "vibe"):
        res = validate(HALLUCINATION, report_kind=kind)
        assert res.passed is False
