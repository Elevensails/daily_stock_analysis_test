# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆（语义召回层）— 数据契约与全局常量
===================================

职责：
1. 定义 `src/memory/` 全包共享的 dataclass 契约（查询 / 命中项 / 召回结果 / 待写记录 / 候选集）
2. 集中定义全局常量（向量版本、model_id 前缀、哨兵、section header），**禁止散点字面量**
3. 定义降级原因枚举 `DegradeReason`（与 PRD §6.2 严格一致）

设计约束（对齐 docs/system_design_u14_long_term_memory.md §8 Shared Knowledge）：
- 本模块**只依赖 numpy + 标准库**，不得 import 项目内任何其它模块，
  以保证 `src.storage` / `src.config` 等重模块永远可以安全地反向引用常量。
- 所有落盘向量必须已 L2 归一化（‖v‖₂ = 1.0 ± 1e-5），dtype 恒为 np.float32。
- 抽取模板 `build_conclusion_text()` 或归一化算法变更时，`VECTOR_VERSION` 必须 +1。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

# --------------------------------------------------------------------------- #
# 全局常量（唯一来源，全包统一引用）
# --------------------------------------------------------------------------- #

#: 抽取 + 向量化逻辑版本。build_conclusion_text() 模板或归一化算法变更时必须 +1。
VECTOR_VERSION: int = 1

#: 本地词法 provider 的固定 model_id
LOCAL_MODEL_ID: str = "local:lexical-v1"

#: litellm provider 的 model_id 前缀（完整形如 "litellm:openai/text-embedding-3-small"）
LITELLM_MODEL_PREFIX: str = "litellm:"

#: provider 短名（写入 RecallResult.embedding_provider，对齐 PRD §6.2）
PROVIDER_LITELLM: str = "litellm"
PROVIDER_LOCAL: str = "local"

#: 本地词法向量的默认投影维度（与 config.ltm_embedding_dim 默认值保持一致）
DEFAULT_LOCAL_DIM: int = 1024

#: litellm:openai/text-embedding-3-small 的服务端固定维度
DEFAULT_LITELLM_DIM: int = 1536

#: prompt 注入段落 header
MEMORY_SECTION_HEADER: str = "## 🧠 历史相似情境记忆"

#: 不可信文本哨兵（复刻 src/services/stock_continuity.py 范式）
SENTINEL_BEGIN: str = "BEGIN_UNTRUSTED_MEMORY_RECALL"
SENTINEL_END: str = "END_UNTRUSTED_MEMORY_RECALL"

#: prompt 注入的统一 context 键名（legacy 与 Agent 两路一致）
RECALL_SECTION_KEY: str = "ltm_recall_section"

#: 命中日志默认路径
DEFAULT_RECALL_LOG_PATH: str = "logs/memory_recall.jsonl"

#: 缺省时段（4 位 HHMM）
DEFAULT_TIME_SLOT: str = "1800"

#: 召回范围
SCOPE_SAME_STOCK: str = "same_stock"
SCOPE_GLOBAL: str = "global"
VALID_SCOPES = frozenset({SCOPE_SAME_STOCK, SCOPE_GLOBAL})

#: embedding provider 选择模式
VALID_EMBEDDING_PROVIDERS = frozenset({"auto", "litellm", "local"})

#: 写入模式
WRITE_MODE_BATCH_END: str = "batch_end"
WRITE_MODE_TEXT_ONLY: str = "text_only"
WRITE_MODE_OFF: str = "off"
VALID_WRITE_MODES = frozenset({WRITE_MODE_BATCH_END, WRITE_MODE_TEXT_ONLY, WRITE_MODE_OFF})

#: 日志前缀（对齐 "[RAG] " / "[AgentMemory] "）
LOG_PREFIX: str = "[LTM]"

#: 结论文本模板标签（固定顺序，便于跨条对齐）
CONCLUSION_LABEL_TREND: str = "[趋势]"
CONCLUSION_LABEL_ADVICE: str = "[建议]"
CONCLUSION_LABEL_SUMMARY: str = "[要点]"

