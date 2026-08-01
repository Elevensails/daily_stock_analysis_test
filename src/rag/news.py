# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — 新闻检索子模块（P1.6 CI 化改造）
===================================

策略链（串行短路，命中即停）：
    neodata CLI（本地首选）→ akshare stock_news_em（CI 首选）
    → search_service（兜底）→ 空 block

P1.6 变更点（docs/p1.6-arch-design.md §3.4 / §3.5）
---------------------------------------------------
1. 去掉硬编码的 neodata 脚本绝对路径 —— 改为 config/env 驱动 + 候选路径探测；
   CI 上探测不到属于**正常路径**，直接降级，不打 warning。
2. 新增 akshare ``stock_news_em`` 作为 CI 可用的新闻源（无需凭证、无需代理）。
3. 新增 :func:`retrieve_news_ex`，返回带 trace 的 :class:`NewsRetrievalResult`，
   让上层 ``SourceTrace.actual_source`` 不再靠"字符串里有没有 search_service"猜。
4. 串行短路 + 总时间预算：默认命中 ≥ ``rag_news_min_items`` 条即停；
   ``rag_news_merge_sources=True`` 时退回旧的多源合并行为（保留退路）。

去重：标题前 N 字符去重；截断：最多 5 条，总计 ≤600 chars。
"""

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._akshare_client import fetch_stock_news, is_circuit_open

logger = logging.getLogger(__name__)

# neodata query.py 候选路径（按序探测）。
# 注意：这里**不再**硬编码任何开发者机器的用户名路径（P1.1 遗留问题），
# 只保留与用户无关的相对/家目录形式。真正的绝对路径请通过
# ``RAG_NEODATA_SCRIPT_PATH`` 或 config.yaml rag.neodata_script_path 指定。
_NEODATA_RELATIVE_CANDIDATES = (
    Path(".workbuddy/skills/neodata-financial-search/scripts/query.py"),
    Path(".claude/skills/neodata-financial-search/scripts/query.py"),
)

# 新闻源标识（唯一真源，禁止在别处写裸字符串）
SOURCE_NEODATA = "neodata"
SOURCE_AKSHARE_EM = "akshare_em"
SOURCE_SEARCH_SERVICE = "search_service"
SOURCE_NONE = "none"


@dataclass
class NewsRetrievalResult:
    """新闻检索结果 + 链路追踪。

    Attributes:
        block: 已格式化的 markdown 新闻块（无数据时为 ""）
        hit_source: 最终命中的数据源标识（无命中为 ``none``）
        attempted: 本次实际尝试过的数据源顺序列表
        counts: 每个数据源返回的条目数
        elapsed_ms: 整条新闻链路耗时（毫秒）
        budget_exceeded: 是否因超出总时间预算而提前放弃后续数据源
        item_count: 去重后进入 block 的条目数
    """

    block: str = ""
    hit_source: str = SOURCE_NONE
    attempted: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    budget_exceeded: bool = False
    item_count: int = 0

    @property
    def success(self) -> bool:
        """是否取到了可用的新闻内容。"""
        return bool(self.block.strip())

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的诊断字典（不含 block 正文）。"""
        return {
            "hit_source": self.hit_source,
            "attempted": list(self.attempted),
            "counts": dict(self.counts),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "budget_exceeded": self.budget_exceeded,
            "item_count": self.item_count,
            "success": self.success,
        }


def _resolve_neodata_script(config: Any = None) -> Optional[Path]:
    """解析 neodata query.py 路径（config/env 驱动，无硬编码用户路径）。

    探测顺序：
    1. ``config.rag_neodata_script_path``
    2. 环境变量 ``RAG_NEODATA_SCRIPT_PATH``
    3. 家目录下的候选相对路径

    Args:
        config: Config 实例，可为 None

    Returns:
        存在的脚本路径；全部探测不到返回 None（CI 上的正常情况）
    """
    explicit = ""
    if config is not None:
        explicit = str(getattr(config, "rag_neodata_script_path", "") or "").strip()
    if not explicit:
        explicit = str(os.getenv("RAG_NEODATA_SCRIPT_PATH", "") or "").strip()

    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate
        logger.debug("[RAG.news] 配置的 neodata 脚本路径不存在: %s", candidate)

    try:
        home = Path.home()
    except Exception:  # 某些容器环境无 HOME
        return None

    for relative in _NEODATA_RELATIVE_CANDIDATES:
        candidate = home / relative
        if candidate.exists():
            return candidate
    return None


