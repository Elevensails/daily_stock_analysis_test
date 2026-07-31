# -*- coding: utf-8 -*-
"""Tests for RAGContext dataclass and SourceTrace."""

import pytest
from src.rag.context import RAGContext, SourceTrace


class TestRAGContext:
    """RAGContext 构造与默认值测试。"""

    def test_default_construction(self):
        """默认构造时所有字段为默认值。"""
        ctx = RAGContext()
        assert ctx.stock_code == ""
        assert ctx.stock_name == ""
        assert ctx.financial_block == ""
        assert ctx.technical_block == ""
        assert ctx.news_block == ""
        assert ctx.industry_block == ""
        assert ctx.source_trace == []
        assert ctx.is_empty is True

    def test_partial_construction(self):
        """部分字段赋值后 is_empty 正确反映状态。"""
        ctx = RAGContext(
            stock_code="600036",
            stock_name="招商银行",
            financial_block="### 财报速览\n| PE | 12.3 |",
        )
        assert ctx.stock_code == "600036"
        assert ctx.stock_name == "招商银行"
        assert ctx.is_empty is False
        assert ctx.financial_block != ""
        assert ctx.technical_block == ""
        assert ctx.news_block == ""

    def test_full_construction(self):
        """所有 block 都有值时。"""
        ctx = RAGContext(
            stock_code="000001",
            stock_name="平安银行",
            financial_block="### 财报速览\n数据...",
            technical_block="### 技术信号\n- MACD金叉",
            news_block="### 近期动态\n- 公告...",
            industry_block="### 行业对比\n...",
        )
        assert ctx.is_empty is False

    def test_whitespace_only_blocks(self):
        """仅含空格的 block 应被视为空。"""
        ctx = RAGContext(
            financial_block="   \n  ",
            technical_block="\t",
            news_block="\n\n",
        )
        assert ctx.is_empty is True

    def test_source_trace_append(self):
        """source_trace 可以正常追加 SourceTrace 记录。"""
        ctx = RAGContext(stock_code="600036")
        trace = SourceTrace(
            dimension="financial",
            primary_source="neodata",
            actual_source="neodata",
            elapsed_ms=1234.5,
            success=True,
        )
        ctx.source_trace.append(trace)
        assert len(ctx.source_trace) == 1
        assert ctx.source_trace[0].dimension == "financial"
        assert ctx.source_trace[0].success is True


class TestSourceTrace:
    """SourceTrace 数据类测试。"""

    def test_default_construction(self):
        trace = SourceTrace()
        assert trace.dimension == ""
        assert trace.primary_source == ""
        assert trace.actual_source == ""
        assert trace.elapsed_ms == 0.0
        assert trace.error is None
        assert trace.success is False

    def test_error_trace(self):
        trace = SourceTrace(
            dimension="news",
            primary_source="neodata",
            actual_source="none",
            elapsed_ms=5000.0,
            error="timeout after 8.0s",
            success=False,
        )
        assert trace.dimension == "news"
        assert trace.error == "timeout after 8.0s"
        assert trace.success is False

    def test_success_trace(self):
        trace = SourceTrace(
            dimension="technical",
            primary_source="westock",
            actual_source="enhanced_context",
            elapsed_ms=350.0,
            success=True,
        )
        assert trace.success is True
        assert trace.actual_source == "enhanced_context"
