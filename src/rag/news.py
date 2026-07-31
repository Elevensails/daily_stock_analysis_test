# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — 新闻检索子模块
===================================

策略链：neodata CLI（首选）→ search_service（回退）→ 空 block

- neodata：通过 subprocess 调用 query.py，查询近 7 日新闻公告
- search_service：复用 search_comprehensive_intel() 的 latest_news + risk_check
- 去重：标题前 20 字符去重
- 截断：最多 5 条，总计 ≤600 chars
"""

import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# neodata query.py 脚本路径
_NEODATA_SCRIPT_PATH = Path(
    "C:/Users/29096/.workbuddy/skills/neodata-financial-search/scripts/query.py"
)


def _resolve_neodata_script() -> Optional[Path]:
    """解析 neodata query.py 路径。"""
    if _NEODATA_SCRIPT_PATH.exists():
        return _NEODATA_SCRIPT_PATH
    alt = Path.home() / ".workbuddy/skills/neodata-financial-search/scripts/query.py"
    if alt.exists():
        return alt
    return None


def retrieve_news(
    code: str,
    stock_name: str,
    *,
    search_service: Any = None,
    config: Any = None,
) -> str:
    """检索近期新闻动态。

    首选 neodata CLI，失败回退 search_service。

    Args:
        code: 股票代码
        stock_name: 股票名称
        search_service: 搜索服务实例（用于回退）
        config: Config 实例

    Returns:
        格式化为 markdown 列表的新闻数据字符串（≤600 chars，最多 5 条），失败返回 ""
    """
    timeout = getattr(config, 'rag_neodata_timeout_seconds', 8.0) if config else 8.0
    dedup_chars = getattr(config, 'rag_news_dedup_title_chars', 20) if config else 20

    news_items: list = []

    # ── 首选：neodata CLI ──
    neodata_items = _neodata_news(code, stock_name, timeout=timeout)
    if neodata_items:
        news_items.extend(neodata_items)

    # ── 回退/合并：search_service ──
    ss_items = _search_service_fallback(code, stock_name, search_service)
    if ss_items:
        news_items.extend(ss_items)

    if not news_items:
        logger.warning("[RAG.news] 新闻检索全部数据源失败，返回空 block")
        return ""

    # 去重（标题前 N 字符）并截断
    deduped = _deduplicate_news(news_items, prefix_chars=dedup_chars)
    return _format_news_block(deduped)


def _neodata_news(code: str, stock_name: str, *, timeout: float = 8.0) -> list:
    """通过 neodata CLI 检索新闻。

    Returns:
        [{"title": ..., "summary": ..., "date": ..., "source": ...}, ...]
    """
    script_path = _resolve_neodata_script()
    if not script_path:
        logger.debug("[RAG.news] neodata 脚本不存在")
        return []

    query = f"{stock_name} 公告 新闻 近7日"
    cmd = [
        sys.executable, str(script_path),
        "--query", query,
    ]

    try:
        logger.debug("[RAG.news] 调用 neodata: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            logger.warning("[RAG.news] neodata 返回非零: rc=%s", result.returncode)
            return []

        raw = result.stdout.strip()
        if not raw:
            return []

        return _parse_neodata_news(raw)

    except subprocess.TimeoutExpired:
        logger.warning("[RAG.news] neodata 超时 (%.1fs)", timeout)
        return []
    except Exception as exc:
        logger.warning("[RAG.news] neodata 调用异常: %s", exc)
        return []


def _parse_neodata_news(raw: str) -> list:
    """解析 neodata 返回文本，提取新闻条目。

    neodata 返回自然语言文本，尝试按行或段落拆分。
    """
    items: list = []
    try:
        # 按行拆分，过滤空行
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        for line in lines[:10]:  # 最多取前 10 行
            # 尝试提取标题（取前 80 字符）
            title = line[:80].strip()
            if len(title) < 5:
                continue
            # 尝试提取日期
            date_match = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', line)
            date_str = date_match.group(1) if date_match else ""
            items.append({
                "title": title,
                "summary": line[:200].strip(),
                "date": date_str,
                "source": "neodata",
            })

        return items

    except Exception as exc:
        logger.warning("[RAG.news] 解析 neodata 新闻失败: %s", exc)
        return []


def _search_service_fallback(
    code: str,
    stock_name: str,
    search_service: Any = None,
) -> list:
    """通过 search_service 获取新闻作为回退。

    Returns:
        [{"title": ..., "summary": ..., "date": ..., "source": ...}, ...]
    """
    if search_service is None:
        logger.debug("[RAG.news] search_service 不可用")
        return []

    try:
        if not hasattr(search_service, 'search_comprehensive_intel'):
            return []

        intel_results = search_service.search_comprehensive_intel(
            stock_code=code,
            stock_name=stock_name,
            max_searches=2,
        )
        if not intel_results:
            return []

        items: list = []
        for dim_name, response in intel_results.items():
            if not response or not response.success or not response.results:
                continue
            for r in response.results[:3]:  # 每维度最多 3 条
                title = getattr(r, 'title', '') or str(r)[:80]
                summary = getattr(r, 'snippet', '') or getattr(r, 'content', '') or ''
                if isinstance(summary, str) and len(summary) > 200:
                    summary = summary[:200]
                date_str = getattr(r, 'date', '') or getattr(r, 'published', '') or ''
                items.append({
                    "title": str(title)[:80].strip(),
                    "summary": str(summary)[:200].strip() if summary else str(title)[:100].strip(),
                    "date": str(date_str).strip(),
                    "source": "search_service",
                })

        return items

    except Exception as exc:
        logger.warning("[RAG.news] search_service 回退失败: %s", exc)
        return []


def _deduplicate_news(items: list, prefix_chars: int = 20) -> list:
    """按标题前 N 字符去重，保留首次出现的条目。"""
    seen: set = set()
    deduped: list = []
    for item in items:
        title = item.get("title", "")
        prefix = title[:prefix_chars].strip()
        if prefix and prefix not in seen:
            seen.add(prefix)
            deduped.append(item)
    return deduped


def _format_news_block(items: list) -> str:
    """格式化新闻条目为 markdown 列表（最多 5 条，≤600 chars）。"""
    header = "### 近期动态\n"
    lines: list = []

    for item in items[:5]:
        title = item.get("title", "")[:60].strip()
        date_str = item.get("date", "")
        prefix = f"- {date_str} " if date_str else "- "
        line = f"{prefix}{title}"
        lines.append(line)

    if not lines:
        return ""

    result = header + "\n".join(lines)

    # 截断到 600 字符
    if len(result) > 600:
        # 尝试去掉最后一条
        shortened_lines = lines[:-1]
        result = header + "\n".join(shortened_lines)
        if len(result) > 600:
            result = result[:597] + "..."

    return result