def retrieve_news(
    code: str,
    stock_name: str,
    *,
    search_service: Any = None,
    config: Any = None,
) -> str:
    """检索近期新闻动态（向后兼容 shim）。

    P1.6 起真实逻辑迁移至 :func:`retrieve_news_ex`，本函数仅丢弃 trace。

    Args:
        code: 股票代码
        stock_name: 股票名称
        search_service: 搜索服务实例（用于回退）
        config: Config 实例

    Returns:
        格式化为 markdown 列表的新闻数据字符串（≤600 chars，最多 5 条），失败返回 ""
    """
    return retrieve_news_ex(
        code, stock_name, search_service=search_service, config=config
    ).block


def retrieve_news_ex(
    code: str,
    stock_name: str,
    *,
    search_service: Any = None,
    config: Any = None,
) -> NewsRetrievalResult:
    """检索近期新闻动态，返回带链路追踪的结果对象。

    串行短路：按 neodata → akshare_em → search_service 顺序尝试，
    任一源返回条目数 ≥ ``rag_news_min_items`` 即停止，避免无谓的网络往返。
    ``config.rag_news_merge_sources=True`` 时退回"全部尝试并合并"的旧行为。

    Args:
        code: 股票代码
        stock_name: 股票名称
        search_service: 搜索服务实例（用于回退）
        config: Config 实例

    Returns:
        NewsRetrievalResult，永不返回 None、永不抛异常
    """
    started = time.time()
    result = NewsRetrievalResult()

    neodata_timeout = _cfg_float(config, "rag_neodata_timeout_seconds", 8.0)
    dedup_chars = _cfg_int(config, "rag_news_dedup_title_chars", 20)
    merge_sources = bool(getattr(config, "rag_news_merge_sources", False)) if config else False
    min_items = _cfg_int(config, "rag_news_min_items", 3)
    total_budget = _cfg_float(config, "rag_news_total_budget_seconds", 25.0)
    akshare_enabled = (
        bool(getattr(config, "rag_akshare_news_enabled", True)) if config else True
    )

    collected: List[dict] = []

    def _remaining_budget() -> float:
        return total_budget - (time.time() - started)

    def _try_source(source_name: str, fetcher) -> bool:
        """执行一个数据源，返回是否应该短路停止。"""
        if _remaining_budget() <= 0:
            result.budget_exceeded = True
            logger.info(
                "[RAG.news] %s 新闻链路超出总预算 %.1fs，跳过 %s",
                code, total_budget, source_name,
            )
            return True

        result.attempted.append(source_name)
        try:
            items = fetcher() or []
        except Exception as exc:  # 单源异常绝不冒泡
            logger.warning("[RAG.news] %s 数据源异常: %s", source_name, exc)
            items = []

        result.counts[source_name] = len(items)
        if not items:
            return False

        collected.extend(items)
        if result.hit_source == SOURCE_NONE:
            result.hit_source = source_name

        # 串行短路：够用就停
        if not merge_sources and len(collected) >= min_items:
            return True
        return False

    # ── 1. neodata CLI（本地首选，CI 上大概率探测不到 → 静默降级）──
    stop = _try_source(
        SOURCE_NEODATA,
        lambda: _neodata_news(code, stock_name, timeout=neodata_timeout, config=config),
    )

    # ── 2. akshare stock_news_em（CI 首选：无凭证、无代理即可用）──
    if not stop and akshare_enabled and not is_circuit_open():
        stop = _try_source(
            SOURCE_AKSHARE_EM,
            lambda: _akshare_news(code, config=config),
        )
    elif not stop and akshare_enabled and is_circuit_open():
        logger.debug("[RAG.news] akshare 新闻熔断中，跳过")

    # ── 3. search_service（兜底，需要外部 API）──
    if not stop:
        _try_source(
            SOURCE_SEARCH_SERVICE,
            lambda: _search_service_fallback(code, stock_name, search_service),
        )

    result.elapsed_ms = (time.time() - started) * 1000.0

    if not collected:
        result.hit_source = SOURCE_NONE
        logger.warning(
            "[RAG.news] %s 新闻检索全部数据源无结果 (尝试: %s, 耗时 %.0fms)",
            code, ", ".join(result.attempted) or "-", result.elapsed_ms,
        )
        return result

    deduped = _deduplicate_news(collected, prefix_chars=dedup_chars)
    result.block = _format_news_block(deduped)
    result.item_count = min(len(deduped), 5)
    logger.info(
        "[RAG.news] %s 新闻命中 source=%s items=%d 耗时 %.0fms",
        code, result.hit_source, result.item_count, result.elapsed_ms,
    )
    return result


