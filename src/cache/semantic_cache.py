# -*- coding: utf-8 -*-
"""
===================================
U12 语义缓存 — 门面层（SemanticCache）
===================================

对外唯一入口。``src/analyzer.py`` 只认识本文件的三个方法：
:meth:`SemanticCache.build_key` / :meth:`SemanticCache.get` /
:meth:`SemanticCache.put`，其余实现细节（分区、SQL、向量、埋点）全部封装在内。

本版能力边界（用户决策 #1，硬约束）：

    ┌── Tier-0 精确命中 ──────────────────────────────────────────┐
    │ prompt 逐字节一致 + 七维分区完全一致 ⇒ 命中                  │
    │ 这是本版**唯一**启用的命中路径                               │
    └──────────────────────────────────────────────────────────────┘
    ┌── Tier-1 语义近似 ──────────────────────────────────────────┐
    │ 本版**不启用**（见 :data:`SEMANTIC_TIER_ENABLED`）           │
    │ 存储层 ``search_semantic`` 能力已完整实现并通过测试，          │
    │ 门面层保留 ``mode`` 字段与调用钩子。                          │
    │ TODO(U12-Tier1): 未来接入只需两步 ——                          │
    │   1. SEMANTIC_TIER_ENABLED = True                            │
    │   2. config.yaml 置 sem_cache.mode: "semantic"               │
    │ 无需改动 analyzer / store / models 任何一行。                 │
    └──────────────────────────────────────────────────────────────┘

降级铁律（用户决策 #5）：本层**所有公开方法永不抛异常**。任何内部异常一律
按"未命中 / 未写入"处理，主链路照常走真实 LLM 调用。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Tuple

from src.cache.cache_store import SqliteSemanticCacheStore
from src.cache.guards import CacheGuards
from src.cache.models import (
    CACHE_MODE_EXACT,
    CACHE_MODE_SEMANTIC,
    CACHE_TIER_EXACT,
    CACHE_TIER_SEMANTIC,
    DEFAULT_SEMCACHE_LOG_PATH,
    LOG_PREFIX,
    SEMCACHE_MODEL_ID,
    SEMCACHE_VECTOR_VERSION,
    VALID_CACHE_MODES,
    CacheDegradeReason,
    CacheEntry,
    CacheHit,
    CacheKey,
    compute_params_fingerprint,
)
from src.cache.telemetry import append_cache_record

logger = logging.getLogger(__name__)

#: **本版 Tier-1 语义命中总闸**。用户决策 #1：首版只做 Tier-0。
#: 置 ``True`` 前必须先补齐 §9 的语义误命中回归用例，否则等于放开铁律 #2。
SEMANTIC_TIER_ENABLED: bool = False

#: U12 自有的向量维度（**不复用 ltm_embedding_dim**，保持与 U14 配置解耦，铁律 #4）
DEFAULT_SEMCACHE_DIM: int = 1024

#: 默认 TTL（小时）
DEFAULT_TTL_HOURS: int = 12


class SemanticCache:
    """LLM 结果缓存门面。

    Attributes:
        enabled: 总开关。``False`` 时 :meth:`get` 恒返回 ``None``、
            :meth:`put` 恒返回 ``False``，且**不建立任何数据库连接**。
        mode: ``exact`` / ``semantic``。本版 ``semantic`` 会被
            :data:`SEMANTIC_TIER_ENABLED` 强制降级为 ``exact``。
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        mode: str = CACHE_MODE_EXACT,
        min_similarity: float = 0.95,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        max_candidates: int = 64,
        min_response_chars: int = 200,
        store_prompt_text: bool = True,
        log_path: str = DEFAULT_SEMCACHE_LOG_PATH,
        embedding_dim: int = DEFAULT_SEMCACHE_DIM,
        embed_on_put: bool = True,
        store: Optional[SqliteSemanticCacheStore] = None,
        guards: Optional[CacheGuards] = None,
        embedding_provider: Any = None,
    ) -> None:
        self.enabled = bool(enabled)

        requested_mode = str(mode or CACHE_MODE_EXACT).strip().lower()
        if requested_mode not in VALID_CACHE_MODES:
            logger.warning(
                "%s 未知 sem_cache.mode=%r，已回退为 %s",
                LOG_PREFIX, requested_mode, CACHE_MODE_EXACT,
            )
            requested_mode = CACHE_MODE_EXACT
        #: 用户配置的原始模式（保留原值，便于运维排查"我明明配了 semantic"）
        self.mode: str = requested_mode

        try:
            self.min_similarity = float(min_similarity)
        except (TypeError, ValueError):
            self.min_similarity = 0.95
        try:
            self.ttl_hours = max(int(ttl_hours), 0)
        except (TypeError, ValueError):
            self.ttl_hours = DEFAULT_TTL_HOURS
        try:
            self.embedding_dim = max(int(embedding_dim), 8)
        except (TypeError, ValueError):
            self.embedding_dim = DEFAULT_SEMCACHE_DIM

        self.log_path = str(log_path or "").strip()
        self.embed_on_put = bool(embed_on_put)
        self.store_prompt_text = bool(store_prompt_text)

        self.guards = guards or CacheGuards(min_response_chars=min_response_chars)
        self._store = store or SqliteSemanticCacheStore(
            SEMCACHE_MODEL_ID,
            vector_version=SEMCACHE_VECTOR_VERSION,
            max_candidates=max_candidates,
            store_prompt_text=self.store_prompt_text,
        )
        self._provider = embedding_provider
        self._semantic_warned = False

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @staticmethod
    def config_signature(config: Any) -> Tuple[Any, ...]:
        """把配置压成可哈希签名（供 ``build_semantic_cache`` 复用实例）。"""
        def _pick(name: str, default: Any) -> Any:
            return getattr(config, name, default) if config is not None else default

        return (
            bool(_pick("sem_cache_enabled", False)),
            str(_pick("sem_cache_mode", CACHE_MODE_EXACT)),
            float(_pick("sem_cache_min_similarity", 0.95) or 0.0),
            int(_pick("sem_cache_ttl_hours", DEFAULT_TTL_HOURS) or 0),
            int(_pick("sem_cache_max_candidates", 64) or 0),
            int(_pick("sem_cache_min_response_chars", 200) or 0),
            bool(_pick("sem_cache_store_prompt_text", True)),
            str(_pick("sem_cache_log_path", DEFAULT_SEMCACHE_LOG_PATH) or ""),
        )

    @classmethod
    def from_config(cls, config: Any = None, **overrides: Any) -> "SemanticCache":
        """从全局配置构造。配置缺失时一律取"关闭 + 保守默认"。

        Args:
            config: :class:`src.config.Config` 实例（或任意有同名属性的对象）。
            **overrides: 覆写任意构造参数（测试注入 store / guards 用）。
        """
        def _pick(name: str, default: Any) -> Any:
            if config is None:
                return default
            value = getattr(config, name, default)
            return default if value is None else value

        kwargs: Dict[str, Any] = {
            "enabled": bool(_pick("sem_cache_enabled", False)),
            "mode": str(_pick("sem_cache_mode", CACHE_MODE_EXACT)),
            "min_similarity": _pick("sem_cache_min_similarity", 0.95),
            "ttl_hours": _pick("sem_cache_ttl_hours", DEFAULT_TTL_HOURS),
            "max_candidates": _pick("sem_cache_max_candidates", 64),
            "min_response_chars": _pick("sem_cache_min_response_chars", 200),
            "store_prompt_text": bool(_pick("sem_cache_store_prompt_text", True)),
            "log_path": str(_pick("sem_cache_log_path", DEFAULT_SEMCACHE_LOG_PATH)),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # ------------------------------------------------------------------ #
    # 惰性 embedding provider
    # ------------------------------------------------------------------ #

    def _get_provider(self) -> Any:
        """惰性构造本地词法向量 provider。

        复用 U14 的 :class:`LocalLexicalEmbeddingProvider` **算法**，但向量落在
        U12 自己的命名空间 :data:`SEMCACHE_MODEL_ID` 下（由 store 统一改写
        ``model_id`` 列），因此不会与 ``analysis_memory_vector`` 产生任何耦合。
        """
        if self._provider is None:
            from src.memory.embedding_provider import (  # noqa: PLC0415 —— 惰性导入
                LocalLexicalEmbeddingProvider,
            )

            self._provider = LocalLexicalEmbeddingProvider(dim=self.embedding_dim)
        return self._provider

    def _embed(self, text: str) -> Any:
        """把文本编码为 L2 归一化向量；失败返回 ``None``（不抛异常）。"""
        try:
            matrix = self._get_provider().embed([text])
            if matrix is None or getattr(matrix, "shape", (0,))[0] == 0:
                return None
            return matrix[0]
        except Exception as exc:
            logger.debug("%s embedding 失败，本行不落向量: %s", LOG_PREFIX, exc)
            return None

    # ------------------------------------------------------------------ #
    # Key 构造
    # ------------------------------------------------------------------ #

    def build_key(
        self,
        *,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        generation_config: Optional[Mapping[str, Any]] = None,
        code: str = "",
        trade_date: Any = "",
        time_slot: Any = "",
        report_type: str = "daily",
        llm_model: str = "",
        backend_id: str = "",
    ) -> Optional[CacheKey]:
        """构造并校验分区键。

        ``context`` 中的同名键优先级**低于**显式关键字参数，便于调用方局部覆写。

        Returns:
            完整且已归一化的 :class:`CacheKey`；任一分区维度缺失时返回 ``None``
            （fail-safe：宁可不缓存，也不允许维度残缺的 key 进入查询）。
        """
        try:
            source: Mapping[str, Any] = context or {}
            key = CacheKey(
                code=code or str(source.get("code", "") or ""),
                trade_date=trade_date or source.get("trade_date", ""),
                time_slot=time_slot or source.get("time_slot", ""),
                report_type=report_type or str(source.get("report_type", "") or "daily"),
                llm_model=llm_model or str(source.get("llm_model", "") or ""),
                backend_id=backend_id or str(source.get("backend_id", "") or ""),
                params_fingerprint=compute_params_fingerprint(generation_config),
                prompt_hash=self.guards.hash_prompt(prompt, system_prompt),
            )
            key.validate()
            if not key.is_complete() or not key.prompt_hash:
                logger.debug(
                    "%s 分区维度不完整，跳过缓存: %s", LOG_PREFIX, key.to_log_fields()
                )
                return None
            return key
        except Exception as exc:
            logger.debug("%s build_key 异常，跳过缓存: %s", LOG_PREFIX, exc)
            return None

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def get(
        self,
        key: CacheKey,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[CacheHit]:
        """查缓存。

        Args:
            key: :meth:`build_key` 产出的分区键。
            prompt: 原始 prompt（用于 deny-list 判定与 Tier-1 向量化）。
            system_prompt: 系统提示词（已参与 ``key.prompt_hash``，此处仅备用）。
            now: 过期判定基准时刻（测试注入）。

        Returns:
            :class:`CacheHit`；未命中 / 关闭 / 任何异常时返回 ``None``。
            **永不抛异常。**
        """
        started = time.perf_counter()
        if not self.enabled:
            return None
        if key is None or not isinstance(key, CacheKey) or not key.is_complete():
            self._log("get", key=key, degrade=CacheDegradeReason.NO_CONTEXT, started=started)
            return None

        cacheable, reason = self.guards.is_prompt_cacheable(prompt)
        if not cacheable:
            self._log(
                "get", key=key, degrade=CacheDegradeReason.NOT_CACHEABLE,
                started=started, prompt_chars=len(prompt or ""), guard_reason=reason,
            )
            return None

        reference = now or datetime.now()
        try:
            entry = self._store.lookup_exact(key, now=reference)
            tier = CACHE_TIER_EXACT
            degrade = CacheDegradeReason.EMPTY_PARTITION

            # ---- Tier-1 语义钩子（本版关闭）------------------------------- #
            if entry is None and self._semantic_requested():
                # TODO(U12-Tier1): 打开 SEMANTIC_TIER_ENABLED 后本分支自动生效。
                #   语义命中的误召回风险由 min_similarity + 同分区强过滤共同约束，
                #   接入前必须补齐 §9「近似 prompt 不得跨语义命中」回归用例。
                vector = self._embed(prompt)
                if vector is None:
                    degrade = CacheDegradeReason.EMBED_FAILED
                else:
                    entry = self._store.search_semantic(
                        key, vector, self.min_similarity, now=reference
                    )
                    tier = CACHE_TIER_SEMANTIC
                    if entry is None:
                        degrade = CacheDegradeReason.BELOW_THRESHOLD
        except Exception as exc:
            logger.warning("%s get 异常，降级为未命中: %s", LOG_PREFIX, exc)
            self._log("get", key=key, degrade=CacheDegradeReason.STORE_ERROR, started=started)
            return None

        if entry is None:
            self._log(
                "get", key=key,
                degrade=(
                    CacheDegradeReason.SEMANTIC_DISABLED
                    if (self.mode == CACHE_MODE_SEMANTIC and not SEMANTIC_TIER_ENABLED)
                    else degrade
                ),
                started=started, prompt_chars=len(prompt or ""),
            )
            return None

        hit = CacheHit.from_entry(
            entry, tier=tier, similarity=entry.similarity, now=reference
        )
        logger.info(
            "%s 缓存命中 tier=%s code=%s date=%s slot=%s age=%ss chars=%d",
            LOG_PREFIX, tier, key.code, key.trade_date, key.time_slot,
            hit.age_seconds, len(hit.response_text or ""),
        )
        self._log(
            "get", key=key, hit=hit, started=started,
            prompt_chars=len(prompt or ""), response_chars=len(hit.response_text or ""),
        )
        return hit

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def put(
        self,
        key: CacheKey,
        prompt: str,
        response_text: str,
        *,
        system_prompt: Optional[str] = None,
        response_model: str = "",
        usage: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """写回缓存。

        Returns:
            是否真正落盘。关闭 / 判据不过 / DB 异常时返回 ``False``。
            **永不抛异常。**
        """
        started = time.perf_counter()
        if not self.enabled:
            return False
        if key is None or not isinstance(key, CacheKey) or not key.is_complete():
            return False

        prompt_ok, prompt_reason = self.guards.is_prompt_cacheable(prompt)
        if not prompt_ok:
            self._log(
                "put", key=key, degrade=CacheDegradeReason.NOT_CACHEABLE,
                started=started, guard_reason=prompt_reason,
            )
            return False

        response_ok, response_reason = self.guards.is_response_cacheable(response_text)
        if not response_ok:
            self._log(
                "put", key=key, degrade=CacheDegradeReason.RESPONSE_REJECTED,
                started=started, response_chars=len(response_text or ""),
                guard_reason=response_reason,
            )
            return False

        reference = now or datetime.now()
        expires_at = (
            reference + timedelta(hours=self.ttl_hours) if self.ttl_hours > 0 else None
        )

        # 向量在 Tier-0 版本用不到，但仍默认落盘 —— 这样未来打开 Tier-1 时
        # 存量缓存立即可用，无需回填脚本（成本：一次纯 numpy 词法编码，亚毫秒级）。
        embedding = self._embed(prompt) if self.embed_on_put else None

        entry = CacheEntry(
            partition_key=key.partition_key(),
            prompt_hash=key.prompt_hash,
            code=key.code,
            trade_date=key.trade_date,
            time_slot=key.time_slot,
            report_type=key.report_type,
            llm_model=key.llm_model,
            backend_id=key.backend_id,
            params_fingerprint=key.params_fingerprint,
            prompt_text=self.guards.normalize_prompt(prompt) if self.store_prompt_text else "",
            response_text=str(response_text or ""),
            response_model=str(response_model or ""),
            original_usage=dict(usage or {}),
            embedding=embedding,
            dim=int(getattr(embedding, "size", 0) or 0),
            model_id=SEMCACHE_MODEL_ID,
            vector_version=SEMCACHE_VECTOR_VERSION,
            created_at=reference,
            expires_at=expires_at,
        )

        try:
            written = self._store.upsert(entry)
        except Exception as exc:  # pragma: no cover —— store 内部已兜底
            logger.warning("%s put 异常，缓存未写入: %s", LOG_PREFIX, exc)
            written = False

        self._log(
            "put", key=key, started=started,
            prompt_chars=len(prompt or ""), response_chars=len(response_text or ""),
            degrade=CacheDegradeReason.NONE if written else CacheDegradeReason.STORE_ERROR,
            written=written,
        )
        return bool(written)

    # ------------------------------------------------------------------ #
    # 运维
    # ------------------------------------------------------------------ #

    def stats(self) -> Dict[str, Any]:
        """缓存统计（``scripts/semcache_admin.py --stats`` 与自检用）。"""
        payload: Dict[str, Any] = {
            "enabled": self.enabled,
            "mode": self.mode,
            "effective_mode": self.effective_mode,
            "min_similarity": self.min_similarity,
            "ttl_hours": self.ttl_hours,
            "semantic_tier_enabled": SEMANTIC_TIER_ENABLED,
        }
        if not self.enabled:
            return payload
        try:
            payload.update(self._store.stats())
        except Exception as exc:  # pragma: no cover —— store 内部已兜底
            logger.debug("%s stats 失败: %s", LOG_PREFIX, exc)
        return payload

    def purge_expired(self, now: Optional[datetime] = None) -> int:
        """清理过期行。返回删除行数（异常时 0）。"""
        try:
            return int(self._store.purge_expired(now))
        except Exception as exc:  # pragma: no cover
            logger.debug("%s purge_expired 失败: %s", LOG_PREFIX, exc)
            return 0

    def purge_all(self) -> int:
        """清空本命名空间缓存。返回删除行数（异常时 0）。"""
        try:
            return int(self._store.purge_all())
        except Exception as exc:  # pragma: no cover
            logger.debug("%s purge_all 失败: %s", LOG_PREFIX, exc)
            return 0

    @property
    def store(self) -> SqliteSemanticCacheStore:
        """底层存储（运维脚本 / 测试断言用）。"""
        return self._store

    @property
    def effective_mode(self) -> str:
        """本版真正生效的模式（``semantic`` 被总闸压回 ``exact``）。"""
        if self.mode == CACHE_MODE_SEMANTIC and SEMANTIC_TIER_ENABLED:
            return CACHE_MODE_SEMANTIC
        return CACHE_MODE_EXACT

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _semantic_requested(self) -> bool:
        """是否应走 Tier-1 分支（本版恒 ``False``，仅在总闸打开后为真）。"""
        if self.mode != CACHE_MODE_SEMANTIC:
            return False
        if SEMANTIC_TIER_ENABLED:
            return True
        if not self._semantic_warned:
            self._semantic_warned = True
            logger.warning(
                "%s 已配置 sem_cache.mode=semantic，但本版 Tier-1 语义命中未开放"
                "（SEMANTIC_TIER_ENABLED=False），已按 exact 精确匹配运行",
                LOG_PREFIX,
            )
        return False

    def _log(
        self,
        event: str,
        *,
        key: Optional[CacheKey] = None,
        hit: Optional[CacheHit] = None,
        degrade: CacheDegradeReason = CacheDegradeReason.NONE,
        started: Optional[float] = None,
        prompt_chars: int = 0,
        response_chars: int = 0,
        **extra: Any,
    ) -> None:
        """埋点小包装（自身永不抛异常）。"""
        elapsed_ms = 0.0
        if started is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
        append_cache_record(
            event=event,
            log_path=self.log_path,
            key=key,
            hit=hit,
            mode=self.effective_mode,
            prompt_chars=prompt_chars,
            response_chars=response_chars,
            degrade_reason=degrade.value if isinstance(degrade, CacheDegradeReason) else str(degrade),
            elapsed_ms=elapsed_ms,
            **extra,
        )


__all__ = [
    "SemanticCache",
    "SEMANTIC_TIER_ENABLED",
    "DEFAULT_SEMCACHE_DIM",
    "DEFAULT_TTL_HOURS",
]
