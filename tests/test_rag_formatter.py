# -*- coding: utf-8 -*-
"""Tests for RAG formatter (format_rag_section)."""

import pytest
from src.rag.context import RAGContext, SourceTrace
from src.rag.formatter import format_rag_section, RAG_SECTION_HEADER


class TestFormatRagSection:
    """format_rag_section 各类场景测试。"""

    def test_none_context(self):
        """传入 None 返回空字符串。"""
        assert format_rag_section(None) == ""

    def test_all_empty_blocks(self):
        """所有 block 为空时返回空字符串。"""
        ctx = RAGContext()
        assert format_rag_section(ctx) == ""

    def test_financial_only(self):
        """仅有财报 block 时正确格式化。"""
        ctx = RAGContext(
            stock_code="600036",
            stock_name="招行",
            financial_block="### 财报速览\n| 指标 | 数值 |\n|------|------|\n| PE | 12.3 |",
        )
        result = format_rag_section(ctx)
        assert RAG_SECTION_HEADER in result
        assert "### 财报速览" in result
        assert "PE" in result
        # 不应包含其他 section
        assert "技术信号" not in result
        assert "近期动态" not in result

    def test_all_blocks(self):
        """所有三个 block 都在时正确组合。"""
        ctx = RAGContext(
            stock_code="000001",
            stock_name="平安银行",
            financial_block="### 财报速览\n| PE | 10.5 |",
            technical_block="### 技术信号\n- MACD金叉",
            news_block="### 近期动态\n- 发布业绩预告",
        )
        result = format_rag_section(ctx)
        assert RAG_SECTION_HEADER in result
        assert "### 财报速览" in result
        assert "### 技术信号" in result
        assert "### 近期动态" in result

    def test_source_trace_not_injected(self):
        """source_trace 不注入 prompt section。"""
        ctx = RAGContext(
            stock_code="600036",
            financial_block="### 财报速览\n数据",
            source_trace=[
                SourceTrace(dimension="financial", primary_source="neodata",
                            actual_source="neodata", success=True, elapsed_ms=100.0),
            ],
        )
        result = format_rag_section(ctx)
        assert "source_trace" not in result
        assert "neodata" not in result  # source_trace 不应泄露到 prompt

    def test_token_budget_enforcement(self):
        """超过 1500 字符时截断。"""
        # 构造一个超长 block
        long_text = "### 财报速览\n" + "| 数据 | " + "x" * 2000 + " |\n"
        ctx = RAGContext(financial_block=long_text)
        result = format_rag_section(ctx)
        assert len(result) <= 1503  # 允许少量溢出（...）

    def test_partial_blocks_mixed(self):
        """混合空和非空 block。"""
        ctx = RAGContext(
            stock_code="600036",
            financial_block="",
            technical_block="### 技术信号\n- 数据",
            news_block="",
        )
        result = format_rag_section(ctx)
        assert RAG_SECTION_HEADER in result
        assert "技术信号" in result
        assert "财报" not in result
        assert "近期动态" not in result

    def test_industry_block_p1(self):
        """行业 block（P1 预留）也能正确注入。"""
        ctx = RAGContext(
            stock_code="600036",
            industry_block="### 行业对比\n- 排名 3/20",
        )
        result = format_rag_section(ctx)
        assert RAG_SECTION_HEADER in result
        assert "行业对比" in result

    def test_header_no_duplicate_injection(self):
        """format 不应该在输出中包含重复的 header。"""
        ctx = RAGContext(
            financial_block="### 财报速览\n数据数据",
            technical_block="### 技术信号\n信号数据",
        )
        result = format_rag_section(ctx)
        # header 只出现一次
        assert result.count(RAG_SECTION_HEADER) == 1
