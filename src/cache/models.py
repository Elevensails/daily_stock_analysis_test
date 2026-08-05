# -*- coding: utf-8 -*-
"""
===================================
U12 语义缓存 — 数据契约（唯一来源）
===================================

本模块是 U12 的**数据契约唯一来源**：分区键构造、prompt 指纹、生成参数指纹
的实现全部收敛在这里，禁止在 `cache_store.py` / `semantic_cache.py` /
`analyzer.py` 中重复实现（设计文档 §10.1）。

铁律映射：
- 铁律 #1（严禁跨时槽/跨交易日命中）→ :meth:`CacheKey.partition_key` 第 1 层防御
- 铁律 #3（跨标的/跨报告类型不命中）→ ``code`` / ``report_type`` 进分区键
- 铁律 #4（与 U14 物理隔离）→ :data:`SEMCACHE_MODEL_ID` 独立命名空间

哈希约定：**必须 ``hashlib.sha256``，禁用内置 ``hash()``**
（``PYTHONHASHSEED`` 随机化会破坏跨进程确定性，见
``src/memory/embedding_provider.py:283-284`` 的既有教训）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import hashlib
import json

import numpy as np

from src.memory.models import normalize_time_slot, normalize_trade_date

# --------------------------------------------------------------------------- #
# 全局常量（唯一来源）
# --------------------------------------------------------------------------- #

#: U12 缓存向量的命名空间 ID —— 与 U14 的 ``local:lexical-v1`` 严格区分（铁律 #4）
SEMCACHE_MODEL_ID: str = "local:semcache-v1"

#: U12 向量版本，**独立于** ``src.memory.models.VECTOR_VERSION``，可独立演进
SEMCACHE_VECTOR_VERSION: int = 1

#: 缓存模式
CACHE_MODE_EXACT: str = "exact"
CACHE_MODE_SEMANTIC: str = "semantic"
VALID_CACHE_MODES = frozenset({CACHE_MODE_EXACT, CACHE_MODE_SEMANTIC})

#: 命中层级
CACHE_TIER_EXACT: str = "exact"
CACHE_TIER_SEMANTIC: str = "semantic"

#: 日志前缀（便于 grep）
LOG_PREFIX: str = "[U12]"

#: 默认埋点路径
DEFAULT_SEMCACHE_LOG_PATH: str = "logs/semantic_cache.jsonl"

#: 组成分区键的字段（顺序固定，**不得调整**，调整即等于缓存全量失效）
PARTITION_FIELDS = (
    "code",
    "trade_date",
    "time_slot",
    "report_type",
    "llm_model",
    "backend_id",
    "params_fingerprint",
)

#: 第 3 层防御（Python 侧断言）逐字段复核的维度
ASSERT_FIELDS = ("code", "trade_date", "time_slot", "report_type", "llm_model")

#: 参与 ``params_fingerprint`` 的生成参数键（只取真正影响输出的键）
PARAMS_FINGERPRINT_KEYS = (
    "temperature",
    "max_tokens",
    "max_output_tokens",
    "top_p",
)

#: 各字段的入库长度上限（对齐 §3.3 DDL，避免超长写入被 SQLite 静默截断）
_MAX_CODE_LEN = 16
_MAX_TRADE_DATE_LEN = 10
_MAX_TIME_SLOT_LEN = 4
_MAX_REPORT_TYPE_LEN = 16
_MAX_LLM_MODEL_LEN = 128
_MAX_BACKEND_ID_LEN = 32
_MAX_PARAMS_FP_LEN = 32

#: ``normalize_trade_date`` / ``normalize_time_slot`` 的哨兵默认值。
#: 上游函数在解析失败时会回退到"今天"/"1800"，这对缓存是**危险**的
#: （空 time_slot 被悄悄补成 1800 会直接违反铁律 #1）。
#: 因此这里用哨兵把"解析失败"与"解析成功"区分开，失败一律归零为空串，
#: 让 :meth:`CacheKey.is_complete` 判否 —— fail-safe：宁可不缓存。
_TRADE_DATE_SENTINEL = "0000-00-00"
_TIME_SLOT_SENTINEL = "0000"


# --------------------------------------------------------------------------- #
# 降级原因
# --------------------------------------------------------------------------- #

class CacheDegradeReason(str, Enum):
    """缓存未命中 / 未写入的归因（供埋点与排障）。"""

    NONE = "none"
    DISABLED = "disabled"
    NO_CONTEXT = "no_context"
    NOT_CACHEABLE = "not_cacheable"
    EMBED_FAILED = "embed_failed"
    STORE_ERROR = "store_error"
    EMPTY_PARTITION = "empty_partition"
    BELOW_THRESHOLD = "below_threshold"
    EXPIRED = "expired"
    RESPONSE_REJECTED = "response_rejected"
    PARTITION_VIOLATION = "partition_violation"
    SEMANTIC_DISABLED = "semantic_disabled"


# --------------------------------------------------------------------------- #
# 归一化小工具（严格版：解析失败 ⇒ 空串，绝不"猜"）
# --------------------------------------------------------------------------- #

def normalize_trade_date_strict(value: Any) -> str:
    """把输入归一化为 ``YYYY-MM-DD``；**无法解析时返回空串**。

    与 :func:`src.memory.models.normalize_trade_date` 的差别：上游在解析失败时
    回退到"今天"，本函数回退到空串。缓存场景下"猜一个日期"等价于制造跨交易日
    命中风险，必须禁止。
    """
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return normalize_trade_date(value)[:_MAX_TRADE_DATE_LEN]
    raw = str(value).strip()
    if not raw:
        return ""
    normalized = normalize_trade_date(raw, default=_TRADE_DATE_SENTINEL)
    if normalized == _TRADE_DATE_SENTINEL:
        return ""
    return normalized[:_MAX_TRADE_DATE_LEN]


def normalize_time_slot_strict(value: Any) -> str:
    """把输入归一化为 4 位 ``HHMM``；**无法解析时返回空串**。

    同样禁止上游 ``normalize_time_slot`` 的 "回退到 1800" 行为 —— 那会让
    一个缺失时槽的请求命中 18:00 盘后缓存，是铁律 #1 最典型的踩雷方式。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = normalize_time_slot(raw, default=_TIME_SLOT_SENTINEL)
    if normalized == _TIME_SLOT_SENTINEL and raw != _TIME_SLOT_SENTINEL:
        return ""
    return normalized[:_MAX_TIME_SLOT_LEN]


