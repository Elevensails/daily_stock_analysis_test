# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆 — 结论抽取与批量写入
===================================

职责：
1. :meth:`MemoryWriter.build_conclusion_text` —— PRD Q5 三字段模板抽取
   （``[趋势] / [建议] / [要点]``），并**剥离股票名称与代码 token**（设计 §0 A8）
2. :meth:`MemoryWriter.stage` —— **零网络**地把一条结论压入缓冲区
3. :meth:`MemoryWriter.flush` —— 批量 embed 一次 + 幂等落盘一次（PRD Q6 ``batch_end`` 模式）

为什么"抽取"与"向量化"必须分离（设计 §3.4）：
    单只股票分析完就 embed 一次，会把 N 次网络往返摊进主流程关键路径；
    ``stage()`` 只做纯字符串处理（微秒级），全批跑完后 ``flush()`` 一次性
    批量 embed + 单事务 UPSERT，把网络与事务开销从 O(N) 压到 O(1)。

失败语义（设计 §8.9）：
    ``flush()`` **永不抛异常**。embed 失败自动退化为"只落文本"，
    向量留给 ``scripts/backfill_ltm.py`` 离线补；落库失败仅告警并丢弃本批缓冲，
    绝不阻断上游分析流程。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from src.memory.models import (
    CONCLUSION_LABEL_ADVICE,
    CONCLUSION_LABEL_SUMMARY,
    CONCLUSION_LABEL_TREND,
    LOG_PREFIX,
    VECTOR_VERSION,
    WRITE_MODE_OFF,
    WRITE_MODE_TEXT_ONLY,
    MemoryRecord,
    normalize_time_slot,
    normalize_trade_date,
)

logger = logging.getLogger(__name__)

#: 数据库列长度上限（与 src/storage.py::AnalysisMemoryVector 一致，写入前先裁剪）
_MAX_CODE_LEN = 10
_MAX_NAME_LEN = 50
_MAX_REPORT_TYPE_LEN = 16
_MAX_ADVICE_LEN = 20

#: 市场前缀（剥离代码 token 时用于还原"裸代码"）
_MARKET_PREFIX_RE = re.compile(r"^(sh|sz|bj|hk|us)", re.IGNORECASE)
#: 交易所后缀，如 600519.SH
_MARKET_SUFFIX_RE = re.compile(r"\.(sh|sz|bj|hk)$", re.IGNORECASE)
#: 连续空白
_WS_RE = re.compile(r"[\s\u3000]+")
#: 剥离后遗留的孤立标点（如"（）"、"【】"、连续顿号）
_ORPHAN_PUNCT_RE = re.compile(r"[（(【\[]\s*[)）】\]]|[、，,]{2,}")