def _cfg_float(config: Any, name: str, default: float) -> float:
    """安全读取 config 上的 float 配置项。"""
    if config is None:
        return default
    try:
        value = getattr(config, name, None)
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _cfg_int(config: Any, name: str, default: int) -> int:
    """安全读取 config 上的 int 配置项。"""
    if config is None:
        return default
    try:
        value = getattr(config, name, None)
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _neodata_news(
    code: str,
    stock_name: str,
    *,
    timeout: float = 8.0,
    config: Any = None,
) -> list:
    """通过 neodata CLI 检索新闻。

    Args:
        code: 股票代码
        stock_name: 股票名称
        timeout: 子进程超时（秒）
        config: Config 实例（用于解析脚本路径）

    Returns:
        [{"title": ..., "summary": ..., "date": ..., "source": ...}, ...]
    """
    script_path = _resolve_neodata_script(config)
    if not script_path:
        # CI / 纯服务器环境没有 neodata 是预期内的，用 debug 而非 warning
        logger.debug("[RAG.news] neodata 脚本未配置或不存在，跳过该数据源")
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
                "source": SOURCE_NEODATA,
            })

        return items

    except Exception as exc:
        logger.warning("[RAG.news] 解析 neodata 新闻失败: %s", exc)
        return []


def _normalize_news_symbol(code: str) -> str:
    """把各种形态的代码归一为 ``stock_news_em`` 需要的 6 位数字。

    支持 ``600036`` / ``sh600036`` / ``600036.SH`` / ``SH.600036`` 等写法。

    Args:
        code: 原始股票代码

    Returns:
        6 位数字代码；无法归一时返回 ""（调用方直接跳过该源）
    """
    raw = str(code or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 6:
        return digits[:6]
    return ""


def _parse_news_date(value: Any) -> str:
    """解析东财新闻的「发布时间」字段。

    E2 边界（docs/p1.6-arch-design.md §8.2）：东财返回的是
    ``2024-05-20 15:30:00`` 这样的完整时间戳，日期部分固定为前 10 位，
    直接切片即可，不要用 ``split(" ")``（部分记录用的是 ``T`` 分隔）。

    Args:
        value: 原始「发布时间」值

    Returns:
        ``YYYY-MM-DD`` 形式的日期串；无法解析时返回 ""
    """
    text = str(value or "").strip()
    if len(text) < 10:
        return ""
    candidate = text[:10]
    if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", candidate):
        return candidate.replace("/", "-")
    return ""


def _is_within_lookback(date_str: str, lookback_days: int) -> bool:
    """判断日期是否落在回溯窗口内。日期为空时保守保留。"""
    if not date_str:
        return True
    try:
        published = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return True
    return published >= datetime.now() - timedelta(days=max(1, lookback_days))


def _akshare_news(code: str, *, config: Any = None) -> list:
    """通过 akshare ``stock_news_em`` 检索个股新闻（CI 首选源）。

    东财返回列关键字段：``关键词`` / ``新闻标题`` / ``新闻内容`` /
    ``发布时间`` / ``文章来源`` / ``新闻链接``。

    Args:
        code: 股票代码（任意形态，内部归一为 6 位数字）
        config: Config 实例

    Returns:
        [{"title": ..., "summary": ..., "date": ..., "source": ...}, ...]
    """
    symbol = _normalize_news_symbol(code)
    if not symbol:
        logger.debug("[RAG.news] %s 无法归一为 6 位代码，跳过 akshare 新闻", code)
        return []

    timeout = _cfg_float(config, "rag_akshare_news_timeout_seconds", 12.0)
    max_items = _cfg_int(config, "rag_akshare_news_max_items", 10)
    lookback_days = _cfg_int(config, "rag_akshare_news_lookback_days", 7)
    max_retries = _cfg_int(config, "rag_akshare_news_max_retries", 1)

    records: list = []
    for attempt in range(max(1, max_retries + 1)):
        records = fetch_stock_news(symbol, timeout=timeout)
        if records:
            break
        if is_circuit_open():
            break

    if not records:
        return []

    items: list = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = str(record.get("新闻标题", "") or "").strip()
        if not title:
            continue
        date_str = _parse_news_date(record.get("发布时间"))
        if not _is_within_lookback(date_str, lookback_days):
            continue
        summary = str(record.get("新闻内容", "") or "").strip() or title
        items.append({
            "title": title[:80],
            "summary": summary[:200],
            "date": date_str,
            "source": SOURCE_AKSHARE_EM,
        })
        if len(items) >= max_items:
            break

    return items


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
                    "source": SOURCE_SEARCH_SERVICE,
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
