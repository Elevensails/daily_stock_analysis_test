# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆 — 命中率遥测（JSONL 落盘）
===================================

职责：把每次语义召回的观测指标追加写入 ``logs/memory_recall.jsonl``，
供 `scripts/analyze_recall_log.py`（P1）与人工调阈值使用。

范式完全复刻 ``src/core/validator.py::append_reject_record()``：
1. ``Path(...).parent.mkdir(parents=True, exist_ok=True)``
2. ``open("a", encoding="utf-8")`` + ``json.dumps(..., ensure_ascii=False) + "\\n"``
3. **整体 ``try/except: pass``** —— 遥测失败绝不允许影响主流程

字段口径严格对齐 PRD §6.3；其中 ``injected_tokens`` 按设计 §8.6 的
"字符近似"口径统一记为 ``injected_chars``（本项目全链路禁用 tiktoken，
见设计 §0 A7），避免用 token 之名行 char 之实造成误读。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.memory.models import (
    DEFAULT_RECALL_LOG_PATH,
    LOG_PREFIX,
    DegradeReason,
    RecallResult,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

#: PRD §6.3 规定的字段顺序（JSONL 单行内的 key 顺序，便于 grep / 肉眼比对）
RECALL_LOG_FIELDS = (
    "ts",
    "run_id",
    "trace_id",
    "stock_code",
    "scope",
    "candidate_count",
    "hit_count",
    "top_similarity",
    "min_similarity",
    "embedding_provider",
    "model_id",
    "elapsed_ms",
    "degraded",
    "degrade_reason",
    "injected_chars",
)


def append_recall_record(
    result: Optional[RecallResult] = None,
    *,
    log_path: str = DEFAULT_RECALL_LOG_PATH,
    run_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    injected_chars: int = 0,
    **extra: Any,
) -> None:
    """把一条召回观测记录追加写入 JSONL（失败静默，不阻断主流程）。

    Args:
        result: 本次召回的 :class:`RecallResult`；``None`` 时只写骨架字段
            （用于 ltm_enabled=False 等"根本没跑召回"的场景）。
        log_path: 目标 JSONL 路径，缺省 ``logs/memory_recall.jsonl``。
        run_id: 本轮批量运行的 ID（用于把同一次运行的多只股票聚合）。
        trace_id: 单次分析的链路 ID。
        injected_chars: 实际注入 prompt 的字符数（字符近似口径，见设计 §8.6）。
        **extra: 额外字段，合并进记录但**不覆盖** PRD §6.3 核心字段。

    Notes:
        本函数**永不抛异常**。路径不可写 / 磁盘满 / 序列化失败一律吞掉，
        仅在 DEBUG 级别留一行痕迹。
    """
    try:
        if isinstance(result, RecallResult):
            record: Dict[str, Any] = result.to_log_record(
                run_id=run_id,
                trace_id=trace_id,
                injected_chars=injected_chars,
            )
        else:
            record = _empty_record(
                run_id=run_id,
                trace_id=trace_id,
                injected_chars=injected_chars,
            )

        # 合并扩展字段（核心字段优先，扩展字段不得覆盖）
        for key, value in (extra or {}).items():
            if key not in record:
                record[key] = value

        path = Path(str(log_path or DEFAULT_RECALL_LOG_PATH))
        parent = path.parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 遥测失败绝不影响主流程
        logger.debug("%s 写召回日志失败（已忽略）: %s", LOG_PREFIX, exc)


def _empty_record(
    *,
    run_id: Optional[str],
    trace_id: Optional[str],
    injected_chars: int,
) -> Dict[str, Any]:
    """构造"未执行召回"场景下的骨架记录（字段齐全，全部取零值）。"""
    return {
        "ts": utc_now_iso(),
        "run_id": run_id,
        "trace_id": trace_id,
        "stock_code": "",
        "scope": "",
        "candidate_count": 0,
        "hit_count": 0,
        "top_similarity": None,
        "min_similarity": 0.0,
        "embedding_provider": "",
        "model_id": "",
        "elapsed_ms": 0.0,
        "degraded": True,
        "degrade_reason": DegradeReason.DISABLED.value,
        "injected_chars": int(injected_chars or 0),
    }


def resolve_log_path(config: Any = None) -> str:
    """从 config 取召回日志路径（缺省 ``logs/memory_recall.jsonl``）。"""
    path = str(getattr(config, "ltm_recall_log_path", "") or "").strip()
    return path or DEFAULT_RECALL_LOG_PATH


__all__ = [
    "RECALL_LOG_FIELDS",
    "append_recall_record",
    "resolve_log_path",
]
