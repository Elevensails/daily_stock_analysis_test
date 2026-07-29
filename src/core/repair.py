"""U3 防幻觉闭环 — repair loop（事后自动修复）。

拦截（gate fail）的报告不直接丢弃，而是进入修复循环：
  1. 用 ``reasons``（首轮违规反馈）+ 可选 ``source_facts`` 构造重写 prompt；
  2. 注入的 ``llm_call(system, user) -> str`` 整篇重写报告；
  3. 对重写文本跑**完整** ``validate()``（同首轮 config / llm_judge，不绕过）；
  4. 通过 → 返回 emit；仍 fail → 下一轮（最多 ``max_rounds``，硬上限 3）；
  5. 轮数耗尽或 ``llm_call`` 抛错 → 安全降级返回 reject（绝不抛到调用方）。

本模块**仅依赖标准库 + ``src.core.validator.validate``**，不 import analyzer，
便于单测用 mock 注入 ``llm_call``，零网络、零重型依赖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from src.core.prompts_repair import REPAIR_SYSTEM, REPAIR_USER_TEMPLATE
from src.core.validator import JudgeConfig, validate


@dataclass
class RepairResult:
    """一次 repair loop 的结果。"""

    passed: bool            # 最终是否通过完整 gate
    final_text: str         # 通过=修正后文本；失败=原始文本（永不 emit 不合格版）
    rounds: int             # 实际 rewrite 次数（首轮即通过=0）
    rewritten: bool         # 是否真正发起过模型重写（rounds>0）
    final_action: str       # "emit" | "reject"
    repair_reasons: list    # 喂给模型的首轮违规原因（便于回查）


def _build_rewrite_prompt(
    text: str,
    reasons: list,
    source_facts: Optional[dict],
    report_kind: str = "stock",
) -> "tuple[str, str]":
    """构造 (system, user) 重写 prompt（整篇重生成 + grounding 约束）。

    使用字符串替换而非 ``str.format``，避免正文/反馈中的 ``{`` ``}`` 触发格式化错误。
    """
    reasons_text = "\n".join(f"- {r}" for r in (reasons or [])) or "- （无具体反馈，请整体复核合规）"
    if source_facts:
        facts_text = json.dumps(source_facts, ensure_ascii=False, indent=2)
    else:
        facts_text = "（无，请仅依据校验反馈修正违规处，禁止编造数字）"

    user = (
        REPAIR_USER_TEMPLATE
        .replace("{report_kind}", report_kind)
        .replace("{reasons}", reasons_text)
        .replace("{source_facts_block}", facts_text)
        .replace("{original_text}", text)
    )
    return REPAIR_SYSTEM, user


def repair_report(
    text: str,
    *,
    reasons: list,
    source_facts: Optional[dict] = None,
    report_kind: str = "stock",
    model: Optional[str] = None,
    max_rounds: int = 2,
    llm_call: Callable[[str, str], str],
    config: Optional[JudgeConfig] = None,
    llm_judge: Optional[Callable[..., dict]] = None,
    temperature: float = 0.1,
) -> RepairResult:
    """fail → 重写 → 再校验循环。任意 llm_call 抛错即安全降级返回 reject。

    参数
    ----
    text: 首轮 gate 判定 fail 的报告全文
    reasons: 首轮 ``ValidationResult.reasons``（喂给模型，全程不变）
    source_facts: 行情源侧车（可选，决策④：缺失即退化，仅靠 reasons 自纠）
    report_kind: stock | market | vibe（用于日志与可读）
    model: 重写模型（默认由 llm_call 内部决定，一般 = REPAIR_MODEL/LITELLM_MODEL）
    max_rounds: 重写轮数（硬上限 3）
    llm_call: 注入的 (system, user) -> 重写文本；通常 = analyzer.call_rewrite_llm
    config: 与首轮相同，保证完整 gate 不绕过
    llm_judge: 可选 LLM judge（P1-2 预留）
    temperature: 重写温度（默认 0.1，照反馈改、不自由发挥）
    """
    cfg = config or JudgeConfig()
    reasons_list = list(reasons) if reasons else []

    # 防御：若原文已通过完整 gate（极端情况），直接 emit，不进 loop（AC3）
    pre = validate(
        text,
        source_facts=source_facts,
        report_kind=report_kind,
        config=cfg,
        llm_judge=llm_judge,
    )
    if pre.passed:
        return RepairResult(
            passed=True,
            final_text=text,
            rounds=0,
            rewritten=False,
            final_action="emit",
            repair_reasons=reasons_list,
        )

    rounds = 0
    current = text
    for i in range(min(int(max_rounds), 3)):
        try:
            system, user = _build_rewrite_prompt(
                current, reasons_list, source_facts, report_kind
            )
            rewritten = llm_call(system, user)
        except Exception:
            # 安全降级：模型不可用，立即 reject，绝不抛到调用方（AC5）
            return RepairResult(
                passed=False,
                final_text=text,
                rounds=i,
                rewritten=True,
                final_action="reject",
                repair_reasons=reasons_list,
            )

        # 对重写文本跑完整 gate（同首轮 config / llm_judge，不绕过任何 check）
        res = validate(
            rewritten,
            source_facts=source_facts,
            report_kind=report_kind,
            config=cfg,
            llm_judge=llm_judge,
        )
        if res.passed:
            return RepairResult(
                passed=True,
                final_text=rewritten,
                rounds=i + 1,
                rewritten=True,
                final_action="emit",
                repair_reasons=reasons_list,
            )
        # 仍 fail：记录轮数，把本轮重写作为下一轮输入（允许渐进修复）
        current = rewritten
        rounds = i + 1

    # 轮数耗尽仍 fail：回退 reject（现有行为）
    return RepairResult(
        passed=False,
        final_text=text,
        rounds=rounds,
        rewritten=True,
        final_action="reject",
        repair_reasons=reasons_list,
    )
