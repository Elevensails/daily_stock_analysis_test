# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆 — 门面（唯一对外入口）
===================================

``LongTermMemory`` 把 embedding provider / 向量存储 / 写入器聚合成一个对
pipeline 友好的门面：

- ``recall_for_stock(stock_code, query_text, ...)`` —— 把当前情境文本向量化
  后检索，返回 :class:`~src.memory.models.RecallResult`（含命中项与降级原因）。
- ``remember(...)`` —— 把一条已完成的结论**零网络**暂存进写缓冲。
- ``flush()`` —— 本 run 末尾一次性批量 embed + 幂等落盘。

铁律（设计 §8 第 9 条 / 第 12 条）：
- ``recall`` / ``remember`` / ``flush`` **永不向外抛异常**；失败一律返回
  ``RecallResult.empty(reason)`` 或静默 WARN（写入侧由 :class:`MemoryWriter` 兜底）。
- ``enabled`` 直读 ``config.ltm_enabled``，与 ``agent_memory_enabled`` 正交。
- ``from_config()`` 内部**惰性构造** provider / store / writer，避免 import 期
  连累 ``src.storage`` / ``litellm`` 等重模块。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np

from src.memory.models import (
    LOG_PREFIX,
    MemoryRecord,
    RecallQuery,
    RecallResult,
    normalize_time_slot,
)

logger = logging.getLogger(__name__)


