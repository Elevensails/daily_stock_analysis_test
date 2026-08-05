# -*- coding: utf-8 -*-
"""
===================================
U12 语义缓存包（LLM 结果缓存去重）
===================================

对外只暴露门面层与数据契约。``src/analyzer.py`` 只应 import 本包根：

    from src.cache import build_semantic_cache

**禁止**从外部直接 import ``src.cache.cache_store`` —— 存储层的三层分区防御
依赖门面层构造的完整 :class:`CacheKey`，绕过门面等于绕过铁律 #1。
（运维脚本 ``scripts/semcache_admin.py`` 通过 ``SemanticCache.store`` 访问。）

物理隔离声明（铁律 #4）：本包**只**读写 ``llm_semantic_cache`` 表，
与 U14 的 ``analysis_memory_vector`` / ``SqliteVectorStore`` 零交集，
仅复用 ``encode_vector`` / ``decode_vector`` / ``LocalLexicalEmbeddingProvider``
三个无状态纯组件。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Tuple

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
    CacheDegradeReason,
    CacheEntry,
    CacheHit,
    CacheKey,
    CacheMiss,
    compute_params_fingerprint,
)
from src.cache.semantic_cache import SEMANTIC_TIER_ENABLED, SemanticCache

logger = logging.getLogger(__name__)

#: 按配置签名复用 :class:`SemanticCache` 实例。
#: 单例的意义不在省内存（对象很轻），而在于 ``_semantic_warned`` 之类的
#: "只告警一次"状态不会因为每股票重建实例而反复刷屏。
_INSTANCES: Dict[Tuple[Any, ...], SemanticCache] = {}
_INSTANCES_LOCK = threading.Lock()

#: 签名缓存上限（配置热更新时避免无限增长）
_MAX_INSTANCES: int = 8


def build_semantic_cache(config: Any = None) -> SemanticCache:
    """按配置获取（并复用）缓存门面实例。

    Args:
        config: :class:`src.config.Config` 实例；``None`` 时返回"全默认 = 关闭"实例。

    Returns:
        :class:`SemanticCache`。**本函数永不抛异常** —— 构造失败时返回一个
        ``enabled=False`` 的空壳实例，让主链路无感降级。
    """
    try:
        signature = SemanticCache.config_signature(config)
    except Exception as exc:
        logger.debug("%s 配置签名计算失败，返回关闭态缓存: %s", LOG_PREFIX, exc)
        return SemanticCache(enabled=False)

    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(signature)
        if instance is not None:
            return instance
        try:
            instance = SemanticCache.from_config(config)
        except Exception as exc:
            logger.warning("%s 缓存门面构造失败，已整体关闭缓存: %s", LOG_PREFIX, exc)
            instance = SemanticCache(enabled=False)
        if len(_INSTANCES) >= _MAX_INSTANCES:
            _INSTANCES.clear()
        _INSTANCES[signature] = instance
        return instance


def reset_semantic_cache() -> None:
    """清空实例缓存（测试隔离用；生产不调用）。"""
    with _INSTANCES_LOCK:
        _INSTANCES.clear()


__all__ = [
    "SemanticCache",
    "CacheGuards",
    "CacheKey",
    "CacheEntry",
    "CacheHit",
    "CacheMiss",
    "CacheDegradeReason",
    "compute_params_fingerprint",
    "build_semantic_cache",
    "reset_semantic_cache",
    "SEMANTIC_TIER_ENABLED",
    "SEMCACHE_MODEL_ID",
    "SEMCACHE_VECTOR_VERSION",
    "CACHE_MODE_EXACT",
    "CACHE_MODE_SEMANTIC",
    "CACHE_TIER_EXACT",
    "CACHE_TIER_SEMANTIC",
    "DEFAULT_SEMCACHE_LOG_PATH",
]
