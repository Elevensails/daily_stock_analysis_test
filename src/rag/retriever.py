# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — 主检索入口
===================================

组合三个子检索器（financial / technical / news），
对外暴露单一入口 ``retrieve_financial_context()``。

设计原则：
- Fail-open：任何子检索器异常返回空 block，不抛出
- 并行独立：各子检索器独立运行，互不影响
- 来源追踪：每个维度记录 SourceTrace 用于日志/监控
"""

import logging
import time
from typing import Any, Dict, Optional

from src.rag.context import RAGContext, SourceTrace

logger = logging.getLogger(__name__)


def retrieve_financial_context(
    code: str,
    stock_name: str,
    report_type: str = "daily",
    *,
    enhanced_context: Optional[Dict[str, Any]] = None,
    search_service: Any = None,
    config: Any = None,
) -> RAGContext:
    """检索股票的外部数据上下文（RAG 主入口）。

    并行调用三个子检索器（财报、技术信号、新闻），单个失败不影响其他。
    超时由各子检索器内部的 subprocess.run(timeout=N) 控制。

    Args:
        code: 股票代码（如 "600036"）
        stock_name: 股票中文名称（如 "招商银行"）
        report_type: 报告类型（daily / weekly / monthly），用于调整检索策略
        enhanced_context: 已增强的分析上下文（用于 technical 回退提取 MA/BIAS）
        search_service: 搜索服务实例（用于 news 回退）
        config: Config 实例（用于超时等配置读取）

    Returns:
        RAGContext: 包含各维度检索结果和来源追踪
    """
    rag_context = RAGContext(
        stock_code=code,
        stock_name=stock_name,
    )
    source_traces: list = []

    # ── 1. 财报检索 ──
    t0 = time.monotonic()
    try:
        from src.rag.financial import retrieve_financial
        financial_text = retrieve_financial(code, stock_name, config=config)
        rag_context.financial_block = financial_text
        source_traces.append(SourceTrace(
            dimension="financial",
            primary_source="neodata",
            actual_source="neodata" if financial_text else "none",
            elapsed_ms=(time.monotonic() - t0) * 1000,
            success=bool(financial_text),
        ))
    except Exception as exc:
        logger.warning("[RAG] 财报检索失败（降级，不阻塞）: %s", exc)
        source_traces.append(SourceTrace(
            dimension="financial",
            primary_source="neodata",
            actual_source="none",
            elapsed_ms=(time.monotonic() - t0) * 1000,
            error=str(exc),
            success=False,
        ))

    # ── 2. 技术信号检索 ──
    t0 = time.monotonic()
    try:
        from src.rag.technical import retrieve_technical
        technical_text = retrieve_technical(code, enhanced_context=enhanced_context, config=config)
        rag_context.technical_block = technical_text
        source_traces.append(SourceTrace(
            dimension="technical",
            primary_source="westock",
            actual_source="westock" if "衍生信号缺失" not in technical_text else "enhanced_context",
            elapsed_ms=(time.monotonic() - t0) * 1000,
            success=bool(technical_text),
        ))
    except Exception as exc:
        logger.warning("[RAG] 技术信号检索失败（降级，不阻塞）: %s", exc)
        source_traces.append(SourceTrace(
            dimension="technical",
            primary_source="westock",
            actual_source="none",
            elapsed_ms=(time.monotonic() - t0) * 1000,
            error=str(exc),
            success=False,
        ))

    # ── 3. 新闻检索（P1.6：基于 retrieve_news_ex 的精确 hit_source）──
    # 旧实现靠「news_text 里有没有 search_service 子串」猜数据源，会在命中
    # akshare_em 时误报为 neodata。改为直接读取 NewsRetrievalResult.hit_source。
    t0 = time.monotonic()
    try:
        from src.rag.news import retrieve_news_ex
        news_result = retrieve_news_ex(
            code, stock_name, search_service=search_service, config=config
        )
        rag_context.news_block = news_result.block
        source_traces.append(SourceTrace(
            dimension="news",
            primary_source="neodata",
            # 精确来源：neodata / akshare_em / search_service / none，不再字符串猜测
            actual_source=news_result.hit_source,
            elapsed_ms=news_result.elapsed_ms,
            success=news_result.success,
        ))
    except Exception as exc:
        logger.warning("[RAG] 新闻检索失败（降级，不阻塞）: %s", exc)
        source_traces.append(SourceTrace(
            dimension="news",
            primary_source="neodata",
            actual_source="none",
            elapsed_ms=(time.monotonic() - t0) * 1000,
            error=str(exc),
            success=False,
        ))

    rag_context.source_trace = source_traces
    logger.info(
        "[RAG] %s(%s) 检索完成: financial=%s, technical=%s, news=%s",
        stock_name, code,
        "ok" if rag_context.financial_block else "empty",
        "ok" if rag_context.technical_block else "empty",
        "ok" if rag_context.news_block else "empty",
    )
    return rag_context
