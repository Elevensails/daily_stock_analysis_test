# -*- coding: utf-8 -*-
"""
===================================
U6 RAG（检索增强生成）包
===================================

职责：
1. 从外部数据源（neodata / westock / efinance / search_service）检索补充数据
2. 将检索结果格式化为 prompt 可注入的 Markdown section
3. 所有数据源故障/超时自动降级，绝不阻断主流程

导出：
- RAGContext: 检索结果数据类
- retrieve_financial_context: 主检索入口
- format_rag_section: 格式化 RAG prompt section
"""

from src.rag.context import RAGContext, SourceTrace
from src.rag.retriever import retrieve_financial_context
from src.rag.formatter import format_rag_section

__all__ = [
    "RAGContext",
    "SourceTrace",
    "retrieve_financial_context",
    "format_rag_section",
]
