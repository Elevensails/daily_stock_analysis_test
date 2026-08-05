# -*- coding: utf-8 -*-
"""
===================================
U12 语义缓存 — 存储与检索层（分区强过滤）
===================================

本文件是 U12 **风险最高**的模块：铁律 #1「严禁跨时槽 / 跨交易日命中」的
第 2 层（SQL 等值强过滤）与第 3 层（Python 防御性断言）都落在这里。

三层防御（设计 §5.1）：

    第 1 层  写入侧：partition_key = sha256(七个分区维度 | 命名空间 | 版本)
             —— 见 src/cache/models.py::CacheKey.partition_key()
    第 2 层  查询侧：WHERE partition_key=? AND code=? AND trade_date=?
                          AND time_slot=? AND report_type=? AND llm_model=?
                          AND params_fp=? AND model_id=? AND vector_version=?
                          AND (expires_at IS NULL OR expires_at > now)
             —— 见 _base_partition_filter()，**所有查询路径必须经过它**
    第 3 层  返回前：逐字段复核 row.time_slot == key.time_slot ...
             —— 见 _assert_partition()，不等即丢弃 + logger.error 留痕

物理隔离（铁律 #4）：本模块只读写 ``llm_semantic_cache`` 表，
**不 import** ``SqliteVectorStore`` / ``build_vector_store``，
仅复用 ``encode_vector`` / ``decode_vector`` 两个纯函数。

降级铁律：本层所有公开方法**永不抛异常**，DB 故障一律返回
``None`` / ``False`` / ``0``，由上层等价处理为"未命中"。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from src.cache.models import (
    ASSERT_FIELDS,
    CACHE_TIER_SEMANTIC,
    LOG_PREFIX,
    SEMCACHE_MODEL_ID,
    SEMCACHE_VECTOR_VERSION,
    CacheEntry,
    CacheKey,
    PartitionCandidates,
)

# 仅复用两个纯函数（设计 §6.2 证据 4 的静态依赖白名单）
from src.memory.vector_store import decode_vector, encode_vector

logger = logging.getLogger(__name__)

#: 单分区候选上限的兜底默认值（分区本就极小，见设计 §7.2 理由三）
DEFAULT_MAX_CANDIDATES: int = 64

#: usage_json 落盘上限，防止异常大的 usage 撑爆行
_MAX_USAGE_JSON_BYTES: int = 8192


class SqliteSemanticCacheStore:
    """``llm_semantic_cache`` 表的轻量读写层。

    与 U14 的 ``SqliteVectorStore`` 的关键差异：
    - **无候选矩阵缓存**（U14 的 ``_MatrixCache`` 键不含 ``time_slot``，是 F5
      记录的分区盲区；U12 分区本就只有个位数行，缓存收益为负、风险为正）
    - **等值过滤而非范围过滤**（U14 是 ``trade_date >= cutoff``，U12 是 ``==``）
    """

    def __init__(
        self,
        model_id: str = SEMCACHE_MODEL_ID,
        *,
        db_manager: Any = None,
        vector_version: int = SEMCACHE_VECTOR_VERSION,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        store_prompt_text: bool = True,
    ) -> None:
        self.model_id = str(model_id or SEMCACHE_MODEL_ID)
        try:
            self.vector_version = int(vector_version)
        except (TypeError, ValueError):
            self.vector_version = SEMCACHE_VECTOR_VERSION
        try:
            self.max_candidates = max(int(max_candidates), 1)
        except (TypeError, ValueError):
            self.max_candidates = DEFAULT_MAX_CANDIDATES
        self.store_prompt_text = bool(store_prompt_text)
        self._db_manager = db_manager

    # ------------------------------------------------------------------ #
    # 惰性依赖
    # ------------------------------------------------------------------ #

    def _get_db(self) -> Any:
        """惰性拿到 DatabaseManager（避免 import 期即建连接 / 触发建表）。"""
        if self._db_manager is None:
            from src.storage import DatabaseManager  # noqa: PLC0415 —— 惰性导入破环

            self._db_manager = DatabaseManager.get_instance()
        return self._db_manager

    @staticmethod
    def _model() -> Any:
        """惰性拿到 ORM 模型类。"""
        from src.storage import LlmSemanticCache  # noqa: PLC0415

        return LlmSemanticCache

    # ------------------------------------------------------------------ #
    # 第 2 层防御：公共强过滤
    # ------------------------------------------------------------------ #

    def _base_partition_filter(self, query: Any, key: CacheKey, now: datetime) -> Any:
        """所有查询路径的公共强过滤 —— **任何情况下都不得省略任何一条**。

        ``partition_key`` 已是九元组的 sha256 摘要，后面的等值列在数学上是冗余的；
        但它们兜的不是"哈希碰撞"，而是"**我们自己拼 key 的代码写错了**"这一
        现实得多的风险（少拼一个字段 / 字段值为空串 ⇒ 不同语义映射到同一 key）。
        代价只是索引里多几列，铁律级约束值得。
        """
        from sqlalchemy import or_  # noqa: PLC0415

        model = self._model()
        return query.filter(
            model.partition_key == key.partition_key(),
            model.code == key.code,                          # 铁律#3：跨标的不命中
            model.trade_date == key.trade_date,              # 铁律#1：跨交易日不命中
            model.time_slot == key.time_slot,                # 铁律#1：跨时槽不命中
            model.report_type == key.report_type,            # 铁律#3：跨请求类型不命中
            model.llm_model == key.llm_model,                # 跨模型不命中
            model.params_fp == key.params_fingerprint,       # 跨生成参数不命中
            model.model_id == self.model_id,                 # 铁律#4：命名空间隔离
            model.vector_version == self.vector_version,
            or_(model.expires_at.is_(None), model.expires_at > now),
        )

    # ------------------------------------------------------------------ #
    # 第 3 层防御：结果复核
    # ------------------------------------------------------------------ #

    def _assert_partition(self, row: Any, key: CacheKey) -> bool:
        """防御性断言：命中行的分区维度必须与请求逐字段相等。

        这是"不可能发生"的分支 —— 一旦发生说明 partition_key 构造或 SQL 过滤
        有严重 bug，必须 **ERROR 级留痕**而非静默。

        Args:
            row: ORM 行对象或等价的 ``dict``。
            key: 本次请求的分区键。

        Returns:
            ``True`` 通过复核；``False`` 需丢弃该行。
        """
        getter = row.get if isinstance(row, dict) else (lambda name, _r=row: getattr(_r, name, None))
        for name in ASSERT_FIELDS:
            row_value = str(getter(name) or "")
            key_value = str(getattr(key, name, "") or "")
            if row_value != key_value:
                logger.error(
                    "%s 分区越界拦截！field=%s row=%r key=%r —— 已丢弃该命中，"
                    "这不应发生，请排查 partition_key 构造逻辑",
                    LOG_PREFIX,
                    name,
                    row_value,
                    key_value,
                )
                return False
        return True

    # ------------------------------------------------------------------ #
    # Tier-0：精确命中
    # ------------------------------------------------------------------ #

    def lookup_exact(self, key: CacheKey, now: Optional[datetime] = None) -> Optional[CacheEntry]:
        """Tier-0：同分区内 ``prompt_hash`` 精确命中。

        Args:
            key: 分区键（必须已 ``validate()`` 且 ``is_complete()``）。
            now: 过期判定基准时刻，缺省 ``datetime.now()``。

        Returns:
            命中的 :class:`CacheEntry`；未命中 / 分区越界 / DB 异常时返回 ``None``。
            **本方法永不抛异常。**
        """
        if key is None or not key.is_complete() or not key.prompt_hash:
            return None
        reference = now or datetime.now()
        model = self._model()
        try:
            with self._get_db().session_scope() as session:
                query = self._base_partition_filter(session.query(model), key, reference)
                row = query.filter(model.prompt_hash == key.prompt_hash).first()
                if row is None:
                    return None
                if not self._assert_partition(row, key):
                    return None
                entry = self._to_entry(row, similarity=1.0)
        except Exception as exc:
            logger.warning("%s lookup_exact 失败，降级为未命中: %s", LOG_PREFIX, exc)
            return None

        self.touch_hit(entry.row_id, now=reference)
        entry.hit_count += 1
        return entry

    def get_same_partition(
        self,
        key: CacheKey,
        prompt_hash: Optional[str] = None,
        time_slot: Optional[str] = None,
        trade_date: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[CacheEntry]:
        """按 ``(time_slot, trade_date)`` 等值过滤的同分区精确查询。

        这是 :meth:`lookup_exact` 的显式契约版本（任务书 T02 指定签名）：
        允许调用方**显式覆写** ``prompt_hash`` / ``time_slot`` / ``trade_date``，
        覆写后会重新计算 ``partition_key``，因此依然满足三层防御 ——
        不存在"用 A 时槽的 partition_key 去查 B 时槽"的可能。

        Args:
            key: 基准分区键。
            prompt_hash: 覆写内容指纹；``None`` 表示沿用 ``key.prompt_hash``。
            time_slot: 覆写时槽；``None`` 表示沿用 ``key.time_slot``。
            trade_date: 覆写交易日；``None`` 表示沿用 ``key.trade_date``。
            now: 过期判定基准时刻。

        Returns:
            命中的 :class:`CacheEntry`，否则 ``None``。
        """
        if key is None:
            return None
        effective = CacheKey(
            code=key.code,
            trade_date=key.trade_date if trade_date is None else trade_date,
            time_slot=key.time_slot if time_slot is None else time_slot,
            report_type=key.report_type,
            llm_model=key.llm_model,
            backend_id=key.backend_id,
            params_fingerprint=key.params_fingerprint,
            prompt_hash=key.prompt_hash if prompt_hash is None else prompt_hash,
        )
        effective.validate()
        if not effective.is_complete():
            return None
        return self.lookup_exact(effective, now=now)

    # ------------------------------------------------------------------ #
    # Tier-1：同分区语义检索（本版由门面层关闭，存储层能力完整保留）
    # ------------------------------------------------------------------ #

    def load_partition(
        self,
        key: CacheKey,
        *,
        now: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> PartitionCandidates:
        """加载同分区候选矩阵（Tier-1 输入）。

        Returns:
            :class:`PartitionCandidates`；异常/空分区时返回空对象（不抛异常）。
        """
        empty = PartitionCandidates(
            matrix=None,
            meta=[],
            partition_key=key.partition_key() if (key is not None and key.is_complete()) else "",
            total_rows=0,
        )
        if key is None or not key.is_complete():
            return empty
        reference = now or datetime.now()
        cap = self.max_candidates if limit is None else max(int(limit), 1)
        model = self._model()
        try:
            with self._get_db().session_scope() as session:
                query = self._base_partition_filter(session.query(model), key, reference)
                rows = query.filter(model.embedding.isnot(None)).limit(cap).all()
                entries = [self._to_entry(row) for row in rows]
        except Exception as exc:
            logger.warning("%s load_partition 失败，降级为空分区: %s", LOG_PREFIX, exc)
            return empty

        vectors: List[np.ndarray] = []
        meta: List[Dict[str, Any]] = []
        expected_dim = 0
        for entry in entries:
            vector = entry.embedding
            if vector is None or vector.size == 0:
                continue
            if expected_dim == 0:
                expected_dim = int(vector.size)
            if int(vector.size) != expected_dim:
                # 维度不符 ⇒ 丢弃该行，绝不混算（设计 §5.2）
                logger.debug(
                    "%s load_partition 丢弃维度不符的候选: got=%d expected=%d",
                    LOG_PREFIX, int(vector.size), expected_dim,
                )
                continue
            vectors.append(vector)
            meta.append({"entry": entry})

        if not vectors:
            return empty
        matrix = np.vstack(vectors).astype(np.float32, copy=False)
        return PartitionCandidates(
            matrix=matrix,
            meta=meta,
            partition_key=key.partition_key(),
            total_rows=len(meta),
        )

    def search_semantic(
        self,
        key: CacheKey,
        query_vec: Any,
        min_similarity: float,
        now: Optional[datetime] = None,
    ) -> Optional[CacheEntry]:
        """Tier-1：同分区内点积检索。

        候选集天然极小（同标的 / 同交易日 / 同时槽 / 同报告类型 / 同模型 / 同参数），
        暴力点积 < 1ms，无需任何 ANN 索引。

        向量落盘前已 L2 归一化 ⇒ **点积即余弦**（对齐 U14 F9 口径，不重复归一化）。

        Returns:
            得分 ``>= min_similarity`` 且通过第 3 层防御的 :class:`CacheEntry`；
            否则 ``None``。**永不抛异常。**
        """
        if key is None or not key.is_complete():
            return None
        reference = now or datetime.now()
        try:
            vector = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        except Exception as exc:
            logger.warning("%s search_semantic 查询向量非法，降级: %s", LOG_PREFIX, exc)
            return None
        if vector.size == 0:
            return None

        candidates = self.load_partition(key, now=reference)
        if candidates.is_empty() or candidates.matrix is None:
            return None
        if int(candidates.matrix.shape[1]) != int(vector.size):
            # 维度不符 ⇒ 降级，绝不混算
            logger.warning(
                "%s search_semantic 维度不符，降级为未命中: matrix_dim=%d query_dim=%d",
                LOG_PREFIX, int(candidates.matrix.shape[1]), int(vector.size),
            )
            return None

        try:
            scores = candidates.matrix @ vector
            best = int(np.argmax(scores))
            best_score = float(scores[best])
        except Exception as exc:
            logger.warning("%s search_semantic 打分失败，降级: %s", LOG_PREFIX, exc)
            return None

        try:
            threshold = float(min_similarity)
        except (TypeError, ValueError):
            return None
        if best_score < threshold:
            return None

        entry = candidates.meta[best].get("entry")
        if not isinstance(entry, CacheEntry):
            return None
        if not self._assert_partition(entry.__dict__, key):
            return None
        entry.similarity = best_score
        self.touch_hit(entry.row_id, now=reference)
        entry.hit_count += 1
        return entry

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def upsert(self, entry: CacheEntry) -> bool:
        """幂等写入一条缓存记录。

        冲突键 ``(partition_key, prompt_hash, model_id, vector_version)``
        与 §3.3 的 ``uq_semcache_partition_prompt`` 一致，重复写入不产生重复行。

        Returns:
            是否真正落盘；非法记录 / DB 异常时返回 ``False``（**不抛异常**）。
        """
        if entry is None or not entry.is_valid():
            return False
        try:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: PLC0415

            model = self._model()
            row = self._to_row(entry)
            with self._get_db().session_scope() as session:
                statement = sqlite_insert(model).values(**row)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "partition_key",
                        "prompt_hash",
                        "model_id",
                        "vector_version",
                    ],
                    set_={
                        "code": statement.excluded.code,
                        "trade_date": statement.excluded.trade_date,
                        "time_slot": statement.excluded.time_slot,
                        "report_type": statement.excluded.report_type,
                        "llm_model": statement.excluded.llm_model,
                        "backend_id": statement.excluded.backend_id,
                        "params_fp": statement.excluded.params_fp,
                        "prompt_text": statement.excluded.prompt_text,
                        "response_text": statement.excluded.response_text,
                        "response_model": statement.excluded.response_model,
                        "usage_json": statement.excluded.usage_json,
                        "embedding": statement.excluded.embedding,
                        "dim": statement.excluded.dim,
                        "created_at": statement.excluded.created_at,
                        "expires_at": statement.excluded.expires_at,
                    },
                )
                session.execute(statement)
            return True
        except Exception as exc:
            logger.warning("%s upsert 失败，缓存未写入（不影响主链路）: %s", LOG_PREFIX, exc)
            return False

    def put(self, entry: CacheEntry) -> bool:
        """:meth:`upsert` 的别名（任务书 T02 指定的对外名字）。"""
        return self.upsert(entry)

    def touch_hit(self, row_id: int, *, now: Optional[datetime] = None) -> None:
        """命中计数 +1 并刷新 ``last_hit_at``。失败静默（纯统计字段）。"""
        try:
            identifier = int(row_id or 0)
        except (TypeError, ValueError):
            return
        if identifier <= 0:
            return
        reference = now or datetime.now()
        try:
            model = self._model()
            with self._get_db().session_scope() as session:
                session.query(model).filter(model.id == identifier).update(
                    {
                        model.hit_count: (model.hit_count + 1),
                        model.last_hit_at: reference,
                    },
                    synchronize_session=False,
                )
        except Exception as exc:
            logger.debug("%s touch_hit 失败（仅统计字段，忽略）: %s", LOG_PREFIX, exc)

    # ------------------------------------------------------------------ #
    # 运维
    # ------------------------------------------------------------------ #

    def purge_expired(self, now: Optional[datetime] = None) -> int:
        """清理已过期行（只清本表，绝不触碰 ``analysis_memory_vector``）。

        Returns:
            删除行数；异常时返回 ``0``。
        """
        reference = now or datetime.now()
        try:
            model = self._model()
            with self._get_db().session_scope() as session:
                deleted = (
                    session.query(model)
                    .filter(model.expires_at.isnot(None), model.expires_at <= reference)
                    .delete(synchronize_session=False)
                )
            return int(deleted or 0)
        except Exception as exc:
            logger.warning("%s purge_expired 失败: %s", LOG_PREFIX, exc)
            return 0

    def purge_all(self) -> int:
        """清空本命名空间下的所有缓存行（``--purge-all`` 用）。

        只删 ``model_id == self.model_id`` 的行，命名空间外的数据不受影响。
        """
        try:
            model = self._model()
            with self._get_db().session_scope() as session:
                deleted = (
                    session.query(model)
                    .filter(model.model_id == self.model_id)
                    .delete(synchronize_session=False)
                )
            return int(deleted or 0)
        except Exception as exc:
            logger.warning("%s purge_all 失败: %s", LOG_PREFIX, exc)
            return 0

    def count(self, key: Optional[CacheKey] = None, *, now: Optional[datetime] = None) -> int:
        """统计行数。

        Args:
            key: 给定时统计**该分区**的有效行数（走完整强过滤）；
                ``None`` 时统计本命名空间全部行。

        Returns:
            行数；异常时返回 ``0``。
        """
        try:
            model = self._model()
            with self._get_db().session_scope() as session:
                query = session.query(model)
                if key is not None and key.is_complete():
                    query = self._base_partition_filter(query, key, now or datetime.now())
                else:
                    query = query.filter(
                        model.model_id == self.model_id,
                        model.vector_version == self.vector_version,
                    )
                return int(query.count())
        except Exception as exc:
            logger.warning("%s count 失败: %s", LOG_PREFIX, exc)
            return 0

    def stats(self) -> Dict[str, Any]:
        """表级统计（``scripts/semcache_admin.py --stats`` 用）。"""
        result: Dict[str, Any] = {
            "model_id": self.model_id,
            "vector_version": self.vector_version,
            "rows": 0,
            "partitions": 0,
            "total_hits": 0,
            "expired_rows": 0,
            "response_chars": 0,
        }
        try:
            from sqlalchemy import distinct, func  # noqa: PLC0415

            model = self._model()
            now = datetime.now()
            with self._get_db().session_scope() as session:
                base = session.query(model).filter(
                    model.model_id == self.model_id,
                    model.vector_version == self.vector_version,
                )
                result["rows"] = int(base.count())
                result["partitions"] = int(
                    session.query(func.count(distinct(model.partition_key)))
                    .filter(
                        model.model_id == self.model_id,
                        model.vector_version == self.vector_version,
                    )
                    .scalar()
                    or 0
                )
                result["total_hits"] = int(
                    session.query(func.coalesce(func.sum(model.hit_count), 0))
                    .filter(
                        model.model_id == self.model_id,
                        model.vector_version == self.vector_version,
                    )
                    .scalar()
                    or 0
                )
                result["expired_rows"] = int(
                    base.filter(model.expires_at.isnot(None), model.expires_at <= now).count()
                )
                result["response_chars"] = int(
                    session.query(func.coalesce(func.sum(func.length(model.response_text)), 0))
                    .filter(
                        model.model_id == self.model_id,
                        model.vector_version == self.vector_version,
                    )
                    .scalar()
                    or 0
                )
        except Exception as exc:
            logger.warning("%s stats 失败: %s", LOG_PREFIX, exc)
        return result

    def inspect_partition(self, partition_key: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按 ``partition_key`` 罗列行摘要（``--inspect`` 用，不含 prompt 原文）。"""
        rows: List[Dict[str, Any]] = []
        target = str(partition_key or "").strip()
        if not target:
            return rows
        try:
            model = self._model()
            with self._get_db().session_scope() as session:
                records = (
                    session.query(model)
                    .filter(
                        model.partition_key == target,
                        model.model_id == self.model_id,
                        model.vector_version == self.vector_version,
                    )
                    .limit(max(int(limit), 1))
                    .all()
                )
                for record in records:
                    rows.append(
                        {
                            "id": int(record.id or 0),
                            "code": record.code,
                            "trade_date": record.trade_date,
                            "time_slot": record.time_slot,
                            "report_type": record.report_type,
                            "llm_model": record.llm_model,
                            "prompt_hash": record.prompt_hash,
                            "response_chars": len(record.response_text or ""),
                            "hit_count": int(record.hit_count or 0),
                            "created_at": record.created_at.isoformat() if record.created_at else None,
                            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
                        }
                    )
        except Exception as exc:
            logger.warning("%s inspect_partition 失败: %s", LOG_PREFIX, exc)
        return rows

    # ------------------------------------------------------------------ #
    # 行 <-> 实体 转换
    # ------------------------------------------------------------------ #

    def _to_entry(self, row: Any, *, similarity: float = 1.0) -> CacheEntry:
        """ORM 行 → :class:`CacheEntry`（**必须在 session 内调用**）。"""
        dim = int(getattr(row, "dim", 0) or 0)
        embedding = decode_vector(getattr(row, "embedding", None), dim=dim)
        return CacheEntry(
            partition_key=str(getattr(row, "partition_key", "") or ""),
            prompt_hash=str(getattr(row, "prompt_hash", "") or ""),
            code=str(getattr(row, "code", "") or ""),
            trade_date=str(getattr(row, "trade_date", "") or ""),
            time_slot=str(getattr(row, "time_slot", "") or ""),
            report_type=str(getattr(row, "report_type", "") or ""),
            llm_model=str(getattr(row, "llm_model", "") or ""),
            backend_id=str(getattr(row, "backend_id", "") or ""),
            params_fingerprint=str(getattr(row, "params_fp", "") or ""),
            prompt_text=str(getattr(row, "prompt_text", "") or ""),
            response_text=str(getattr(row, "response_text", "") or ""),
            response_model=str(getattr(row, "response_model", "") or ""),
            original_usage=_loads_usage(getattr(row, "usage_json", None)),
            embedding=embedding,
            dim=dim,
            model_id=str(getattr(row, "model_id", "") or SEMCACHE_MODEL_ID),
            vector_version=int(getattr(row, "vector_version", 0) or SEMCACHE_VECTOR_VERSION),
            created_at=getattr(row, "created_at", None),
            expires_at=getattr(row, "expires_at", None),
            hit_count=int(getattr(row, "hit_count", 0) or 0),
            last_hit_at=getattr(row, "last_hit_at", None),
            row_id=int(getattr(row, "id", 0) or 0),
            similarity=float(similarity),
        )

    def _to_row(self, entry: CacheEntry) -> Dict[str, Any]:
        """:class:`CacheEntry` → INSERT 值字典。"""
        embedding_blob = None
        dim = 0
        if entry.embedding is not None:
            try:
                vector = np.asarray(entry.embedding, dtype=np.float32).reshape(-1)
                if vector.size > 0:
                    embedding_blob = encode_vector(vector)
                    dim = int(vector.size)
            except Exception as exc:
                logger.debug("%s 向量编码失败，本行只落文本: %s", LOG_PREFIX, exc)
                embedding_blob = None
                dim = 0
        return {
            "partition_key": entry.partition_key,
            "code": entry.code,
            "trade_date": entry.trade_date,
            "time_slot": entry.time_slot,
            "report_type": entry.report_type,
            "llm_model": entry.llm_model,
            "backend_id": entry.backend_id,
            "params_fp": entry.params_fingerprint,
            "prompt_hash": entry.prompt_hash,
            "prompt_text": (entry.prompt_text or "") if self.store_prompt_text else None,
            "response_text": entry.response_text,
            "response_model": entry.response_model or "",
            "usage_json": _dumps_usage(entry.original_usage),
            "embedding": embedding_blob,
            "dim": dim,
            "model_id": self.model_id,
            "vector_version": self.vector_version,
            "created_at": entry.created_at or datetime.now(),
            "expires_at": entry.expires_at,
            "hit_count": 0,
        }


# --------------------------------------------------------------------------- #
# usage_json 编解码（纯函数）
# --------------------------------------------------------------------------- #

def _dumps_usage(usage: Any) -> Optional[str]:
    """把 usage 字典序列化为 JSON 文本；失败或超限时返回 ``None``。"""
    if not usage:
        return None
    try:
        payload = json.dumps(usage, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return None
    if len(payload.encode("utf-8")) > _MAX_USAGE_JSON_BYTES:
        return None
    return payload


def _loads_usage(payload: Any) -> Dict[str, Any]:
    """把 ``usage_json`` 还原为字典；失败时返回空字典。"""
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "SqliteSemanticCacheStore",
    "DEFAULT_MAX_CANDIDATES",
]
