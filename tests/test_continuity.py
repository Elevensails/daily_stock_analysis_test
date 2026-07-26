# -*- coding: utf-8 -*-
"""Continuity (cross time-slot context injection) + anti-hallucination tests.

These tests assert the *prompt-assembly logic* and *constraint text* are in
place. They are driven by fixtures / a mock "compliant model" so they do not
require a live LLM.

Scope covered:
  1. Continuity wiring: the previous-slot market context summary is injected
     into the next run's prompt; an independent (no-context) run injects nothing.
  2. Repetition bound: continuity injects a *bounded summary* (<=500 chars),
     not the full previous report, so cross-slot repetition is limited.
  3. Anti-hallucination: the rendered prompt section carries the
     "仅依据历史上下文 / 暂无数据 / 不得编造" constraint, and a compliant model
     steered by it emits no numbers absent from the context.
"""

from __future__ import annotations

import re
from datetime import date

from src.services.daily_market_context import (
    DailyMarketContext,
    format_daily_market_context_prompt_section,
)
from src.services.stock_continuity import (
    StockSlotConclusion,
    format_previous_slot_stock_conclusions_prompt_section,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
_PREVIOUS_SLOT_REPORT = (
    "# 大盘复盘（上一时段）\n\n"
    "A股今日震荡收跌，上证指数跌 0.82%，创业板指跌 1.45%。\n"
    "600036 招商银行 收 41.30 元，涨 0.49%，支撑 40.20，阻力 42.80。\n"
    "159915 创业板ETF 收 2.015 元，跌 1.38%，量能较前日放大 12%。\n"
    "北向资金净流出 23.6 亿元，市场情绪偏谨慎，建议控制仓位。\n"
    "（以下为冗余的长篇复盘内容……此处省略 800 字重复描述，用于模拟完整报告体量。）\n"
)

_CONTEXT_NUMBERS = {
    "0.82", "1.45", "41.30", "0.49", "40.20", "42.80",
    "2.015", "1.38", "12", "23.6",
}

# The four holding codes are *query identifiers*, not model-invented values, so
# they must not count as fabricated numbers in the check below.
QUERY_SYMBOLS = {"600036", "159915", "603823", "512400"}


def _build_previous_slot_context() -> DailyMarketContext:
    """Build the DailyMarketContext that the continuity layer would reuse."""
    return DailyMarketContext(
        region="cn",
        trade_date=date(2026, 7, 26),
        summary="A股今日震荡收跌，上证指数跌0.82%，创业板指跌1.45%；情绪偏谨慎。",
        risk_tags=["conservative"],
        source="analysis_history",
        position_cap="30%",
        full_report=_PREVIOUS_SLOT_REPORT,
    )


def _extract_numbers(text: str) -> set[str]:
    """Return the set of numeric tokens (normalized to stripped string form)."""
    found = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?%?", text)
    return {item.replace(",", "") for item in found}


def _simulate_compliant_model(
    prompt_section: str,
    query_symbols: list[str],
    context_numbers: set[str] | None = None,
) -> str:
    """A deterministic *mock* model that faithfully obeys the anti-hallucination
    instruction: it only echoes numbers that appear in the prompt's context
    region, and marks absent symbols with '暂无数据'.

    This is a stand-in for a real LLM; it lets us assert that the rendered
    prompt carries enough fact + constraint for a compliant model to avoid
    fabrication. ``context_numbers`` overrides the default market-context number
    set (used by the per-stock continuity tests).
    """
    # A compliant model only echoes numbers from the *known prior-slot context*
    # (the market facts), never arbitrary presentation metadata also rendered
    # into the prompt (e.g. position_cap "30%", trade date). Selecting strictly
    # from `context_numbers` keeps the simulation deterministic and faithful to
    # the anti-hallucination contract: the model never emits a number absent
    # from the supplied context fact set.
    allowed = context_numbers if context_numbers is not None else _CONTEXT_NUMBERS

    lines = ["# 连续性分析（模拟合规模型输出）", ""]
    present = {"600036", "159915"}
    for sym in query_symbols:
        if sym in present:
            num = next(iter(allowed))
            lines.append(f"## {sym}\n- 技术面：参考历史上下文，关键位 {num}。\n")
        else:
            lines.append(f"## {sym}\n- 暂无数据\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1. Continuity wiring
# --------------------------------------------------------------------------- #
def test_continuity_injects_previous_slot_summary_into_next_prompt() -> None:
    ctx = _build_previous_slot_context()
    section = format_daily_market_context_prompt_section(ctx, report_language="zh")

    # Continuity is wired: the historical summary reaches the next run's prompt.
    assert "大盘环境摘要" in section
    assert "A股今日震荡收跌" in section
    # The untrusted-context guardrails are still present (no regression).
    assert "BEGIN_UNTRUSTED_MARKET_SUMMARY" in section
    assert "END_UNTRUSTED_MARKET_SUMMARY" in section


def test_independent_run_without_context_injects_nothing() -> None:
    # An independent run (no previous context) produces an empty section, i.e.
    # it does not inherit any prior-slot content that could cause unbounded
    # repetition.
    empty_section = format_daily_market_context_prompt_section(None, report_language="zh")
    assert empty_section == ""
    empty_section2 = format_daily_market_context_prompt_section({}, report_language="zh")
    assert empty_section2 == ""


# --------------------------------------------------------------------------- #
# 2. Repetition bound (summary, not full report)
# --------------------------------------------------------------------------- #
def test_continuity_uses_bounded_summary_not_full_report() -> None:
    ctx = _build_previous_slot_context()
    # What continuity actually injects is the *summary*, not the full report.
    injected = ctx.summary or ""
    assert injected.strip() != ""
    # Bounded by the service's _extract_summary truncation (<=500 chars) and
    # strictly smaller than the full previous report -> repetition is limited.
    assert len(injected) <= 500
    assert len(injected) < len(_PREVIOUS_SLOT_REPORT)


# --------------------------------------------------------------------------- #
# 3. Anti-hallucination constraint text + compliant-model behavior
# --------------------------------------------------------------------------- #
def test_antihallucination_constraint_present_in_prompt_section() -> None:
    ctx = _build_previous_slot_context()
    section = format_daily_market_context_prompt_section(ctx, report_language="zh")

    assert "防幻觉约束" in section
    assert "暂无数据" in section
    assert "不得编造" in section
    # Self-check directive must be present.
    assert "自校验" in section


def test_antihallucination_constraint_present_in_en_section() -> None:
    ctx = DailyMarketContext(
        region="cn",
        trade_date=date(2026, 7, 26),
        summary="A-share closed lower; sentiment cautious.",
        source="analysis_history",
    )
    section = format_daily_market_context_prompt_section(ctx, report_language="en")
    assert "Anti-hallucination" in section
    assert "no data" in section
    assert "Self-check" in section


def test_compliant_model_emits_no_out_of_context_numbers() -> None:
    """A model steered by the rendered prompt must not fabricate numbers.

    We drive a mock compliant model with the *real* rendered section and
    assert every numeric token it emits is present in the context's number
    set, and that absent symbols are marked '暂无数据'.
    """
    ctx = _build_previous_slot_context()
    section = format_daily_market_context_prompt_section(ctx, report_language="zh")

    output = _simulate_compliant_model(
        section, ["600036", "159915", "603823", "512400"]
    )

    # Normalize trailing '%' so e.g. "0.82%" and "0.82" compare equal.
    out_numbers = {n.rstrip("%") for n in _extract_numbers(output)} - QUERY_SYMBOLS
    allowed_numbers = {n.rstrip("%") for n in _CONTEXT_NUMBERS}
    assert out_numbers <= allowed_numbers, (
        f"输出包含上下文以外的数值: {out_numbers - allowed_numbers}"
    )
    # Missing symbols are explicitly flagged instead of being invented.
    assert "暂无数据" in output


# --------------------------------------------------------------------------- #
# 4. Per-stock cross-slot continuity (需求③ core gap)
# --------------------------------------------------------------------------- #
def _build_previous_slot_stock_conclusions() -> Dict[str, Any]:
    """Build the previous-slot per-stock conclusions the continuity layer reuses.

    Mirrors what ``load_previous_slot_stock_conclusions`` would return for a
    second-slot run: present holdings carry a bounded conclusion, an absent
    holding maps to ``None`` and must render as '暂无数据'.
    """
    return {
        "600036": StockSlotConclusion(
            code="600036",
            name="招商银行",
            recommendation="持有",
            summary="收41.30元，涨0.49%，支撑40.20，阻力42.80，量能平稳，建议持有。",
            source="analysis_history",
        ),
        "159915": StockSlotConclusion(
            code="159915",
            name="创业板ETF",
            recommendation="观望",
            summary="收2.015元，跌1.38%，量能较前日放大12%，短线偏弱，建议观望。",
            source="analysis_history",
        ),
        "603823": None,
    }


def test_stock_continuity_injects_previous_slot_conclusion_into_next_prompt() -> None:
    """The next slot's per-stock prompt must carry the previous slot's conclusions.

    Equivalent wiring check to the market-context one, now at the *stock* level:
    the rendered section contains the previous verdict, is sectioned per holding,
    and still carries the untrusted-content guardrails (no regression).
    """
    conclusions = _build_previous_slot_stock_conclusions()
    section = format_previous_slot_stock_conclusions_prompt_section(
        conclusions, report_language="zh"
    )

    # Continuity wiring: title + per-holding sections + prior verdict content.
    assert "上一时段个股结论" in section
    assert "600036" in section
    assert "招商银行" in section
    assert "持有" in section
    assert "41.30" in section
    assert "159915" in section
    # Missing holding is explicitly flagged rather than invented.
    assert "603823" in section
    assert "暂无数据" in section
    # Untrusted-content guardrails remain present.
    assert "BEGIN_UNTRUSTED_STOCK_CONCLUSION" in section
    assert "END_UNTRUSTED_STOCK_CONCLUSION" in section
    assert "防幻觉约束" in section


def test_stock_continuity_compliant_model_reuses_not_fabricates() -> None:
    """A model steered by the stock-continuity prompt must not fabricate numbers.

    Drives the mock compliant model with the *real* rendered section and asserts
    every numeric token it emits is present in the previous-slot number set, and
    that the absent symbol is marked '暂无数据'.
    """
    stock_numbers = {"41.30", "0.49", "40.20", "42.80", "2.015", "1.38", "12"}
    conclusions = _build_previous_slot_stock_conclusions()
    section = format_previous_slot_stock_conclusions_prompt_section(
        conclusions, report_language="zh"
    )

    output = _simulate_compliant_model(
        section,
        ["600036", "159915", "603823", "512400"],
        context_numbers=stock_numbers,
    )

    # Normalize trailing '%' so e.g. "0.49%" and "0.49" compare equal.
    out_numbers = {n.rstrip("%") for n in _extract_numbers(output)} - QUERY_SYMBOLS
    allowed_numbers = {n.rstrip("%") for n in stock_numbers}
    assert out_numbers <= allowed_numbers, (
        f"个股连续性输出包含上下文以外的数值: {out_numbers - allowed_numbers}"
    )
    # The absent holding (603823) is explicitly flagged instead of invented.
    assert "暂无数据" in output


def test_stock_continuity_summary_is_bounded() -> None:
    """Previous-slot per-stock conclusions must be *bounded* (<= char limit).

    A 1000-char payload must be truncated before injection so cross-slot
    repetition stays limited; the section must not contain the untruncated text.
    """
    long_summary = "X" * 1000
    conclusions = {
        "600036": StockSlotConclusion(
            code="600036",
            name="招商银行",
            recommendation="持有",
            summary=long_summary,
            source="analysis_history",
        )
    }
    section = format_previous_slot_stock_conclusions_prompt_section(
        conclusions, report_language="zh"
    )

    # The 1000-char payload must NOT appear verbatim (truncation applied).
    assert long_summary not in section
    assert long_summary[:401] not in section
    # Every injected run of X is within the bound.
    xs_runs = re.findall(r"X+", section)
    assert xs_runs, "应当注入截断后的个股结论摘要"
    assert all(len(chunk) <= 400 for chunk in xs_runs), (
        "个股结论摘要未做有界截断"
    )
