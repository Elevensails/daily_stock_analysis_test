# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — Prompt 格式化器
===================================

将 RAGContext 格式化为可注入 LLM prompt 的 Markdown section。

规则：
- 每个子 block 非空才输出
- 全部空则返回空字符串（不注入空 section）
- 总计 ≤1500 chars（config.rag_max_prompt_tokens）
- source_trace 不注入 prompt（仅日志用）
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.rag.context import RAGContext

logger = logging.getLogger(__name__)

# RAG section 锚定标记
RAG_SECTION_HEADER = "## 🔍 外部数据检索（RAG Grounding）"


def format_rag_section(rag_context: "RAGContext") -> str:
    """将 RAGContext 格式化为 prompt section。

    Args:
        rag_context: RAG 检索结果

    Returns:
        Markdown 格式的 RAG section 字符串；若所有 block 为空则返回 ""
    """
    if rag_context is None:
        return ""

    sections: list = []

    # 财报 block (≤400 chars)
    if rag_context.financial_block and rag_context.financial_block.strip():
        section = rag_context.financial_block.strip()
        if len(section) > 400:
            section = section[:397] + "..."
        sections.append(section)

    # 技术信号 block (≤500 chars)
    if rag_context.technical_block and rag_context.technical_block.strip():
        section = rag_context.technical_block.strip()
        if len(section) > 500:
            section = section[:497] + "..."
        sections.append(section)

    # 新闻动态 block (≤600 chars)
    if rag_context.news_block and rag_context.news_block.strip():
        section = rag_context.news_block.strip()
        if len(section) > 600:
            section = section[:597] + "..."
        sections.append(section)

    # 行业 block (P1 预留)
    if rag_context.industry_block and rag_context.industry_block.strip():
        section = rag_context.industry_block.strip()
        if len(section) > 300:
            section = section[:297] + "..."
        sections.append(section)

    if not sections:
        return ""

    body = "\n\n".join(sections)
    result = f"{RAG_SECTION_HEADER}\n\n{body}"

    # 总 token 预算校验（字符近似）
    max_tokens = 1500
    if len(result) > max_tokens:
        logger.warning(
            "[RAG.formatter] RAG section 超预算 (%d > %d chars)，截断最后一条记录",
            len(result), max_tokens,
        )
        # 截断策略：缩短最后一个 block
        overshoot = len(result) - max_tokens + 3  # +3 for "..."
        if sections:
            last = sections[-1]
            if len(last) > overshoot:
                sections[-1] = last[:(len(last) - overshoot)] + "..."
            else:
                sections.pop()
        body = "\n\n".join(sections)
        result = f"{RAG_SECTION_HEADER}\n\n{body}"
        # 如果还是超预算，强制截断
        if len(result) > max_tokens:
            result = result[:max_tokens - 3] + "..."

    return result
