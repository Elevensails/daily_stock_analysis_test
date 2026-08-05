# -*- coding: utf-8 -*-
"""
===================================
U12 语义缓存 — 可缓存性判据（guards）
===================================

职责（设计 §2 N5 / T03）：
1. :meth:`CacheGuards.normalize_prompt` —— prompt 规范化（**不得抹掉语义**）
2. :meth:`CacheGuards.hash_prompt`      —— sha256 内容指纹
3. :meth:`CacheGuards.is_prompt_cacheable`   —— 敏感内容 deny-list
4. :meth:`CacheGuards.is_response_cacheable` —— 响应质量门槛

设计取舍（相对设计文档的一处保守化，见报告"偏离"章节）：
``normalize_prompt`` **不做任何"时间戳类噪声剔除"**。原因是本项目 prompt 里
的时间信息（如"截至 14:30 的分时数据"）本身就是语义的一部分，一旦被正则抹掉，
14:30 与 11:30 两份不同数据的 prompt 会算出同一个 hash —— 那是在 Tier-0
这条"零误命中风险"的路径上人为制造误命中。这里只做**幂等的空白归一**。
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable, List, Optional, Pattern, Tuple

from src.cache.models import LOG_PREFIX

logger = logging.getLogger(__name__)

#: 响应最小长度默认门槛（字符）
DEFAULT_MIN_RESPONSE_CHARS: int = 200

#: 敏感内容 deny-list 默认规则（对齐铁律 #6 / 设计 Q7）。
#: 命中任意一条 ⇒ 该 prompt **既不查缓存也不写缓存**。
#: 口径保守：只拦"明确出现账户资金类字面"的 prompt；若实际 prompt 只以
#: 「代码 + 名称」形式注入持仓，本清单不会产生任何误伤。
DEFAULT_DENY_PATTERNS: Tuple[str, ...] = (
    r"持仓金额",
    r"持仓市值",
    r"可用余额",
    r"可用资金",
    r"账户余额",
    r"账户资产",
    r"总资产",
    r"资金账号",
    r"account[_\s-]*balance",
    r"available[_\s-]*(?:cash|balance|funds)",
    r"portfolio[_\s-]*value",
)

#: 疑似错误页 / 截断响应的特征（**必须整体短小**才判定，避免误伤正文提到
#: "rate limit" 的正常分析文本）
_ERROR_MARKERS: Tuple[str, ...] = (
    "rate limit",
    "too many requests",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "upstream error",
    "请求过于频繁",
    "服务暂时不可用",
    "invalid api key",
    "insufficient balance",
)

#: 判定"疑似错误页"的长度上限（超过此长度即认为是正常长文，不再按错误页处理）
_ERROR_PAGE_MAX_CHARS: int = 400

#: 连续空行压缩上限
_BLANK_RUN_RE = re.compile(r"\n{3,}")
#: 行尾空白
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


class CacheGuards:
    """可缓存性判据集合。所有方法**永不抛异常**。"""

    def __init__(
        self,
        min_response_chars: int = DEFAULT_MIN_RESPONSE_CHARS,
        deny_patterns: Optional[Iterable[str]] = None,
    ) -> None:
        """
        Args:
            min_response_chars: 响应最小长度门槛，``<= 0`` 表示不设门槛。
            deny_patterns: 敏感内容正则列表；``None`` 时用
                :data:`DEFAULT_DENY_PATTERNS`。
        """
        try:
            self.min_response_chars = max(int(min_response_chars), 0)
        except (TypeError, ValueError):
            self.min_response_chars = DEFAULT_MIN_RESPONSE_CHARS
        sources = list(deny_patterns) if deny_patterns is not None else list(DEFAULT_DENY_PATTERNS)
        self.deny_patterns: List[Pattern[str]] = []
        for raw in sources:
            try:
                self.deny_patterns.append(re.compile(str(raw), re.IGNORECASE))
            except re.error as exc:
                logger.warning("%s 非法 deny-list 正则已跳过: %r (%s)", LOG_PREFIX, raw, exc)

    # ------------------------------------------------------------------ #
    # 规范化 / 指纹
    # ------------------------------------------------------------------ #

    @staticmethod
    def normalize_prompt(text: Optional[str]) -> str:
        """幂等的空白归一化。

        规则（**只碰空白，不碰任何有语义的字符**）：
        1. ``\\r\\n`` / ``\\r`` → ``\\n``
        2. 去除每行行尾空白
        3. 3 个以上连续换行压成 2 个
        4. 首尾整体 strip

        Returns:
            规范化后的文本；输入为 ``None`` 时返回空串。
        """
        if text is None:
            return ""
        try:
            normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
            normalized = _TRAILING_WS_RE.sub("\n", normalized)
            normalized = _BLANK_RUN_RE.sub("\n\n", normalized)
            return normalized.strip()
        except Exception:
            return str(text or "")

    @classmethod
    def hash_prompt(cls, prompt: Optional[str], system_prompt: Optional[str] = None) -> str:
        """内容指纹 = ``sha256(normalize(prompt) + "\\x00" + normalize(system))``。

        ``\\x00`` 分隔防止 ``("ab", "c")`` 与 ``("a", "bc")`` 拼接歧义。
        """
        try:
            body = cls.normalize_prompt(prompt)
            system = cls.normalize_prompt(system_prompt)
            payload = body + "\x00" + system
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # 判据
    # ------------------------------------------------------------------ #

    def is_prompt_cacheable(self, prompt: Optional[str]) -> Tuple[bool, str]:
        """prompt 是否允许进入缓存链路。

        Returns:
            ``(True, "")`` 允许；``(False, reason)`` 拒绝。
        """
        try:
            text = str(prompt or "")
            if not text.strip():
                return False, "empty_prompt"
            for pattern in self.deny_patterns:
                if pattern.search(text):
                    return False, "sensitive_content"
            return True, ""
        except Exception as exc:
            logger.debug("%s is_prompt_cacheable 异常，按不可缓存处理: %s", LOG_PREFIX, exc)
            return False, "guard_error"

    def is_response_cacheable(self, text: Optional[str]) -> Tuple[bool, str]:
        """响应是否值得写入缓存。

        拒绝条件：
        1. 空 / 全空白
        2. 长度低于 ``min_response_chars``
        3. 短文本且命中错误页特征（防止把 429/5xx 错误文案缓存 12 小时）

        Returns:
            ``(True, "")`` 允许；``(False, reason)`` 拒绝。
        """
        try:
            body = str(text or "").strip()
            if not body:
                return False, "empty_response"
            if self.min_response_chars > 0 and len(body) < self.min_response_chars:
                return False, "too_short"
            if len(body) <= _ERROR_PAGE_MAX_CHARS:
                lowered = body.lower()
                for marker in _ERROR_MARKERS:
                    if marker in lowered:
                        return False, "suspected_error_page"
            return True, ""
        except Exception as exc:
            logger.debug("%s is_response_cacheable 异常，按不可缓存处理: %s", LOG_PREFIX, exc)
            return False, "guard_error"


__all__ = [
    "CacheGuards",
    "DEFAULT_DENY_PATTERNS",
    "DEFAULT_MIN_RESPONSE_CHARS",
]