_TIME_SLOT_RE = re.compile(r"^\d{4}$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


# --------------------------------------------------------------------------- #
# 降级原因枚举（与 PRD §6.2 / 设计 §8.10 严格一致）
# --------------------------------------------------------------------------- #

class DegradeReason(str, Enum):
    """召回降级原因。

    ``NONE`` 是"未降级"的内部表示，序列化到 JSON 时输出 ``null``
    （PRD §6.2 的 ``degrade_reason`` 枚举首项即为 ``null``）。
    """

    NONE = "none"
    DISABLED = "disabled"
    EMPTY_STORE = "empty_store"
    EMBED_FAILED = "embed_failed"
    MODEL_MISMATCH = "model_mismatch"
    STORE_ERROR = "store_error"
    TIMEOUT = "timeout"

    @classmethod
    def normalize(cls, value: Any) -> Optional[str]:
        """把任意输入归一化为合法的 reason 字符串；未降级返回 ``None``。

        Args:
            value: ``DegradeReason`` / ``str`` / ``None``。

        Returns:
            合法枚举值字符串；``None`` 表示未降级。未知字符串按
            ``store_error`` 兜底（保证日志字段始终落在枚举内）。
        """
        if value is None:
            return None
        if isinstance(value, DegradeReason):
            return None if value is DegradeReason.NONE else value.value
        text = str(value).strip().lower()
        if not text or text in {"none", "null"}:
            return None
        for member in cls:
            if member.value == text:
                return None if member is DegradeReason.NONE else member.value
        return DegradeReason.STORE_ERROR.value


# --------------------------------------------------------------------------- #
# 通用小工具（跨模块共享，避免散点实现）
# --------------------------------------------------------------------------- #

def normalize_time_slot(value: Any, default: str = DEFAULT_TIME_SLOT) -> str:
    """把任意输入归一化为 4 位 HHMM 字符串。

    取值链：显式入参 ▶ default ▶ ``DEFAULT_TIME_SLOT``（设计 §8.4）。
    """
    text = str(value or "").strip()
    if _TIME_SLOT_RE.match(text):
        return text
    fallback = str(default or "").strip()
    if _TIME_SLOT_RE.match(fallback):
        return fallback
    return DEFAULT_TIME_SLOT


def normalize_trade_date(value: Any, default: Optional[str] = None) -> str:
    """把 datetime / date / 字符串归一化为 ``YYYY-MM-DD``。

    无法解析时返回 ``default``（缺省为今天）。
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    matched = _DATE_RE.match(text)
    if matched:
        return f"{matched.group(1)}-{matched.group(2)}-{matched.group(3)}"
    if default:
        return str(default)
    return date.today().isoformat()


def compute_age_days(trade_date: Any, reference: Optional[date] = None) -> int:
    """计算 ``trade_date`` 距参考日的天数（下界 0，不可解析时返回 0）。"""
    normalized = normalize_trade_date(trade_date, default="")
    if not normalized:
        return 0
    try:
        parsed = date.fromisoformat(normalized)
    except (TypeError, ValueError):
        return 0
    ref = reference or date.today()
    return max(int((ref - parsed).days), 0)


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（日志时间戳统一口径）。"""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 查询契约
# --------------------------------------------------------------------------- #

@dataclass
class RecallQuery:
    """一次语义召回的输入契约（对齐 PRD §6.2 输入 schema）。

    ``query_text`` 与 ``query_embedding`` 二选一：前者由门面负责 embed，
    后者用于复用已算好的向量避免重复网络往返。
    """

    stock_code: str = ""
    query_text: str = ""
    query_embedding: Optional[np.ndarray] = None
    scope: str = SCOPE_SAME_STOCK
    top_k: int = 3
    min_similarity: float = 0.75
    lookback_days: int = 90
    time_slot: Optional[str] = None
    report_type: Optional[str] = None
    exclude_history_ids: List[int] = field(default_factory=list)
    apply_decay: bool = True
    halflife_days: int = 60

    def validate(self) -> None:
        """就地归一化并 clamp 非法值。

        **本方法永不抛异常**（设计 §8.9 fail-open 铁律）：非法输入一律夹到
        合法域，调用方用 :meth:`has_query` 判断是否具备可执行的查询条件。
        """
        self.stock_code = str(self.stock_code or "").strip()

        scope = str(self.scope or SCOPE_SAME_STOCK).strip().lower()
        self.scope = scope if scope in VALID_SCOPES else SCOPE_SAME_STOCK

        try:
            self.top_k = max(int(self.top_k), 1)
        except (TypeError, ValueError):
            self.top_k = 3

        try:
            similarity = float(self.min_similarity)
        except (TypeError, ValueError):
            similarity = 0.75
        self.min_similarity = min(max(similarity, -1.0), 1.0)

        try:
            self.lookback_days = max(int(self.lookback_days), 0)
        except (TypeError, ValueError):
            self.lookback_days = 90

        try:
            self.halflife_days = max(int(self.halflife_days), 0)
        except (TypeError, ValueError):
            self.halflife_days = 60

        slot = str(self.time_slot or "").strip()
        self.time_slot = slot if _TIME_SLOT_RE.match(slot) else None

        report_type = str(self.report_type or "").strip()
        self.report_type = report_type or None

        cleaned_ids: List[int] = []
        seen: set = set()
        for raw in list(self.exclude_history_ids or []):
            try:
                history_id = int(raw)
            except (TypeError, ValueError):
                continue
            if history_id <= 0 or history_id in seen:
                continue
            seen.add(history_id)
            cleaned_ids.append(history_id)
        self.exclude_history_ids = cleaned_ids

        self.apply_decay = bool(self.apply_decay)
        self.query_text = str(self.query_text or "")

        if self.query_embedding is not None:
            vector = np.asarray(self.query_embedding, dtype=np.float32).reshape(-1)
            self.query_embedding = vector if vector.size > 0 else None

    def has_query(self) -> bool:
        """是否具备可执行的查询条件（文本或向量至少有一个非空）。"""
        if self.query_embedding is not None and np.asarray(self.query_embedding).size > 0:
            return True
        return bool(str(self.query_text or "").strip())


# --------------------------------------------------------------------------- #
# 命中项 / 召回结果契约
# --------------------------------------------------------------------------- #

@dataclass
class RecallItem:
    """单条召回命中（对齐 PRD §6.2 输出 items 元素）。"""

    history_id: int = 0
    stock_code: str = ""
    stock_name: str = ""
    trade_date: str = ""
    time_slot: str = ""
    age_days: int = 0
    similarity: float = 0.0
    final_score: float = 0.0
    conclusion_text: str = ""
    sentiment_score: Optional[int] = None
    operation_advice: str = ""
    #: P1-1 预留：兑现情况。P0 阶段恒为 None（上游 BacktestService 数据源为空）
    outcome: Optional[Dict[str, Any]] = None

    def to_safe_dict(self) -> Dict[str, Any]:
        """转换为可直接 JSON 序列化的字典（供 AgentMemory 委托层返回）。"""
        return {
            "history_id": int(self.history_id or 0),
            "stock_code": str(self.stock_code or ""),
            "stock_name": str(self.stock_name or ""),
            "trade_date": str(self.trade_date or ""),
            "time_slot": str(self.time_slot or ""),
            "age_days": int(self.age_days or 0),
            "similarity": round(float(self.similarity or 0.0), 6),
            "final_score": round(float(self.final_score or 0.0), 6),
            "conclusion_text": str(self.conclusion_text or ""),
            "sentiment_score": self.sentiment_score,
            "operation_advice": str(self.operation_advice or ""),
            "outcome": self.outcome,
        }


@dataclass
class RecallResult:
    """一次语义召回的输出契约（对齐 PRD §6.2 输出 schema）。

    **本对象永远由 LongTermMemory 返回，任何失败路径都返回
    :meth:`empty`，绝不向 pipeline 抛异常。**
    """

    enabled: bool = False
    degraded: bool = False
    degrade_reason: Optional[str] = None
    embedding_provider: str = ""
    model_id: str = ""
    candidate_count: int = 0
    hit_count: int = 0
    elapsed_ms: float = 0.0
    items: List[RecallItem] = field(default_factory=list)
    # --- 以下为日志/渲染所需的上下文回填字段（不在 PRD items 契约内）---
    stock_code: str = ""
    scope: str = SCOPE_SAME_STOCK
    min_similarity: float = 0.0
    truncated: bool = False

    def is_empty(self) -> bool:
        """是否无任何命中。"""
        return not self.items

    def top_similarity(self) -> Optional[float]:
        """最高原始相似度；无命中时返回 ``None``。"""
        if not self.items:
            return None
        return round(float(max(item.similarity for item in self.items)), 6)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 PRD §6.2 的输出 JSON 结构。"""
        return {
            "enabled": bool(self.enabled),
            "degraded": bool(self.degraded),
            "degrade_reason": DegradeReason.normalize(self.degrade_reason),
            "embedding_provider": str(self.embedding_provider or ""),
            "model_id": str(self.model_id or ""),
            "candidate_count": int(self.candidate_count or 0),
            "hit_count": int(self.hit_count or 0),
            "elapsed_ms": round(float(self.elapsed_ms or 0.0), 3),
            "items": [item.to_safe_dict() for item in self.items],
        }

    def to_log_record(
        self,
        *,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        injected_chars: int = 0,
    ) -> Dict[str, Any]:
        """序列化为 `logs/memory_recall.jsonl` 的一行（PRD §6.3 全字段）。

        字段顺序即 PRD §6.3 列举顺序；``injected_tokens`` 按设计 §8.6 的
        字符近似口径统一改名为 ``injected_chars``。
        """
        return {
            "ts": utc_now_iso(),
            "run_id": run_id,
            "trace_id": trace_id,
            "stock_code": str(self.stock_code or ""),
            "scope": str(self.scope or SCOPE_SAME_STOCK),
            "candidate_count": int(self.candidate_count or 0),
            "hit_count": int(self.hit_count or 0),
            "top_similarity": self.top_similarity(),
            "min_similarity": round(float(self.min_similarity or 0.0), 6),
            "embedding_provider": str(self.embedding_provider or ""),
            "model_id": str(self.model_id or ""),
            "elapsed_ms": round(float(self.elapsed_ms or 0.0), 3),
            "degraded": bool(self.degraded),
            "degrade_reason": DegradeReason.normalize(self.degrade_reason),
            "injected_chars": int(injected_chars or 0),
        }

    @classmethod
    def empty(
        cls,
        reason: Any = None,
        *,
        enabled: bool = True,
        embedding_provider: str = "",
        model_id: str = "",
        stock_code: str = "",
        scope: str = SCOPE_SAME_STOCK,
        min_similarity: float = 0.0,
        candidate_count: int = 0,
        elapsed_ms: float = 0.0,
    ) -> "RecallResult":
        """构造一个空召回结果（所有降级返回值的唯一出口）。

        Args:
            reason: ``DegradeReason`` 或字符串；``None`` 表示"正常但零命中"。
        """
        normalized = DegradeReason.normalize(reason)
        return cls(
            enabled=bool(enabled),
            degraded=normalized is not None,
            degrade_reason=normalized,
            embedding_provider=str(embedding_provider or ""),
            model_id=str(model_id or ""),
            candidate_count=int(candidate_count or 0),
            hit_count=0,
            elapsed_ms=float(elapsed_ms or 0.0),
            items=[],
            stock_code=str(stock_code or ""),
            scope=str(scope or SCOPE_SAME_STOCK),
            min_similarity=float(min_similarity or 0.0),
            truncated=False,
        )


# --------------------------------------------------------------------------- #
# 写入记录契约
# --------------------------------------------------------------------------- #

@dataclass
class MemoryRecord:
    """待落盘的一条记忆（MemoryWriter 缓冲区元素）。

    ``embedding`` 为 ``None`` 表示只落文本（``ltm_write_mode="text_only"``
    或批量 embed 失败后的退化路径），向量交由 `scripts/backfill_ltm.py` 离线补。
    """

    history_id: int = 0
    code: str = ""
    name: str = ""
    report_type: str = "daily"
    trade_date: str = ""
    time_slot: str = DEFAULT_TIME_SLOT
    conclusion_text: str = ""
    text_hash: str = ""
    embedding: Optional[np.ndarray] = None
    sentiment_score: Optional[int] = None
    operation_advice: str = ""

    def __post_init__(self) -> None:
        """规范化字段并自动补齐 ``text_hash``。"""
        self.history_id = int(self.history_id or 0)
        self.code = str(self.code or "").strip()
        self.name = str(self.name or "").strip()
        self.report_type = str(self.report_type or "daily").strip() or "daily"
        self.trade_date = normalize_trade_date(self.trade_date)
        self.time_slot = normalize_time_slot(self.time_slot)
        self.conclusion_text = str(self.conclusion_text or "")
        self.operation_advice = str(self.operation_advice or "")
        if not self.text_hash:
            self.text_hash = self.compute_hash(self.conclusion_text)

    @staticmethod
    def compute_hash(text: str) -> str:
        """内容指纹：``sha256(conclusion_text)``，用于变更检测与幂等 UPSERT。"""
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    def is_valid(self) -> bool:
        """是否可入库（必须有正的 history_id 与非空结论文本）。"""
        return self.history_id > 0 and bool(self.conclusion_text.strip())


# --------------------------------------------------------------------------- #
# 候选集契约（SqliteVectorStore.load_candidates 的产出）
# --------------------------------------------------------------------------- #

@dataclass
class CandidateSet:
    """一次候选拉取的结果：向量矩阵 + 行元数据。

    ``matrix`` 形如 ``(n, dim)``、``float32``、**已 L2 归一化**（落盘前即归一化，
    检索侧禁止重复归一化，见设计 §8.1）。``meta[i]`` 与 ``matrix[i]`` 行对齐。
    """

    matrix: Optional[np.ndarray] = None
    meta: List[Dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    total_rows: int = 0
    model_id: str = ""
    vector_version: int = VECTOR_VERSION
    dim: int = 0
    #: 拉取阶段即可判定的降级原因（empty_store / model_mismatch / store_error）
    degrade_reason: Optional[str] = None

    def __len__(self) -> int:
        if self.matrix is None:
            return 0
        return int(self.matrix.shape[0])

    def is_empty(self) -> bool:
        """候选集是否为空。"""
        return len(self) == 0


def as_float32_matrix(vectors: Sequence[Any], dim: int) -> np.ndarray:
    """把任意序列安全地转换为 ``(n, dim)`` 的 float32 矩阵（形状不符时返回空矩阵）。"""
    safe_dim = max(int(dim or 0), 0)
    if not vectors:
        return np.zeros((0, safe_dim), dtype=np.float32)
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2:
        return np.zeros((0, safe_dim), dtype=np.float32)
    return matrix


__all__ = [
    "VECTOR_VERSION",
    "LOCAL_MODEL_ID",
    "LITELLM_MODEL_PREFIX",
    "PROVIDER_LITELLM",
    "PROVIDER_LOCAL",
    "DEFAULT_LOCAL_DIM",
    "DEFAULT_LITELLM_DIM",
    "MEMORY_SECTION_HEADER",
    "SENTINEL_BEGIN",
    "SENTINEL_END",
    "RECALL_SECTION_KEY",
    "DEFAULT_RECALL_LOG_PATH",
    "DEFAULT_TIME_SLOT",
    "SCOPE_SAME_STOCK",
    "SCOPE_GLOBAL",
    "VALID_SCOPES",
    "VALID_EMBEDDING_PROVIDERS",
    "WRITE_MODE_BATCH_END",
    "WRITE_MODE_TEXT_ONLY",
    "WRITE_MODE_OFF",
    "VALID_WRITE_MODES",
    "LOG_PREFIX",
    "CONCLUSION_LABEL_TREND",
    "CONCLUSION_LABEL_ADVICE",
    "CONCLUSION_LABEL_SUMMARY",
    "DegradeReason",
    "RecallQuery",
    "RecallItem",
    "RecallResult",
    "MemoryRecord",
    "CandidateSet",
    "normalize_time_slot",
    "normalize_trade_date",
    "compute_age_days",
    "utc_now_iso",
    "as_float32_matrix",
]
