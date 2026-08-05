# -*- coding: utf-8 -*-
"""
===================================
U12 语义缓存 — 命中率遥测（JSONL 落盘）
===================================

职责：把每次缓存 get/put 的观测指标追加写入 ``logs/semantic_cache.jsonl``，
供命中率评估、分区拒绝数监控与阈值调参使用（T05 运维验收）。

范式完全复刻 ``src/memory/telemetry.py``：
1. ``Path(...).parent.mkdir(parents=True, exist_ok=True)``
2. ``open("a", encoding="utf-8")`` + ``json.dumps(..., ensure_ascii=False) + "\\n"``
3. **整体 ``try/except: pass``** —— 遥测失败绝不允许影响主流程

脱敏铁律（设计 §10.2）：JSONL **只记 prompt 的 hash 与长度，不记原文**。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.cache.models import (
    DEFAULT_SEMCACHE_LOG_PATH,
    LOG_PREFIX,
    CacheDegradeReason,
    CacheHit,
    CacheKey,
)

logger = logging.getLogger(__name__)

#: JSONL 单行内的 key 顺序（便于 grep / 肉眼比对）
SEMCACHE_LOG_FIELDS = (
    "ts",
    "event",
    "mode",
    "hit",
    "tier",
    "similarity",
    "age_seconds",
    "code",
    "trade_date",
    "time_slot",
    "report_type",
    "llm_model",
    "partition_key",
    "prompt_hash",
    "prompt_chars",
    "response_chars",
    "candidate_count",
    "degrade_reason",
    "elapsed_ms",
)


def utc_now_iso() -> str:
    """UTC ISO8601 时间戳（秒级，``Z`` 结尾）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_cache_record(
    *,
    event: str,
    log_path: str = DEFAULT_SEMCACHE_LOG_PATH,
    key: Optional[CacheKey] = None,
    hit: Optional[CacheHit] = None,
    mode: str = "exact",
    prompt_chars: int = 0,
    response_chars: int = 0,
    candidate_count: int = 0,
    degrade_reason: str = CacheDegradeReason.NONE.value,
    elapsed_ms: float = 0.0,
    **extra: Any,
) -> None:
    """把一条缓存观测记录追加写入 JSONL。

    Args:
        event: ``get`` / ``put`` / ``partition_reject``。
        log_path: 目标 JSONL 路径。空串表示**关闭埋点**。
        key: 本次请求的分区键（脱敏后写入维度字段）。
        hit: 命中结果；``None`` 表示未命中。
        mode: 当前缓存模式（``exact`` / ``semantic``）。
        prompt_chars: prompt 字符数（只记长度，不记原文）。
        response_chars: 响应字符数。
        candidate_count: Tier-1 候选数。
        degrade_reason: 降级归因。
        elapsed_ms: 本次操作耗时（毫秒）。
        **extra: 额外字段，合并进记录但**不覆盖**核心字段。

    Notes:
        本函数**永不抛异常**。
    """
    try:
        target = str(log_path or "").strip()
        if not target:
            return

        record: Dict[str, Any] = {name: None for name in SEMCACHE_LOG_FIELDS}
        record.update(
            {
                "ts": utc_now_iso(),
                "event": str(event or ""),
                "mode": str(mode or ""),
                "hit": bool(hit is not None),
                "tier": hit.tier if hit is not None else None,
                "similarity": float(hit.similarity) if hit is not None else None,
                "age_seconds": int(hit.age_seconds) if hit is not None else None,
                "prompt_chars": int(prompt_chars or 0),
                "response_chars": int(response_chars or 0),
                "candidate_count": int(candidate_count or 0),
                "degrade_reason": str(degrade_reason or CacheDegradeReason.NONE.value),
                "elapsed_ms": round(float(elapsed_ms or 0.0), 3),
            }
        )
        if key is not None:
            fields = key.to_log_fields()
            record.update(
                {
                    "code": fields.get("code"),
                    "trade_date": fields.get("trade_date"),
                    "time_slot": fields.get("time_slot"),
                    "report_type": fields.get("report_type"),
                    "llm_model": fields.get("llm_model"),
                    "partition_key": fields.get("partition_key"),
                    "prompt_hash": fields.get("prompt_hash"),
                }
            )
        for name, value in (extra or {}).items():
            if name not in record:
                record[name] = value

        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover - 埋点失败不影响主流程
        logger.debug("%s 埋点写入失败（已忽略）: %s", LOG_PREFIX, exc)


__all__ = ["append_cache_record", "SEMCACHE_LOG_FIELDS", "utc_now_iso"]