def compute_params_fingerprint(generation_config: Any) -> str:
    """生成参数指纹 = ``sha256(json(排序后的关键参数))[:16]``（设计 §10.1）。

    只取 :data:`PARAMS_FINGERPRINT_KEYS` 中真正影响输出的键，避免无关键
    （如 ``stream`` / 回调对象）导致分区无谓碎片化。

    Args:
        generation_config: ``_call_litellm`` 收到的 ``generation_config`` 字典。

    Returns:
        16 位十六进制指纹；输入不可用时返回 ``"none"``（仍是稳定值，可入分区键）。
    """
    payload: Dict[str, Any] = {}
    try:
        source = generation_config if isinstance(generation_config, dict) else {}
        for key in PARAMS_FINGERPRINT_KEYS:
            if key not in source:
                continue
            value = source.get(key)
            if isinstance(value, (int, float, str, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = str(value)
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    except Exception:
        # fail-safe：指纹算不出来时给一个稳定占位值，绝不抛异常
        return "none"


# --------------------------------------------------------------------------- #
# CacheKey
# --------------------------------------------------------------------------- #

@dataclass
class CacheKey:
    """缓存分区键 —— 铁律 #1 / #3 的数据载体。

    七个分区维度中**任一维度不同 ⇒ 分区不同 ⇒ 物理上不可能互相命中**。
    ``prompt_hash`` 不属于分区维度，而是分区**内部**的内容指纹（Tier-0 判据）。
    """

    code: str = ""
    trade_date: str = ""
    time_slot: str = ""
    report_type: str = "daily"
    llm_model: str = ""
    backend_id: str = ""
    params_fingerprint: str = ""
    prompt_hash: str = ""

    # ------------------------------------------------------------------ #

    def validate(self) -> None:
        """就地归一化，**永不抛异常**（fail-open 铁律）。

        非法/缺失的分区字段一律归零为空串，由 :meth:`is_complete` 判否。
        """
        self.code = str(self.code or "").strip()[:_MAX_CODE_LEN]
        self.trade_date = normalize_trade_date_strict(self.trade_date)
        self.time_slot = normalize_time_slot_strict(self.time_slot)
        self.report_type = (str(self.report_type or "").strip().lower())[:_MAX_REPORT_TYPE_LEN]
        self.llm_model = str(self.llm_model or "").strip()[:_MAX_LLM_MODEL_LEN]
        self.backend_id = str(self.backend_id or "").strip()[:_MAX_BACKEND_ID_LEN]
        self.params_fingerprint = str(self.params_fingerprint or "").strip()[:_MAX_PARAMS_FP_LEN]
        self.prompt_hash = str(self.prompt_hash or "").strip()[:64]

    def is_complete(self) -> bool:
        """七个分区维度是否全部就绪。

        任一维度为空 ⇒ ``False`` ⇒ **既不查也不写**（设计 §10.1 fail-safe）。
        """
        for name in PARTITION_FIELDS:
            if not str(getattr(self, name, "") or "").strip():
                return False
        return True

    def partition_key(self) -> str:
        """分区指纹（第 1 层防御）。字段顺序固定，不得调整。"""
        raw = "|".join(
            [
                self.code,
                self.trade_date,
                self.time_slot,
                self.report_type,
                self.llm_model,
                self.backend_id,
                self.params_fingerprint,
                SEMCACHE_MODEL_ID,
                str(SEMCACHE_VECTOR_VERSION),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_log_fields(self) -> Dict[str, Any]:
        """脱敏后的埋点字段（不含 prompt 原文）。"""
        return {
            "code": self.code,
            "trade_date": self.trade_date,
            "time_slot": self.time_slot,
            "report_type": self.report_type,
            "llm_model": self.llm_model,
            "backend_id": self.backend_id,
            "params_fingerprint": self.params_fingerprint,
            "partition_key": self.partition_key() if self.is_complete() else "",
            "prompt_hash": self.prompt_hash,
        }


# --------------------------------------------------------------------------- #
# CacheEntry
# --------------------------------------------------------------------------- #

@dataclass
class CacheEntry:
    """一条缓存记录（内存态），与 ``llm_semantic_cache`` 表一一对应。"""

    partition_key: str = ""
    prompt_hash: str = ""
    code: str = ""
    trade_date: str = ""
    time_slot: str = ""
    report_type: str = "daily"
    llm_model: str = ""
    backend_id: str = ""
    params_fingerprint: str = ""
    prompt_text: str = ""
    response_text: str = ""
    response_model: str = ""
    original_usage: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    dim: int = 0
    model_id: str = SEMCACHE_MODEL_ID
    vector_version: int = SEMCACHE_VECTOR_VERSION
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    hit_count: int = 0
    last_hit_at: Optional[datetime] = None
    #: 数据库主键（读取时回填，供 ``touch_hit`` 使用；写入时可为 0）
    row_id: int = 0
    #: 检索得分（Tier-0 恒为 1.0；Tier-1 为点积余弦）
    similarity: float = 1.0

    def is_valid(self) -> bool:
        """是否是一条可入库的合法记录。"""
        if not self.partition_key or not self.prompt_hash:
            return False
        if not str(self.response_text or "").strip():
            return False
        for name in ("code", "trade_date", "time_slot", "report_type", "llm_model"):
            if not str(getattr(self, name, "") or "").strip():
                return False
        return True

    def age_seconds(self, now: Optional[datetime] = None) -> int:
        """记录写入至今的秒数（无 ``created_at`` 时返回 0）。"""
        if self.created_at is None:
            return 0
        reference = now or datetime.now()
        try:
            delta = (reference - self.created_at).total_seconds()
        except TypeError:
            return 0
        return max(int(delta), 0)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """是否已过 TTL（``expires_at is None`` 视为永不过期）。"""
        if self.expires_at is None:
            return False
        return (now or datetime.now()) > self.expires_at


# --------------------------------------------------------------------------- #
# CacheHit / CacheMiss
# --------------------------------------------------------------------------- #

@dataclass
class CacheHit:
    """一次缓存命中的对外结果（门面唯一产出类型）。"""

    response_text: str = ""
    response_model: str = ""
    original_usage: Dict[str, Any] = field(default_factory=dict)
    tier: str = CACHE_TIER_EXACT
    similarity: float = 1.0
    age_seconds: int = 0
    matched_prompt_hash: str = ""
    partition_key: str = ""

    def as_usage_payload(self) -> Dict[str, Any]:
        """缓存命中时返回给 ``_call_litellm`` 的 usage（设计 §10.4）。

        口径：本次未产生真实 token 消耗 ⇒ 三个 token 计数一律归零；
        同时携带 ``cache_hit=True`` 与 ``cached_original_usage``，
        供成本报表回放"如果没有缓存会花多少"。

        **绝不伪造 token 数** —— 命中率可观测性由 ``cache_hit`` 标记承担
        （见 ``src/llm/usage.py::should_persist_usage_telemetry``）。
        """
        original = self.original_usage if isinstance(self.original_usage, dict) else {}
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "cache_hit": True,
            "cache_tier": self.tier,
            "cache_similarity": float(self.similarity),
            "cache_age_seconds": int(self.age_seconds),
            "cached_original_usage": original,
            "provider": str(original.get("provider") or ""),
        }

    @classmethod
    def from_entry(
        cls,
        entry: CacheEntry,
        *,
        tier: str = CACHE_TIER_EXACT,
        similarity: float = 1.0,
        now: Optional[datetime] = None,
    ) -> "CacheHit":
        """由 :class:`CacheEntry` 构造命中结果。"""
        return cls(
            response_text=entry.response_text,
            response_model=entry.response_model,
            original_usage=dict(entry.original_usage or {}),
            tier=tier,
            similarity=float(similarity),
            age_seconds=entry.age_seconds(now),
            matched_prompt_hash=entry.prompt_hash,
            partition_key=entry.partition_key,
        )


@dataclass
class CacheMiss:
    """一次未命中的归因（仅用于埋点/排障，不对外返回）。"""

    reason: str = CacheDegradeReason.NONE.value
    detail: str = ""
    candidate_count: int = 0
    top_similarity: float = 0.0

    def to_log_fields(self) -> Dict[str, Any]:
        return {
            "degrade_reason": self.reason,
            "degrade_detail": self.detail,
            "candidate_count": int(self.candidate_count),
            "top_similarity": float(self.top_similarity),
        }


# --------------------------------------------------------------------------- #
# PartitionCandidates（Tier-1 候选矩阵）
# --------------------------------------------------------------------------- #

@dataclass
class PartitionCandidates:
    """同分区候选集（Tier-1 点积检索的输入）。"""

    matrix: Optional[np.ndarray] = None
    meta: List[Dict[str, Any]] = field(default_factory=list)
    partition_key: str = ""
    total_rows: int = 0

    def is_empty(self) -> bool:
        if self.matrix is None or not self.meta:
            return True
        return int(getattr(self.matrix, "shape", (0,))[0] or 0) == 0


__all__ = [
    "SEMCACHE_MODEL_ID",
    "SEMCACHE_VECTOR_VERSION",
    "CACHE_MODE_EXACT",
    "CACHE_MODE_SEMANTIC",
    "VALID_CACHE_MODES",
    "CACHE_TIER_EXACT",
    "CACHE_TIER_SEMANTIC",
    "LOG_PREFIX",
    "DEFAULT_SEMCACHE_LOG_PATH",
    "PARTITION_FIELDS",
    "ASSERT_FIELDS",
    "PARAMS_FINGERPRINT_KEYS",
    "CacheDegradeReason",
    "CacheKey",
    "CacheEntry",
    "CacheHit",
    "CacheMiss",
    "PartitionCandidates",
    "compute_params_fingerprint",
    "normalize_trade_date_strict",
    "normalize_time_slot_strict",
]