class LongTermMemory:
    """语义召回门面。"""

    def __init__(self, config: Any) -> None:
        self._config = config
        # 正交性：enabled 只由 ltm_enabled 决定，不受 agent_memory_enabled 连坐
        self.enabled = bool(getattr(config, "ltm_enabled", False))
        self._writer: Any = None
        self._provider: Any = None
        self._store: Any = None

    @classmethod
    def from_config(cls, config: Any = None) -> "LongTermMemory":
        """按当前 config 构造门面（config 缺省时惰性取单例）。"""
        if config is None:
            from src.config import get_config

            config = get_config()
        return cls(config)

    # ------------------------------------------------------------------ #
    # 惰性构造
    # ------------------------------------------------------------------ #

    def _ensure_writer(self) -> Any:
        """惰性构造 writer（含 provider / store），并缓存复用。"""
        if self._writer is None:
            from src.memory.writer import build_memory_writer

            self._writer = build_memory_writer(self._config)
            self._provider = self._writer.provider
            self._store = self._writer.store
        return self._writer

    # ------------------------------------------------------------------ #
    # 召回
    # ------------------------------------------------------------------ #

    def recall_for_stock(
        self,
        stock_code: str,
        query_text: str,
        *,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
        scope: Optional[str] = None,
        report_type: Optional[str] = None,
        time_slot: Optional[str] = None,
        exclude_history_ids: Optional[List[int]] = None,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> RecallResult:
        """语义召回当前股票的历史相似情境。

        Returns:
            :class:`RecallResult`。任何异常路径都返回 ``RecallResult.empty(...)``，
            **绝不向外抛**。

        Notes:
            本方法不写遥测 JSONL（调用方——pipeline——持有 ``injected_chars``，
            由 pipeline 负责恰好一条记录的落盘，见设计 §8.11）。
        """
        try:
            if not self.enabled:
                return RecallResult.empty("disabled", enabled=False)

            writer = self._ensure_writer()
            provider = self._provider
            store = self._store
            cfg = self._config

            text = str(query_text or "").strip()
            if not text:
                # 无查询文本 = 合法但零命中，不降级（reason=None）
                return RecallResult.empty(
                    None,
                    enabled=True,
                    stock_code=str(stock_code or ""),
                    scope=str(scope or getattr(cfg, "ltm_scope", "same_stock")),
                )

            # ① 向量化查询（唯一允许抛异常的层，此处捕获降级）
            try:
                matrix = provider.embed([text])
                query_embedding = np.asarray(matrix, dtype=np.float32).reshape(-1)
                if query_embedding.size == 0:
                    raise ValueError("embedding 为空")
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 查询向量化失败，召回降级: %s", LOG_PREFIX, exc)
                return RecallResult.empty(
                    "embed_failed",
                    enabled=True,
                    embedding_provider=getattr(provider, "provider_name", ""),
                    model_id=getattr(provider, "model_id", ""),
                    stock_code=str(stock_code or ""),
                    scope=str(scope or getattr(cfg, "ltm_scope", "same_stock")),
                )

            # ② 组装查询（config 默认值 + 调用方覆盖）
            query = RecallQuery(
                stock_code=str(stock_code or ""),
                query_embedding=query_embedding,
                scope=str(scope or getattr(cfg, "ltm_scope", "same_stock")),
                top_k=int(top_k if top_k is not None else getattr(cfg, "ltm_top_k", 3)),
                min_similarity=float(
                    min_similarity
                    if min_similarity is not None
                    else getattr(cfg, "ltm_min_similarity", 0.75)
                ),
                lookback_days=int(getattr(cfg, "ltm_lookback_days", 90)),
                time_slot=normalize_time_slot(time_slot) if time_slot else None,
                report_type=str(report_type) if report_type else None,
                exclude_history_ids=list(exclude_history_ids or []),
                halflife_days=int(getattr(cfg, "ltm_halflife_days", 60)),
            )
            query.validate()

            # ③ 检索（store.search 内部已 fail-open）
            result = store.search(query)
            result.stock_code = str(stock_code or "")
            result.scope = query.scope
            return result
        except Exception as exc:  # noqa: BLE001 —— fail-open 最后一道闸
            logger.warning("%s 语义召回异常，降级为空结果: %s", LOG_PREFIX, exc)
            return RecallResult.empty(
                "store_error",
                enabled=True,
                stock_code=str(stock_code or ""),
                scope=str(scope or getattr(self._config, "ltm_scope", "same_stock")),
            )

    # 别名：兼容设计 §3.2.4 的 recall_similar 调用与 monkeypatch 测试
    recall = recall_for_stock

    # ------------------------------------------------------------------ #
    # 写入（零网络暂存 + run 末尾批量落盘）
    # ------------------------------------------------------------------ #

    def remember(
        self,
        *,
        history_id: int,
        code: str,
        name: str = "",
        report_type: str = "daily",
        trade_date: Any = "",
        time_slot: Any = None,
        trend: str = "",
        advice: str = "",
        summary: str = "",
        sentiment_score: Any = None,
        operation_advice: str = "",
    ) -> Optional[MemoryRecord]:
        """把一条已完成的结论零网络暂存进写缓冲（失败返回 ``None``）。"""
        try:
            if not self.enabled:
                return None
            writer = self._ensure_writer()
            return writer.stage(
                history_id=history_id,
                code=code,
                name=name,
                report_type=report_type,
                trade_date=trade_date,
                time_slot=time_slot,
                trend=trend,
                advice=advice,
                summary=summary,
                sentiment_score=sentiment_score,
                operation_advice=operation_advice,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 记忆暂存失败（已跳过）: %s", LOG_PREFIX, exc)
            return None

    def flush(self) -> Dict[str, Any]:
        """本 run 末尾批量 embed + 幂等落盘（永不抛异常）。"""
        try:
            if not self.enabled:
                return {
                    "pending": 0,
                    "embedded": 0,
                    "written": 0,
                    "skipped": 0,
                    "degraded": False,
                    "reason": "disabled",
                    "elapsed_ms": 0.0,
                }
            writer = self._ensure_writer()
            return writer.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 记忆落库失败（本批丢弃）: %s", LOG_PREFIX, exc)
            return {
                "pending": 0,
                "embedded": 0,
                "written": 0,
                "skipped": 0,
                "degraded": True,
                "reason": "store_error",
                "elapsed_ms": 0.0,
            }


__all__ = [
    "LongTermMemory",
]
