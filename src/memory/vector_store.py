# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆 — SQLite 向量存储与暴力检索
===================================

职责：
1. ``SqliteVectorStore.upsert_many()`` —— 幂等落盘（``ON CONFLICT ... DO UPDATE ... WHERE``）
2. ``SqliteVectorStore.load_candidates()`` —— 版本强过滤地拉取候选矩阵
3. ``SqliteVectorStore.search()`` —— 余弦 TopK + 时间衰减，产出 :class:`RecallResult`
4. ``_MatrixCache`` —— 进程内 LRU，避免同批多股票重复解码 BLOB

关键设计（对齐 docs/system_design_u14_long_term_memory.md §3.3 / §8）：

- **不引入任何 ANN 库**（faiss / hnswlib / chromadb 等）。requirements.txt 零改动是
  本次交付的铁律；万级候选下 ``numpy`` 暴力点积（O(n·d)）耗时在毫秒量级，完全够用。
- **落盘前已 L2 归一化** ⇒ 余弦相似度退化为点积 ``M @ q``，检索侧禁止重复归一化。
- **查询恒带 ``(model_id, vector_version)`` 双过滤**。跨模型向量的点积是无意义的噪声，
  宁可召回 0 条并上报 ``model_mismatch``，也绝不允许混算（设计 §8.2）。
- TopK 用 ``np.argpartition``（O(n)）而非全排序（O(n log n)）。
- 所有公开方法**只在 :meth:`search` 层 fail-open**；``upsert_many`` 的异常向上抛给
  :class:`~src.memory.writer.MemoryWriter` 统一吞掉，便于写入侧记账。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.memory.models import (
    LOG_PREFIX,
    PROVIDER_LOCAL,
    SCOPE_GLOBAL,
    SCOPE_SAME_STOCK,
    VECTOR_VERSION,
    CandidateSet,
    DegradeReason,
    MemoryRecord,
    RecallItem,
    RecallQuery,
    RecallResult,
    compute_age_days,
)

logger = logging.getLogger(__name__)

#: 单条 INSERT 语句最多携带的行数（13 列 × 100 行 = 1300 个绑定参数，远低于 SQLite 上限）
_UPSERT_CHUNK_SIZE = 100

#: 候选矩阵缓存的默认容量与存活时间
_CACHE_MAX_ENTRIES = 8
_CACHE_TTL_SECONDS = 300.0

#: 归一化容差：落盘向量的 L2 范数偏离超过该值时，检索侧兜底重新归一化
_NORM_TOLERANCE = 1e-3


def apply_time_decay(similarity: float, age_days: int, halflife_days: int) -> float:
    """半衰期时间衰减：``score = similarity * 0.5 ** (age_days / halflife_days)``。

    Args:
        similarity: 原始余弦相似度。
        age_days: 距今天数（下界 0）。
        halflife_days: 半衰期天数；``<= 0`` 表示**关闭衰减**（直接返回原值）。

    Returns:
        衰减后的排序分。
    """
    if halflife_days <= 0:
        return float(similarity)
    return float(similarity) * float(0.5 ** (max(int(age_days), 0) / float(halflife_days)))


def encode_vector(vector: Any) -> bytes:
    """把向量编码为可入库的 BLOB（``np.float32`` 小端连续内存）。"""
    array = np.ascontiguousarray(np.asarray(vector, dtype=np.float32).reshape(-1))
    return array.tobytes()


def decode_vector(blob: Any, dim: int = 0) -> Optional[np.ndarray]:
    """把 BLOB 还原为 ``np.float32`` 一维向量；无法还原时返回 ``None``。

    Args:
        blob: 数据库取出的 ``bytes`` / ``memoryview``。
        dim: 期望维度；``> 0`` 时长度不符即判为脏数据。
    """
    if blob is None:
        return None
    try:
        raw = bytes(blob)
    except (TypeError, ValueError):
        return None
    if not raw or len(raw) % 4 != 0:
        return None
    vector = np.frombuffer(raw, dtype=np.float32)
    if dim > 0 and vector.size != dim:
        return None
    if vector.size == 0:
        return None
    return vector


