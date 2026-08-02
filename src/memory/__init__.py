# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆（语义召回层）包
===================================

职责：
1. 把历史分析结论抽取为规范化文本并向量化，落盘到 `analysis_memory_vector`
2. 在新一次分析前，按语义相似度召回历史相似情境，渲染为有界 prompt 段落
3. 全链路 fail-open：任何一层故障都降级为"空召回"，绝不阻断主链路

分层（见 docs/system_design_u14_long_term_memory.md §1.3）：
- ``models``             — dataclass 契约 + 全局常量（轻量，只依赖 numpy）
- ``embedding_provider`` — litellm 主路 / 本地词法兜底，两级降级链
- ``vector_store``       — sqlite BLOB 存储 + numpy 余弦 TopK + 进程内 LRU
- ``writer``             — 结论抽取 + 内存缓冲 + run 末尾批量落盘
- ``telemetry``          — logs/memory_recall.jsonl 命中日志

导入策略：
    ``models`` 与 ``telemetry`` 之外的子模块采用 **PEP 562 惰性导入**，
    避免 ``import src.memory`` 时连带拉起 ``src.storage`` / ``litellm``
    等重依赖，也彻底杜绝 ``src.storage`` ↔ ``src.memory`` 的循环导入。
"""

from __future__ import annotations

import importlib
from typing import Any

from src.memory.models import (
    DEFAULT_LOCAL_DIM,
    DEFAULT_RECALL_LOG_PATH,
    LOCAL_MODEL_ID,
    LOG_PREFIX,
    MEMORY_SECTION_HEADER,
    RECALL_SECTION_KEY,
    SCOPE_GLOBAL,
    SCOPE_SAME_STOCK,
    SENTINEL_BEGIN,
    SENTINEL_END,
    VECTOR_VERSION,
    CandidateSet,
    DegradeReason,
    MemoryRecord,
    RecallItem,
    RecallQuery,
    RecallResult,
)

#: 惰性导出表：属性名 → 所在子模块
_LAZY_EXPORTS = {
    "EmbeddingProvider": "src.memory.embedding_provider",
    "LiteLLMEmbeddingProvider": "src.memory.embedding_provider",
    "LocalLexicalEmbeddingProvider": "src.memory.embedding_provider",
    "build_embedding_provider": "src.memory.embedding_provider",
    "build_local_provider": "src.memory.embedding_provider",
    "probe_embedding_endpoint": "src.memory.embedding_provider",
    "SqliteVectorStore": "src.memory.vector_store",
    "build_vector_store": "src.memory.vector_store",
    "apply_time_decay": "src.memory.vector_store",
    "encode_vector": "src.memory.vector_store",
    "decode_vector": "src.memory.vector_store",
    "MemoryWriter": "src.memory.writer",
    "build_memory_writer": "src.memory.writer",
    "append_recall_record": "src.memory.telemetry",
    "resolve_log_path": "src.memory.telemetry",
}


def __getattr__(name: str) -> Any:
    """PEP 562 惰性属性解析：首次访问时才导入对应子模块。"""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module 'src.memory' has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value  # 缓存，后续访问零开销
    return value


def __dir__() -> list:
    """让 dir(src.memory) 也能看见惰性导出项。"""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    # 契约与常量（即时导出）
    "VECTOR_VERSION",
    "LOCAL_MODEL_ID",
    "LOG_PREFIX",
    "MEMORY_SECTION_HEADER",
    "RECALL_SECTION_KEY",
    "SENTINEL_BEGIN",
    "SENTINEL_END",
    "DEFAULT_LOCAL_DIM",
    "DEFAULT_RECALL_LOG_PATH",
    "SCOPE_SAME_STOCK",
    "SCOPE_GLOBAL",
    "DegradeReason",
    "RecallQuery",
    "RecallItem",
    "RecallResult",
    "MemoryRecord",
    "CandidateSet",
    # 惰性导出
    "EmbeddingProvider",
    "LiteLLMEmbeddingProvider",
    "LocalLexicalEmbeddingProvider",
    "build_embedding_provider",
    "build_local_provider",
    "probe_embedding_endpoint",
    "SqliteVectorStore",
    "build_vector_store",
    "apply_time_decay",
    "encode_vector",
    "decode_vector",
    "MemoryWriter",
    "build_memory_writer",
    "append_recall_record",
    "resolve_log_path",
]