def _truncate(text: str, limit: int) -> str:
    """硬截断（复刻 src/services/stock_continuity.py::_truncate 的省略号口径）。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _collapse(text: str) -> str:
    """折叠空白并清理剥离后遗留的孤立标点。"""
    cleaned = _ORPHAN_PUNCT_RE.sub("", str(text or ""))
    return _WS_RE.sub(" ", cleaned).strip(" ，,、;；")


class MemoryWriter:
    """结论抽取 + 批量向量化 + 幂等落盘。

    Args:
        config: ``Config`` 实例（或任意具备同名属性的对象）。
        provider: 显式注入的 :class:`~src.memory.embedding_provider.EmbeddingProvider`；
            ``None`` 时由工厂按 config 构造（构造过程零网络）。
        store: 显式注入的 :class:`~src.memory.vector_store.SqliteVectorStore`；
            ``None`` 时按 ``provider.model_id`` 自动组装。
        db_manager: 传给自动组装的 store，便于测试注入临时库。

    Notes:
        本类**不是**线程安全边界之外的共享对象，但内部缓冲区加了锁，
        允许多线程分析流程并发 ``stage()``。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        provider: Any = None,
        store: Any = None,
        db_manager: Any = None,
    ) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._buffer: List[MemoryRecord] = []

        self.write_mode = str(
            getattr(config, "ltm_write_mode", "batch_end") or "batch_end"
        ).strip().lower()
        self.conclusion_max_chars = max(
            int(getattr(config, "ltm_conclusion_max_chars", 500) or 500), 32
        )

        self.provider = provider if provider is not None else self._build_provider(config)
        self.store = store if store is not None else self._build_store(config, db_manager)

    # ------------------------------------------------------------------ #
    # 组装
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_provider(config: Any) -> Any:
        """构造 embedding provider；显式 litellm 不可用时兜底 local（写入侧不该因此瘫痪）。"""
        from src.memory.embedding_provider import (  # noqa: PLC0415 —— 惰性导入破环
            build_embedding_provider,
            build_local_provider,
        )
        try:
            provider, degraded, reason = build_embedding_provider(config)
            if degraded:
                logger.info("%s 写入侧 embedding 降级: reason=%s", LOG_PREFIX, reason)
            return provider
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s embedding provider 构造失败，回落本地词法: %s", LOG_PREFIX, exc)
            return build_local_provider(config)

    def _build_store(self, config: Any, db_manager: Any) -> Any:
        from src.memory.vector_store import build_vector_store  # noqa: PLC0415
        return build_vector_store(config, self.provider.model_id, db_manager=db_manager)

    # ------------------------------------------------------------------ #
    # 开关
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        """写入是否生效：总开关 + 写开关 + 写模式，三者同时成立才写。"""
        if not bool(getattr(self.config, "ltm_enabled", False)):
            return False
        if not bool(getattr(self.config, "ltm_write_enabled", True)):
            return False
        return self.write_mode != WRITE_MODE_OFF

    @property
    def text_only(self) -> bool:
        """是否只落文本、不算向量（CI 与断网环境的默认姿势）。"""
        return self.write_mode == WRITE_MODE_TEXT_ONLY

    @property
    def pending_count(self) -> int:
        """当前缓冲区待写条数。"""
        with self._lock:
            return len(self._buffer)

    # ------------------------------------------------------------------ #
    # 结论抽取（PRD Q5 三字段模板 + 设计 §0 A8 身份 token 剥离）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _identity_tokens(code: str = "", name: str = "") -> List[str]:
        """收集需要从结论文本中剥离的"股票身份" token。

        为什么要剥离（设计 §0 A8）：
            结论文本里高频出现"贵州茅台/600519"这类身份词。若保留，同一只股票的
            任意两条结论都会因共享身份 token 而获得虚高相似度（本地词法向量尤其明显），
            ``scope=global`` 下更会退化成"按名字聚类"。剥离后相似度才真正反映**情境**。
        """
        tokens: set = set()

        raw_code = str(code or "").strip()
        if raw_code:
            tokens.add(raw_code)
            bare = _MARKET_SUFFIX_RE.sub("", raw_code)
            bare = _MARKET_PREFIX_RE.sub("", bare).strip()
            if len(bare) >= 4:
                tokens.add(bare)
                for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
                    tokens.add(f"{prefix}{bare}")
                for suffix in (".SH", ".SZ", ".BJ", ".sh", ".sz", ".bj"):
                    tokens.add(f"{bare}{suffix}")

        raw_name = str(name or "").strip()
        if len(raw_name) >= 2:
            tokens.add(raw_name)
            # A 股常见的连字符/空格写法，如 "万 科A" / "万科-A"
            compact = _WS_RE.sub("", raw_name)
            if len(compact) >= 2:
                tokens.add(compact)

        return [token for token in tokens if token]

    @classmethod
    def strip_identity_tokens(cls, text: str, code: str = "", name: str = "") -> str:
        """从文本中剥离股票名称 / 代码 token 并清理残留标点。"""
        source = str(text or "")
        tokens = cls._identity_tokens(code, name)
        if not tokens:
            return _collapse(source)
        # 长 token 优先，避免 "600519" 先被 "60051" 之类的短串吃掉
        pattern = re.compile(
            "|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True)),
            re.IGNORECASE,
        )
        return _collapse(pattern.sub("", source))

    def build_conclusion_text(
        self,
        *,
        trend: str = "",
        advice: str = "",
        summary: str = "",
        code: str = "",
        name: str = "",
        max_chars: Optional[int] = None,
    ) -> str:
        """按 PRD Q5 三字段模板拼装被向量化的规范化文本。

        产出形如::

            [趋势] 缩量回踩 20 日线，量能未放大
            [建议] 观望
            [要点] 主线切换至有色，资金分歧加大

        规则：
        - 空字段整行省略（不留 ``[建议]`` 空标签，避免污染词法向量）
        - 每字段内部空白折叠为单空格，保证三行结构稳定
        - 先剥离身份 token，再按 ``max_chars`` 收敛；预算不足时**优先保住
          ``[趋势]`` 与 ``[建议]``**，压缩 ``[要点]``（结论 > 细节）

        Args:
            max_chars: 上限，缺省取 ``config.ltm_conclusion_max_chars``。

        Returns:
            规范化文本；三字段全空时返回空串（调用方据此跳过入库）。
        """
        limit = int(max_chars) if max_chars else self.conclusion_max_chars
        limit = max(limit, 32)

        trend_text = self.strip_identity_tokens(trend, code, name)
        advice_text = self.strip_identity_tokens(advice, code, name)
        summary_text = self.strip_identity_tokens(summary, code, name)

        lines: List[str] = []
        if trend_text:
            lines.append(f"{CONCLUSION_LABEL_TREND} {trend_text}")
        if advice_text:
            lines.append(f"{CONCLUSION_LABEL_ADVICE} {advice_text}")

        head = "\n".join(lines)
        if not summary_text:
            return _truncate(head, limit)

        prefix = f"{CONCLUSION_LABEL_SUMMARY} "
        separator = 1 if head else 0
        budget = limit - len(head) - separator - len(prefix)
        if budget <= 0:
            # 预算已被趋势/建议吃满：丢弃要点，保住更高价值的结论字段
            return _truncate(head, limit)

        lines.append(prefix + _truncate(summary_text, budget))
        return _truncate("\n".join(lines), limit)

    # ------------------------------------------------------------------ #
    # 缓冲（零网络）
    # ------------------------------------------------------------------ #

    def stage(
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
        conclusion_text: str = "",
        sentiment_score: Any = None,
        operation_advice: str = "",
    ) -> Optional[MemoryRecord]:
        """把一条分析结论压入写缓冲区。

        **本方法零网络、零数据库访问**，可以安全地放在单只股票分析的收尾处调用。

        Args:
            conclusion_text: 已拼好的规范化文本；为空时用
                ``trend`` / ``advice`` / ``summary`` 现场拼装。

        Returns:
            入队的 :class:`MemoryRecord`；被跳过（功能关闭 / 文本为空 /
            history_id 非法）时返回 ``None``。**永不抛异常。**
        """
        try:
            if not self.enabled:
                return None

            text = str(conclusion_text or "").strip()
            if not text:
                text = self.build_conclusion_text(
                    trend=trend, advice=advice, summary=summary, code=code, name=name,
                )
            else:
                text = _truncate(
                    self.strip_identity_tokens(text, code, name), self.conclusion_max_chars
                )
            if not text:
                return None

            record = MemoryRecord(
                history_id=int(history_id or 0),
                code=str(code or "")[:_MAX_CODE_LEN],
                name=str(name or "")[:_MAX_NAME_LEN],
                report_type=(str(report_type or "daily")[:_MAX_REPORT_TYPE_LEN] or "daily"),
                trade_date=normalize_trade_date(trade_date),
                time_slot=normalize_time_slot(time_slot),
                conclusion_text=text,
                sentiment_score=self._coerce_sentiment(sentiment_score),
                operation_advice=str(operation_advice or "")[:_MAX_ADVICE_LEN],
            )
            if not record.is_valid():
                return None

            with self._lock:
                self._buffer.append(record)
            return record
        except Exception as exc:  # noqa: BLE001 —— 写入侧任何异常都不得影响分析主流程
            logger.warning("%s stage 失败（已跳过该条）: %s", LOG_PREFIX, exc)
            return None

    def stage_many(self, records: Sequence[Dict[str, Any]]) -> int:
        """批量 :meth:`stage`；返回成功入队的条数。"""
        staged = 0
        for payload in list(records or []):
            if isinstance(payload, dict) and self.stage(**payload) is not None:
                staged += 1
        return staged

    @staticmethod
    def _coerce_sentiment(value: Any) -> Optional[int]:
        """情绪分归一化为 0–100 的整数；不可解析返回 ``None``。"""
        if value is None or value == "":
            return None
        try:
            score = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return min(max(score, 0), 100)

    def clear(self) -> int:
        """丢弃缓冲区，返回被丢弃的条数。"""
        with self._lock:
            dropped = len(self._buffer)
            self._buffer.clear()
        return dropped

    # ------------------------------------------------------------------ #
    # 落盘（批量 embed 一次 + 单事务 UPSERT）
    # ------------------------------------------------------------------ #

    def flush(self) -> Dict[str, Any]:
        """批量向量化并幂等落盘。**永不抛异常。**

        Returns:
            ``{"pending", "embedded", "written", "skipped", "degraded",
            "reason", "elapsed_ms"}``
        """
        started = time.perf_counter()
        stats: Dict[str, Any] = {
            "pending": 0,
            "embedded": 0,
            "written": 0,
            "skipped": 0,
            "degraded": False,
            "reason": "",
            "elapsed_ms": 0.0,
        }

        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        stats["pending"] = len(batch)

        if not batch:
            stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            return stats

        if not self.enabled:
            stats["skipped"] = len(batch)
            stats["reason"] = "disabled"
            stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            return stats

        if not self.text_only:
            self._embed_batch(batch, stats)
        else:
            stats["degraded"] = True
            stats["reason"] = "text_only"

        try:
            written = self.store.upsert_many(batch)
            stats["written"] = int(written.get("written", 0))
            stats["skipped"] += int(written.get("skipped", 0))
        except Exception as exc:  # noqa: BLE001 —— 落库失败仅告警
            stats["degraded"] = True
            stats["reason"] = stats["reason"] or "store_error"
            stats["skipped"] += len(batch)
            logger.warning("%s 记忆落库失败（本批丢弃）: %s", LOG_PREFIX, exc)

        stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info(
            "%s flush: pending=%d embedded=%d written=%d skipped=%d "
            "degraded=%s reason=%s elapsed=%.1fms model_id=%s vector_version=%d",
            LOG_PREFIX, stats["pending"], stats["embedded"], stats["written"],
            stats["skipped"], stats["degraded"], stats["reason"] or "-",
            stats["elapsed_ms"], self.provider.model_id, VECTOR_VERSION,
        )
        return stats

    def _embed_batch(self, batch: List[MemoryRecord], stats: Dict[str, Any]) -> None:
        """一次性 embed 整批文本；失败则整批退化为"只落文本"。"""
        texts = [record.conclusion_text for record in batch]
        try:
            matrix = self.provider.embed(texts)
        except Exception as exc:  # noqa: BLE001
            stats["degraded"] = True
            stats["reason"] = "embed_failed"
            logger.warning(
                "%s 批量 embedding 失败，退化为只落文本（向量交由 backfill 补）: %s",
                LOG_PREFIX, exc,
            )
            return

        array = np.asarray(matrix, dtype=np.float32)
        if array.ndim != 2 or array.shape[0] != len(batch):
            stats["degraded"] = True
            stats["reason"] = "embed_shape_mismatch"
            logger.warning(
                "%s embedding 形状 %s 与批次大小 %d 不匹配，退化为只落文本",
                LOG_PREFIX, getattr(array, "shape", None), len(batch),
            )
            return

        embedded = 0
        for index, record in enumerate(batch):
            vector = array[index]
            # 全零行 = provider 对空白文本的占位输出，落盘无意义，留给 backfill
            if not np.any(vector):
                continue
            record.embedding = vector
            embedded += 1
        stats["embedded"] = embedded


def build_memory_writer(
    config: Any,
    *,
    provider: Any = None,
    store: Any = None,
    db_manager: Any = None,
) -> MemoryWriter:
    """工厂函数（与 ``build_embedding_provider`` / ``build_vector_store`` 风格一致）。"""
    return MemoryWriter(config, provider=provider, store=store, db_manager=db_manager)


__all__ = [
    "MemoryWriter",
    "build_memory_writer",
]