# --------------------------------------------------------------------------- #
# 进程内候选矩阵缓存
# --------------------------------------------------------------------------- #

@dataclass
class _CacheEntry:
    """一条缓存记录：候选集 + 写入时刻 + 写代次。"""

    candidates: CandidateSet
    created_at: float
    generation: int


class _MatrixCache:
    """线程安全的 LRU 缓存，键为「过滤条件签名」。

    失效策略是双保险：
    1. **写代次**（``generation``）—— 任何 upsert 都会 +1，使本进程内的旧快照立刻作废；
    2. **TTL** —— 兜住"别的进程写了库"这种本进程无法感知的情况。
    """

    def __init__(
        self,
        max_entries: int = _CACHE_MAX_ENTRIES,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self._max_entries = max(int(max_entries or 1), 1)
        self._ttl = float(ttl_seconds or 0.0)
        self._lock = threading.RLock()
        self._entries: "OrderedDict[Tuple, _CacheEntry]" = OrderedDict()

    def get(self, key: Tuple, generation: int) -> Optional[CandidateSet]:
        """取缓存；未命中 / 已过期 / 代次落后时返回 ``None``。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.generation != generation:
                self._entries.pop(key, None)
                return None
            if self._ttl > 0 and (time.time() - entry.created_at) > self._ttl:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.candidates

    def put(self, key: Tuple, candidates: CandidateSet, generation: int) -> None:
        """写入缓存并按 LRU 淘汰。"""
        with self._lock:
            self._entries[key] = _CacheEntry(
                candidates=candidates,
                created_at=time.time(),
                generation=generation,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# --------------------------------------------------------------------------- #
# 存储 + 检索
# --------------------------------------------------------------------------- #

class SqliteVectorStore:
    """基于既有 SQLite（``analysis_memory_vector`` 表）的向量存储与暴力检索。

    Args:
        model_id: 本实例负责读写的向量归属标识（如 ``local:lexical-v1``）。
            **读写两侧共用同一个值**，是版本隔离的唯一开关。
        db_manager: :class:`~src.storage.DatabaseManager` 实例；``None`` 时惰性取单例。
        vector_version: 向量逻辑版本，缺省取 :data:`~src.memory.models.VECTOR_VERSION`。
        max_candidates: 单次候选拉取的行数上限（超出即截断并置 ``truncated``）。
        cache_ttl_seconds: 候选矩阵缓存存活秒数；``0`` 表示禁用 TTL（仍受写代次约束）。
    """

    def __init__(
        self,
        model_id: str,
        *,
        db_manager: Any = None,
        vector_version: int = VECTOR_VERSION,
        max_candidates: int = 20000,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self.model_id = str(model_id or "").strip()
        self.vector_version = int(vector_version or VECTOR_VERSION)
        self.max_candidates = max(int(max_candidates or 0), 1)
        self._db_manager = db_manager
        self._cache = _MatrixCache(ttl_seconds=cache_ttl_seconds)
        self._generation = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    @property
    def provider_name(self) -> str:
        """从 ``model_id`` 推导 provider 短名（``litellm`` / ``local``）。"""
        return str(self.model_id or PROVIDER_LOCAL).split(":", 1)[0]

    def _get_db(self) -> Any:
        """惰性拿到 DatabaseManager（避免 import 期即建连接 / 触发建表）。"""
        if self._db_manager is None:
            from src.storage import DatabaseManager  # noqa: PLC0415 —— 惰性导入破环
            self._db_manager = DatabaseManager.get_instance()
        return self._db_manager

    @staticmethod
    def _model() -> Any:
        """惰性拿到 ORM 模型类。"""
        from src.storage import AnalysisMemoryVector  # noqa: PLC0415
        return AnalysisMemoryVector

    def invalidate_cache(self) -> None:
        """显式作废候选缓存（写入后自动调用，也可供测试/运维手动触发）。"""
        with self._lock:
            self._generation += 1
        self._cache.clear()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def upsert_many(self, records: Sequence[MemoryRecord]) -> Dict[str, int]:
        """幂等批量写入。

        冲突键为 ``(history_id, model_id, vector_version)``；仅当
        ``excluded.text_hash != analysis_memory_vector.text_hash`` 时才执行 UPDATE，
        因此**同内容重复写入是零写放大的 no-op**（设计 §3.3.2）。

        Args:
            records: 待写入记录；``is_valid()`` 为假的条目会被跳过。

        Returns:
            ``{"received", "written", "skipped"}`` 统计。

        Raises:
            Exception: 数据库异常向上抛出，由 :class:`MemoryWriter` 统一吞掉。
        """
        stats = {"received": len(records or []), "written": 0, "skipped": 0}
        rows = self._build_rows(records, stats)
        if not rows:
            return stats

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: PLC0415

        model = self._model()
        db = self._get_db()
        with db.session_scope() as session:
            for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
                chunk = rows[start:start + _UPSERT_CHUNK_SIZE]
                statement = sqlite_insert(model).values(chunk)
                statement = statement.on_conflict_do_update(
                    index_elements=["history_id", "model_id", "vector_version"],
                    set_={
                        "code": statement.excluded.code,
                        "name": statement.excluded.name,
                        "report_type": statement.excluded.report_type,
                        "trade_date": statement.excluded.trade_date,
                        "time_slot": statement.excluded.time_slot,
                        "conclusion_text": statement.excluded.conclusion_text,
                        "text_hash": statement.excluded.text_hash,
                        "embedding": statement.excluded.embedding,
                        "dim": statement.excluded.dim,
                        "sentiment_score": statement.excluded.sentiment_score,
                        "operation_advice": statement.excluded.operation_advice,
                        "created_at": statement.excluded.created_at,
                    },
                    # 内容未变则整行跳过：避免无谓写放大与 created_at 抖动
                    where=(statement.excluded.text_hash != model.text_hash),
                )
                session.execute(statement)
                stats["written"] += len(chunk)

        self.invalidate_cache()
        logger.debug(
            "%s upsert 完成: received=%d written=%d skipped=%d model_id=%s",
            LOG_PREFIX, stats["received"], stats["written"], stats["skipped"], self.model_id,
        )
        return stats

    def _build_rows(
        self,
        records: Sequence[MemoryRecord],
        stats: Dict[str, int],
    ) -> List[Dict[str, Any]]:
        """把 :class:`MemoryRecord` 列表转成可直接 INSERT 的字典列表（同键去重保留最后一条）。"""
        from datetime import datetime  # noqa: PLC0415 —— 与 storage 默认值口径一致

        deduped: "OrderedDict[int, Dict[str, Any]]" = OrderedDict()
        now = datetime.now()
        for record in list(records or []):
            if not isinstance(record, MemoryRecord) or not record.is_valid():
                stats["skipped"] += 1
                continue
            embedding = record.embedding
            blob: Optional[bytes] = None
            dim = 0
            if embedding is not None:
                vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
                if vector.size > 0:
                    blob = encode_vector(vector)
                    dim = int(vector.size)
            row: Dict[str, Any] = {
                "history_id": int(record.history_id),
                "code": record.code,
                "name": record.name,
                "report_type": record.report_type,
                "trade_date": record.trade_date,
                "time_slot": record.time_slot,
                "conclusion_text": record.conclusion_text,
                "text_hash": record.text_hash,
                "embedding": blob,
                "dim": dim,
                "model_id": self.model_id,
                "vector_version": self.vector_version,
                "sentiment_score": record.sentiment_score,
                "operation_advice": record.operation_advice,
                "created_at": now,
            }
            # 同一批次内同 history_id 只保留最后一条，规避 SQLite
            # "ON CONFLICT DO UPDATE 不能在单语句内命中两次同一行"的报错
            deduped[row["history_id"]] = row
        if len(deduped) < (stats["received"] - stats["skipped"]):
            stats["skipped"] += (stats["received"] - stats["skipped"]) - len(deduped)
        return list(deduped.values())

    # ------------------------------------------------------------------ #
    # 候选拉取
    # ------------------------------------------------------------------ #

    def _cache_key(self, query: RecallQuery, cutoff: str) -> Tuple:
        """候选缓存键：所有影响 SQL 结果集的过滤条件。"""
        return (
            self.model_id,
            self.vector_version,
            query.scope,
            query.stock_code if query.scope == SCOPE_SAME_STOCK else "",
            cutoff,
            query.report_type or "",
            self.max_candidates,
        )

    @staticmethod
    def _cutoff_date(lookback_days: int) -> str:
        """回看窗口起始日；``lookback_days <= 0`` 表示不限制（返回空串）。"""
        if lookback_days <= 0:
            return ""
        return (date.today() - timedelta(days=int(lookback_days))).isoformat()

    def load_candidates(self, query: RecallQuery) -> CandidateSet:
        """按 ``(model_id, vector_version)`` + 业务条件拉取候选矩阵。

        **绝不放宽版本过滤**：宁可返回空集并标记 ``model_mismatch``，也不允许
        跨模型向量参与点积（那只会产出看似合理实则随机的相似度）。

        本方法不抛异常：数据库故障返回 ``degrade_reason=store_error`` 的空候选集。
        """
        cutoff = self._cutoff_date(int(query.lookback_days or 0))
        key = self._cache_key(query, cutoff)
        with self._lock:
            generation = self._generation
        cached = self._cache.get(key, generation)
        if cached is not None:
            return cached

        try:
            candidates = self._query_candidates(query, cutoff)
        except Exception as exc:  # noqa: BLE001 —— 拉取失败一律降级，不影响主流程
            logger.warning("%s 候选拉取失败，降级为空召回: %s", LOG_PREFIX, exc)
            return CandidateSet(
                matrix=None,
                meta=[],
                model_id=self.model_id,
                vector_version=self.vector_version,
                degrade_reason=DegradeReason.STORE_ERROR.value,
            )

        self._cache.put(key, candidates, generation)
        return candidates

    def _query_candidates(self, query: RecallQuery, cutoff: str) -> CandidateSet:
        """真正执行 SQL 并解码 BLOB（异常由 :meth:`load_candidates` 兜住）。"""
        model = self._model()
        db = self._get_db()

        with db.session_scope() as session:
            statement = session.query(
                model.history_id,
                model.code,
                model.name,
                model.report_type,
                model.trade_date,
                model.time_slot,
                model.conclusion_text,
                model.sentiment_score,
                model.operation_advice,
                model.embedding,
                model.dim,
            ).filter(
                # ↓↓↓ 版本双过滤：本层的核心不变量，任何情况下都不得省略 ↓↓↓
                model.model_id == self.model_id,
                model.vector_version == self.vector_version,
                model.embedding.isnot(None),
            )

            if query.scope == SCOPE_SAME_STOCK and query.stock_code:
                statement = statement.filter(model.code == query.stock_code)
            if cutoff:
                statement = statement.filter(model.trade_date >= cutoff)
            if query.report_type:
                statement = statement.filter(model.report_type == query.report_type)

            statement = statement.order_by(
                model.trade_date.desc(), model.history_id.desc()
            ).limit(self.max_candidates + 1)

            rows = statement.all()

            truncated = len(rows) > self.max_candidates
            if truncated:
                rows = rows[: self.max_candidates]

            if not rows:
                return self._empty_candidates(session, model)

        return self._decode_rows(rows, truncated)

    def _empty_candidates(self, session: Any, model: Any) -> CandidateSet:
        """零候选时区分 ``empty_store`` 与 ``model_mismatch``。

        表里一条数据都没有 → ``empty_store``（正常冷启动）；
        表里有数据但**没有一条属于当前 (model_id, vector_version)** → ``model_mismatch``
        （典型场景：改了 ``LTM_EMBEDDING_MODEL`` 却没跑 backfill）。
        """
        any_row = session.query(model.id).limit(1).first()
        if any_row is None:
            return CandidateSet(
                matrix=None,
                meta=[],
                total_rows=0,
                model_id=self.model_id,
                vector_version=self.vector_version,
                degrade_reason=DegradeReason.EMPTY_STORE.value,
            )

        matched = session.query(model.id).filter(
            model.model_id == self.model_id,
            model.vector_version == self.vector_version,
        ).limit(1).first()
        if matched is None:
            logger.warning(
                "%s 向量库非空但无 (model_id=%s, vector_version=%d) 记录，"
                "召回降级为 model_mismatch；请执行 scripts/backfill_ltm.py 回填",
                LOG_PREFIX, self.model_id, self.vector_version,
            )
            reason = DegradeReason.MODEL_MISMATCH.value
        else:
            reason = DegradeReason.EMPTY_STORE.value

        return CandidateSet(
            matrix=None,
            meta=[],
            total_rows=0,
            model_id=self.model_id,
            vector_version=self.vector_version,
            degrade_reason=reason,
        )

    def _decode_rows(self, rows: Sequence[Any], truncated: bool) -> CandidateSet:
        """把查询行解码为 ``(n, dim)`` 矩阵 + 对齐的元数据列表。"""
        vectors: List[np.ndarray] = []
        meta: List[Dict[str, Any]] = []
        expected_dim = 0
        dropped = 0

        for row in rows:
            declared_dim = int(row.dim or 0)
            vector = decode_vector(row.embedding, declared_dim)
            if vector is None:
                dropped += 1
                continue
            if expected_dim == 0:
                expected_dim = int(vector.size)
            elif int(vector.size) != expected_dim:
                # 同一 model_id 下出现异构维度 = 脏数据，直接丢弃该行
                dropped += 1
                continue
            vectors.append(vector)
            meta.append({
                "history_id": int(row.history_id or 0),
                "code": str(row.code or ""),
                "name": str(row.name or ""),
                "report_type": str(row.report_type or ""),
                "trade_date": str(row.trade_date or ""),
                "time_slot": str(row.time_slot or ""),
                "conclusion_text": str(row.conclusion_text or ""),
                "sentiment_score": row.sentiment_score,
                "operation_advice": str(row.operation_advice or ""),
            })

        if dropped:
            logger.warning("%s 候选中有 %d 行向量无法解码，已丢弃", LOG_PREFIX, dropped)

        if not vectors:
            return CandidateSet(
                matrix=None,
                meta=[],
                truncated=truncated,
                total_rows=0,
                model_id=self.model_id,
                vector_version=self.vector_version,
                degrade_reason=DegradeReason.EMPTY_STORE.value,
            )

        matrix = np.vstack(vectors).astype(np.float32, copy=False)
        matrix = self._ensure_normalized(matrix)
        return CandidateSet(
            matrix=matrix,
            meta=meta,
            truncated=truncated,
            total_rows=int(matrix.shape[0]),
            model_id=self.model_id,
            vector_version=self.vector_version,
            dim=int(matrix.shape[1]),
            degrade_reason=None,
        )

    @staticmethod
    def _ensure_normalized(matrix: np.ndarray) -> np.ndarray:
        """兜底归一化：正常路径下落盘即归一化，此处只修历史脏数据。"""
        norms = np.linalg.norm(matrix, axis=1)
        if np.all(np.abs(norms - 1.0) <= _NORM_TOLERANCE):
            return matrix
        safe = np.where(norms == 0.0, 1.0, norms).astype(np.float32).reshape(-1, 1)
        logger.debug("%s 检测到未归一化的落盘向量，已在检索侧兜底归一化", LOG_PREFIX)
        return np.asarray(matrix / safe, dtype=np.float32)

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #

    def search(self, query: RecallQuery) -> RecallResult:
        """余弦 TopK 检索 + 时间衰减。

        Args:
            query: 必须已带 ``query_embedding``（embed 由门面层负责，本层不碰网络）。

        Returns:
            :class:`RecallResult`。**任何异常路径都返回 ``RecallResult.empty(...)``**，
            绝不向 pipeline 抛异常（设计 §8.9）。
        """
        started = time.perf_counter()
        query.validate()

        def _empty(reason: Any, candidate_count: int = 0) -> RecallResult:
            return RecallResult.empty(
                reason,
                embedding_provider=self.provider_name,
                model_id=self.model_id,
                stock_code=query.stock_code,
                scope=query.scope,
                min_similarity=query.min_similarity,
                candidate_count=candidate_count,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        try:
            if query.query_embedding is None:
                return _empty(DegradeReason.EMBED_FAILED)

            vector = np.asarray(query.query_embedding, dtype=np.float32).reshape(-1)
            if vector.size == 0 or not np.isfinite(vector).all():
                return _empty(DegradeReason.EMBED_FAILED)

            candidates = self.load_candidates(query)
            if candidates.is_empty() or candidates.matrix is None:
                return _empty(candidates.degrade_reason or DegradeReason.EMPTY_STORE)

            matrix = candidates.matrix
            if int(vector.size) != int(matrix.shape[1]):
                logger.warning(
                    "%s 查询向量维度 %d 与候选矩阵维度 %d 不一致，降级 model_mismatch",
                    LOG_PREFIX, int(vector.size), int(matrix.shape[1]),
                )
                return _empty(DegradeReason.MODEL_MISMATCH, len(candidates))

            vector = self._normalize_query(vector)
            items = self._rank(query, matrix, candidates.meta, vector)

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            result = RecallResult(
                enabled=True,
                degraded=False,
                degrade_reason=None,
                embedding_provider=self.provider_name,
                model_id=self.model_id,
                candidate_count=len(candidates),
                hit_count=len(items),
                elapsed_ms=elapsed_ms,
                items=items,
                stock_code=query.stock_code,
                scope=query.scope,
                min_similarity=query.min_similarity,
                truncated=bool(candidates.truncated),
            )
            logger.debug(
                "%s search 完成: code=%s scope=%s candidates=%d hits=%d elapsed=%.2fms",
                LOG_PREFIX, query.stock_code, query.scope,
                result.candidate_count, result.hit_count, elapsed_ms,
            )
            return result
        except Exception as exc:  # noqa: BLE001 —— fail-open 铁律
            logger.warning("%s 语义召回异常，已降级为空结果: %s", LOG_PREFIX, exc)
            return _empty(DegradeReason.STORE_ERROR)

    @staticmethod
    def _normalize_query(vector: np.ndarray) -> np.ndarray:
        """查询向量兜底归一化（provider 已归一化，此处只防调用方手搓向量）。"""
        norm = float(np.linalg.norm(vector))
        if norm == 0.0 or abs(norm - 1.0) <= _NORM_TOLERANCE:
            return vector
        return np.asarray(vector / norm, dtype=np.float32)

    def _rank(
        self,
        query: RecallQuery,
        matrix: np.ndarray,
        meta: List[Dict[str, Any]],
        vector: np.ndarray,
    ) -> List[RecallItem]:
        """点积打分 → 阈值过滤 → 时间衰减 → TopK。"""
        # 已归一化 ⇒ 余弦相似度 == 点积
        scores = np.asarray(matrix @ vector, dtype=np.float32)

        keep = scores >= float(query.min_similarity)
        if query.exclude_history_ids:
            excluded = set(query.exclude_history_ids)
            for index, entry in enumerate(meta):
                if entry["history_id"] in excluded:
                    keep[index] = False
        candidate_index = np.flatnonzero(keep)
        if candidate_index.size == 0:
            return []

        today = date.today()
        halflife = int(query.halflife_days) if query.apply_decay else 0
        ages = np.array(
            [compute_age_days(meta[int(i)]["trade_date"], today) for i in candidate_index],
            dtype=np.float32,
        )
        raw = scores[candidate_index]
        if halflife > 0:
            final = raw * np.power(np.float32(0.5), ages / np.float32(halflife))
        else:
            final = raw.copy()

        top_positions = self._topk_positions(final, int(query.top_k))

        items: List[RecallItem] = []
        for position in top_positions:
            row = int(candidate_index[position])
            entry = meta[row]
            items.append(RecallItem(
                history_id=int(entry["history_id"]),
                stock_code=str(entry["code"]),
                stock_name=str(entry["name"]),
                trade_date=str(entry["trade_date"]),
                time_slot=str(entry["time_slot"]),
                age_days=int(ages[position]),
                similarity=float(raw[position]),
                final_score=float(final[position]),
                conclusion_text=str(entry["conclusion_text"]),
                sentiment_score=entry["sentiment_score"],
                operation_advice=str(entry["operation_advice"]),
                outcome=None,
            ))
        return items

    @staticmethod
    def _topk_positions(scores: np.ndarray, top_k: int) -> List[int]:
        """返回 ``scores`` 中最大的 ``top_k`` 个下标（降序）。

        ``n`` 较大时用 ``np.argpartition``（O(n)）先粗选再对 k 个元素排序，
        比全量 ``argsort``（O(n log n)）显著更省。
        """
        size = int(scores.size)
        k = max(min(int(top_k), size), 1)
        if k >= size:
            order = np.argsort(-scores, kind="stable")
        else:
            partition = np.argpartition(-scores, k - 1)[:k]
            order = partition[np.argsort(-scores[partition], kind="stable")]
        return [int(i) for i in order[:k]]

    # ------------------------------------------------------------------ #
    # 运维辅助
    # ------------------------------------------------------------------ #

    def count(self, *, current_model_only: bool = True) -> int:
        """统计向量行数（失败返回 0，不抛异常）。"""
        try:
            model = self._model()
            db = self._get_db()
            with db.session_scope() as session:
                statement = session.query(model.id)
                if current_model_only:
                    statement = statement.filter(
                        model.model_id == self.model_id,
                        model.vector_version == self.vector_version,
                    )
                return int(statement.count())
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s count 失败: %s", LOG_PREFIX, exc)
            return 0

    def list_missing_embedding(self, limit: int = 500) -> List[Dict[str, Any]]:
        """列出"只有文本、没有向量"的行（供 backfill 脚本消费）。"""
        try:
            model = self._model()
            db = self._get_db()
            with db.session_scope() as session:
                rows = session.query(
                    model.history_id, model.conclusion_text, model.text_hash,
                ).filter(
                    model.model_id == self.model_id,
                    model.vector_version == self.vector_version,
                    model.embedding.is_(None),
                ).limit(max(int(limit or 0), 1)).all()
            return [
                {
                    "history_id": int(row.history_id or 0),
                    "conclusion_text": str(row.conclusion_text or ""),
                    "text_hash": str(row.text_hash or ""),
                }
                for row in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s list_missing_embedding 失败: %s", LOG_PREFIX, exc)
            return []


def build_vector_store(config: Any, model_id: str, db_manager: Any = None) -> SqliteVectorStore:
    """按 config 组装 :class:`SqliteVectorStore`。"""
    return SqliteVectorStore(
        model_id,
        db_manager=db_manager,
        vector_version=VECTOR_VERSION,
        max_candidates=int(getattr(config, "ltm_max_candidates", 20000) or 20000),
    )


__all__ = [
    "SqliteVectorStore",
    "build_vector_store",
    "apply_time_decay",
    "encode_vector",
    "decode_vector",
    "SCOPE_SAME_STOCK",
    "SCOPE_GLOBAL",
]
