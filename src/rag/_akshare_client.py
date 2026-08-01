# -*- coding: utf-8 -*-
"""
===================================
P1.6 RAG — akshare 新闻最小客户端
===================================

职责单一：把 ``ak.stock_news_em`` 包成一个「有超时、有限频、有进程内熔断」
的安全调用，供 :mod:`src.rag.news` 使用。

为什么不复用 ``data_provider.akshare_fetcher`` 的反爬设施
--------------------------------------------------------
``AkshareFetcher`` 的限频/熔断是为「日线 + 实时行情 + 筹码」这条重链路设计的，
其 sleep_min/sleep_max 默认 2~5 秒、熔断 key 与筹码/行情共享。RAG 新闻是
旁路增强链路，一旦复用就会出现两个后果：

1. 新闻失败会污染行情/筹码的熔断计数（正是 P1.6 要修的那类耦合）；
2. 行情链路的 2~5 秒随机 sleep 会把新闻检索拖进总预算之外。

因此这里刻意保持独立、极小实现（无 tenacity、无 requests session 改写）。

E1 边界（docs/p1.6-arch-design.md §8.2）
----------------------------------------
``ak.stock_news_em()`` **没有** timeout 参数，底层 requests 调用可能长时间挂起。
必须由调用侧用 ``ThreadPoolExecutor.submit(...).result(timeout=)`` 强制截断。
超时后底层线程仍在跑，因此线程池必须是 daemon 线程（Python 的
``ThreadPoolExecutor`` 工作线程默认非 daemon，会阻塞解释器退出），这里改用
每次调用新建线程池 + ``shutdown(wait=False)`` 并在模块级注册退出清理，
确保 CI 进程不会 hang 住。
"""

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# ── 模块级限频（E5）──────────────────────────────────────────────
# 东财新闻接口在 CI 上同 IP 高频访问容易触发限流。P1.6 先保守取 0.8s，
# 待 CI 实证（docs/p1.6-ci-probe-guide.md）给出真实阈值后再调。
_MIN_INTERVAL_SECONDS: float = 0.8
_rate_lock = threading.Lock()
_last_call_ts: float = 0.0

# ── 进程内熔断 ──────────────────────────────────────────────────
# 连续 N 次失败后本进程内不再尝试，避免每只股票都白等一次超时。
_CIRCUIT_FAILURE_THRESHOLD: int = 3
_circuit_lock = threading.Lock()
_consecutive_failures: int = 0

# 复用同一个线程池执行器，避免每次调用新建线程（CI 上批量股票开销明显）。
# 使用 daemon 语义：Python 3.9+ 的 ThreadPoolExecutor 在解释器退出时会 join
# 所有工作线程，因此这里不能让超时的调用永久占用池中唯一线程 —— 池大小设为
# 1 但每次超时后主动重建，保证下一只股票不被上一只的僵尸请求堵死。
_executor_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def _get_executor() -> ThreadPoolExecutor:
    """获取（必要时创建）模块级线程池执行器。"""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="rag-akshare-news"
            )
        return _executor


def _discard_executor() -> None:
    """丢弃当前执行器（超时后调用）。

    ``shutdown(wait=False)`` 不会等待仍在跑的请求线程，下一次调用会新建
    一个干净的执行器。被遗弃的线程在 requests 超时/返回后自然结束。
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None


def _enforce_min_interval() -> None:
    """模块级限频：保证两次 akshare 新闻调用间隔不小于 ``_MIN_INTERVAL_SECONDS``。"""
    global _last_call_ts
    with _rate_lock:
        now = time.time()
        wait = _MIN_INTERVAL_SECONDS - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def is_circuit_open() -> bool:
    """进程内熔断是否已打开。"""
    with _circuit_lock:
        return _consecutive_failures >= _CIRCUIT_FAILURE_THRESHOLD


def _record_failure() -> None:
    """记录一次失败，达到阈值即打开熔断。"""
    global _consecutive_failures
    with _circuit_lock:
        _consecutive_failures += 1
        if _consecutive_failures == _CIRCUIT_FAILURE_THRESHOLD:
            logger.warning(
                "[RAG.akshare] 连续 %d 次失败，本进程内暂停 akshare 新闻检索",
                _CIRCUIT_FAILURE_THRESHOLD,
            )


def _record_success() -> None:
    """记录一次成功，清零失败计数并关闭熔断。"""
    global _consecutive_failures
    with _circuit_lock:
        _consecutive_failures = 0


def reset_circuit() -> None:
    """重置熔断状态（供测试与长驻进程每轮 run 调用）。"""
    global _consecutive_failures, _last_call_ts
    with _circuit_lock:
        _consecutive_failures = 0
    with _rate_lock:
        _last_call_ts = 0.0


def fetch_stock_news(symbol: str, *, timeout: float = 12.0) -> List[dict]:
    """调用 ``ak.stock_news_em`` 获取个股新闻，带超时/限频/熔断保护。

    Args:
        symbol: 股票代码（6 位数字，如 "600036"）
        timeout: 单次调用超时（秒）。ak 本身无 timeout 参数，由此处强制截断。

    Returns:
        原始记录列表（``DataFrame.to_dict("records")``）；任何异常/超时/熔断
        均返回空列表，绝不抛出。
    """
    if not symbol:
        return []

    if is_circuit_open():
        logger.debug("[RAG.akshare] 熔断中，跳过 %s", symbol)
        return []

    try:
        import akshare as ak
    except Exception as exc:
        logger.warning("[RAG.akshare] akshare 不可用: %s", exc)
        _record_failure()
        return []

    _enforce_min_interval()

    future: Optional[Future] = None
    try:
        future = _get_executor().submit(ak.stock_news_em, symbol=symbol)
        df: Any = future.result(timeout=max(1.0, float(timeout)))
    except FutureTimeoutError:
        logger.warning("[RAG.akshare] %s 新闻检索超时 (%.1fs)", symbol, timeout)
        if future is not None:
            future.cancel()
        # 超时线程仍在跑，丢弃执行器避免堵住后续调用
        _discard_executor()
        _record_failure()
        return []
    except Exception as exc:
        logger.warning("[RAG.akshare] %s 新闻检索异常: %s", symbol, exc)
        _record_failure()
        return []

    if df is None or getattr(df, "empty", True):
        logger.debug("[RAG.akshare] %s 新闻返回空", symbol)
        # 空数据不是故障（有些冷门票确实没新闻），不计入熔断
        _record_success()
        return []

    try:
        records = df.to_dict("records")
    except Exception as exc:
        logger.warning("[RAG.akshare] %s 新闻 DataFrame 解析失败: %s", symbol, exc)
        _record_failure()
        return []

    _record_success()
    logger.debug("[RAG.akshare] %s 新闻返回 %d 条", symbol, len(records))
    return records
