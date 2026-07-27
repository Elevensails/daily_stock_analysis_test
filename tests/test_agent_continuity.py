# -*- coding: utf-8 -*-
"""Agent 路径跨时段个股连续性测试（T01/U1）。

本测试聚焦 PRD 验收标准中最关键的闭环：pipeline 预渲染的上一时段个股结论段
（``previous_slot_stock_conclusions``，字符串形态）会被 ``AgentExecutor._build_user_message``
正确追加进最终 user message prompt。

pipeline 侧注入逻辑（``_build_agent_continuity_section`` + ``_analyze_with_agent`` 写入
``initial_context``）由代码审查保证，其本身仅是对既有
``format_previous_slot_stock_conclusions_prompt_section`` 的调用与异常兜底，已在
``tests/test_continuity.py`` 中覆盖该格式化函数的渲染、截断、防幻觉约束。

全部离线、不调用真实 LLM/网络/DB，不标 network marker，纳入 ci.yml 的 offline-tests
门禁。

运行（项目根目录）：
    python3 -m pytest tests/test_agent_continuity.py -q
"""

from __future__ import annotations

import sys
from typing import Any, Dict
from unittest.mock import MagicMock

# 本地测试环境可能未安装 litellm；executor 仅在该模块需要时才调用其功能，
# 而本测试只验证 _build_user_message 对 continuity 段落的拼装逻辑，故先 mock。
if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()

# 强制预加载 executor 模块，避免 pkgutil 解析子模块问题。
from src.agent import executor as _executor_module  # noqa: F401


def _build_dummy_continuity_section(code: str = "600036") -> str:
    """构造一个符合实际渲染形态的连续性段落字符串。"""
    return (
        f"## 上一时段个股结论（{code}）\n"
        "BEGIN_UNTRUSTED_STOCK_CONCLUSION\n"
        f"- {code} 招商银行：持有，收41.30元，涨0.49%，支撑40.20，阻力42.80。\n"
        "END_UNTRUSTED_STOCK_CONCLUSION\n"
        "防幻觉约束：仅依据上述上下文，缺失数据请明确标注'暂无数据'，不得编造。\n"
    )


def test_build_user_message_appends_previous_slot_conclusions() -> None:
    """executor._build_user_message 应把 context 中的连续性段落追加进 prompt。"""
    from src.agent.executor import AgentExecutor

    executor = AgentExecutor(
        tool_registry=MagicMock(),
        llm_adapter=MagicMock(),
    )
    continuity_section = _build_dummy_continuity_section("600036")
    context: Dict[str, Any] = {
        "stock_code": "600036",
        "report_language": "zh",
        "previous_slot_stock_conclusions": continuity_section,
    }

    message = executor._build_user_message("请分析股票 600036", context=context)

    assert continuity_section in message
    assert "股票代码: 600036" in message
    assert "上一时段个股结论" in message
    assert "持有" in message
    assert "41.30" in message


def test_build_user_message_ignores_invalid_continuity_value() -> None:
    """连续性值非字符串或为空时，不应追加进 prompt。"""
    from src.agent.executor import AgentExecutor

    executor = AgentExecutor(
        tool_registry=MagicMock(),
        llm_adapter=MagicMock(),
    )
    context: Dict[str, Any] = {
        "stock_code": "600036",
        "report_language": "zh",
        "previous_slot_stock_conclusions": {"not": "a string"},  # type: ignore[dict-item]
    }

    message = executor._build_user_message("请分析股票 600036", context=context)

    assert "上一时段个股结论" not in message


def test_build_user_message_appends_english_continuity_section() -> None:
    """英文场景下同样应追加连续性段落。"""
    from src.agent.executor import AgentExecutor

    executor = AgentExecutor(
        tool_registry=MagicMock(),
        llm_adapter=MagicMock(),
    )
    section = "## Previous Slot Stock Conclusion (600036)\n- 600036 CMBC: HOLD.\n"
    context: Dict[str, Any] = {
        "stock_code": "600036",
        "report_language": "en",
        "previous_slot_stock_conclusions": section,
    }

    message = executor._build_user_message("Analyze stock 600036", context=context)

    assert section in message
    assert "English" in message
