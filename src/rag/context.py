# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — 数据结构定义
===================================

定义 RAG 检索结果的 dataclass，用于在整个 RAG 链路（retriever → formatter → analyzer）中传递数据。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SourceTrace:
    """单个数据维度的来源追踪记录。

    用于日志/监控，不注入 prompt。
    """

    dimension: str = ""               # 维度名称：financial / technical / news / industry
    primary_source: str = ""          # 首选数据源
    actual_source: str = ""           # 实际使用的数据源
    elapsed_ms: float = 0.0           # 检索耗时（毫秒）
    error: Optional[str] = None       # 错误信息（成功时为 None）
    success: bool = False             # 是否成功获取到有效数据


@dataclass
class RAGContext:
    """RAG 检索结果数据类。

    包含四个维度的检索结果字符串及来源追踪记录。
    所有 block 均为空字符串表示该维度无数据。
    formatter 负责将非空 block 组装为 prompt section。

    Attributes:
        stock_code: 股票代码
        stock_name: 股票名称
        financial_block: 财报关键指标（已格式化为 markdown）
        technical_block: 衍生技术信号（已格式化为 markdown）
        news_block: 近期动态（已格式化为 markdown）
        industry_block: 行业/板块对比（P1 预留，当前总为 ""）
        source_trace: 各维度的数据来源追踪列表
    """

    stock_code: str = ""
    stock_name: str = ""
    financial_block: str = ""
    technical_block: str = ""
    news_block: str = ""
    industry_block: str = ""
    source_trace: List[SourceTrace] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """是否所有 block 都为空（无任何检索数据可用）。"""
        return not any([
            self.financial_block.strip(),
            self.technical_block.strip(),
            self.news_block.strip(),
            self.industry_block.strip(),
        ])
