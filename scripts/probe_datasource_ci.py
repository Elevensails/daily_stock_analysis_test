# -*- coding: utf-8 -*-
"""
==========================================================
P1.6 — 数据源 CI 实证探测脚本（CI canary）
==========================================================

目的
----
在不依赖任何业务代码（data_provider / src）的前提下，独立探测 dsa-test 依赖的
两个外部数据源在「真实网络（CI 有网 / 本地开代理）」环境下的可达性：

  1. 筹码分布（akshare.stock_cyq_em）—— 探测 600036 / 603823
  2. 个股新闻（akshare.stock_news_em）—— 探测 600036
  3. 裸 requests 直连东方财富（quote / news 两个后端）—— 验证底层通道

设计铁律
--------
- **零业务依赖**：本脚本只使用标准库 + akshare + requests，绝不 import 项目的
  data_provider / src 模块。这样即便业务代码重构，canary 依然可用。
- **永不非零退出**：任何探测失败都只记录到结构化 JSON 的 status 字段
  （ok / empty / error / skip），main() 永远返回 0，CI 不会被本脚本本身打挂。
  真正有价值的是产物 JSON 与日志，供人工 / 后续步骤复盘「是反爬还是接口变更」。
- **原子写入**：若指定 --out，先写 .tmp 再 os.replace，避免 CI 并发写坏文件。
- **超时保护**：akshare / requests 的每次调用都包了超时（线程池 / timeout 参数），
  防止单个数据源 hang 死整条 CI。

注意（沙箱限制）
----------------
本仓库的本地开发沙箱**禁止出网**，因此本脚本**只在 CI（有网）或本地开 Clash 后
手动运行**，沙箱内不执行。对应测试见 tests/test_probe_datasource_ci.py（全程 mock，
无网络）。
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import json
import logging
import os
import platform
import sys
import time

# 注入 repo 根，保证脚本无论从哪个 cwd 启动都能 import 标准库之外的本仓库辅助模块。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logger = logging.getLogger("probe_datasource_ci")

# ── 探测目标 ──
CHIP_SYMBOLS = ("600036", "603823")  # 招商银行 / 上机数控（覆盖不同板块）
NEWS_SYMBOLS = ("600036",)

# ── 裸 requests 直连东财的两个稳定后端 ──
EASTMONEY_QUOTE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_NEWS_URL = "https://np-anotice-stock.eastmoney.com/api/notice/query"
QUOTE_PARAMS = {
    "secid": "1.600036",
    "fields1": "f1",
    "fields2": "f51,f52,f53",
    "klt": "101",
    "fqt": "0",
    "end": "20500101",
    "lmt": "1",
}
NEWS_PARAMS = {"notice_type": "1", "page_size": "1", "page_index": "1"}

DEFAULT_CHIP_TIMEOUT = 10.0
DEFAULT_NEWS_TIMEOUT = 12.0
DEFAULT_RAW_TIMEOUT = 10.0

# ── 反爬策略默认值（分叉 B）──
# 这些默认值**必须与 config.yaml [chip] / Config 的默认值保持一致**，
# 否则 canary 验证的就不是线上真实链路。之所以在这里重复一份而不是
# import ChipFetchPolicy，是为了守住本脚本「零业务依赖」的设计铁律
# （见模块 docstring）：业务代码重构不得让 canary 失效。
# 对应 config.yaml:
#   chip.force_user_agent / chip.referer / chip.max_retries
#   / chip.retry_backoff_seconds
DEFAULT_CHIP_FORCE_USER_AGENT = True
DEFAULT_CHIP_REFERER = "https://quote.eastmoney.com"
DEFAULT_CHIP_MAX_RETRIES = 2
DEFAULT_CHIP_RETRY_BACKOFF = 1.5
DEFAULT_CHIP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ─────────────────────────────── 工具函数 ───────────────────────────────
def _import_akshare():
    """惰性导入 akshare；缺失时抛 ImportError（由调用方降级为 skip）。"""
    import akshare  # noqa: WPS433 惰性导入，缺失即降级
    return akshare


def _import_requests():
    """惰性导入 requests；缺失时抛 ImportError（由调用方降级为 skip）。"""
    import requests  # noqa: WPS433 惰性导入，缺失即降级
    return requests


def _run_with_timeout(fn, timeout: float):
    """在线程池里跑 fn，超时即抛 TimeoutError（不会拖垮整个 CI 步骤）。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            return future.result(timeout=max(0.1, timeout))
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"call exceeded {timeout}s")


def _latency_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def _df_snippet(df) -> object:
    """把返回的 DataFrame 首行转成可 JSON 序列化的摘要（截断）。"""
    try:
        if hasattr(df, "iloc"):
            row = df.iloc[0]
            if hasattr(row, "to_dict"):
                return {k: str(v)[:80] for k, v in row.to_dict().items()}
            return str(row)[:200]
        if isinstance(df, (list, tuple)) and df:
            return df[0]
        return str(df)[:200]
    except Exception as exc:  # 摘要失败不应影响主结果
        return f"<snippet-error:{exc}>"


def _result(status: str, **extra) -> dict:
    base = {"status": status}
    base.update(extra)
    return base


# ─────────────────────────────── 反爬补丁（分叉 B）───────────────────────────────
@contextlib.contextmanager
def chip_anti_crawl_patch(
    *,
    force_user_agent: bool = DEFAULT_CHIP_FORCE_USER_AGENT,
    referer: str = DEFAULT_CHIP_REFERER,
    timeout: float = DEFAULT_CHIP_TIMEOUT,
):
    """临时给所有 requests 出站请求注入 UA/Referer 的上下文管理器。

    与 ``data_provider.akshare_fetcher.chip_anti_crawl_patch`` 行为对齐
    （刻意不 import 业务代码，见模块 docstring 的「零业务依赖」铁律）：
    唯一可靠的注入点是 ``requests.sessions.Session.request``，因为 akshare
    内部所有出站请求最终都收敛到这里；改 ``requests.utils.default_headers()``
    对已建 Session 无效。退出时在 finally 中无条件还原。

    Args:
        force_user_agent: False 时完全不做任何事（等价于旧行为）
        referer: 注入的 Referer；空串表示不注入
        timeout: 为未显式指定 timeout 的请求兜底注入的超时（<=0 表示不注入）

    Yields:
        bool: True 表示真正安装了补丁
    """
    if not force_user_agent:
        yield False
        return

    try:
        requests = _import_requests()
    except Exception:  # requests 缺失时无声降级，探测本身仍继续
        yield False
        return

    original = requests.sessions.Session.request
    if getattr(original, "_probe_chip_patched", False):
        yield False  # 幂等：已在补丁作用域内
        return

    def _wrapped(self, method, url, *args, **kwargs):
        if args:  # 位置参数形式罕见，原样透传避免签名错位
            return original(self, method, url, *args, **kwargs)
        headers = dict(kwargs.pop("headers", None) or {})
        lowered = {str(k).lower() for k in headers}
        if "user-agent" not in lowered:
            headers["User-Agent"] = DEFAULT_CHIP_USER_AGENT
        if referer and "referer" not in lowered:
            headers["Referer"] = referer
        kwargs["headers"] = headers
        if timeout and timeout > 0 and kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(self, method, url, **kwargs)

    _wrapped._probe_chip_patched = True
    requests.sessions.Session.request = _wrapped
    try:
        yield True
    finally:
        requests.sessions.Session.request = original


# ─────────────────────────────── 各类探测 ───────────────────────────────
def probe_chip_one(
    symbol: str,
    *,
    akshare=None,
    timeout: float = DEFAULT_CHIP_TIMEOUT,
    max_retries: int = DEFAULT_CHIP_MAX_RETRIES,
    force_user_agent: bool = DEFAULT_CHIP_FORCE_USER_AGENT,
    referer: str = DEFAULT_CHIP_REFERER,
    retry_backoff: float = DEFAULT_CHIP_RETRY_BACKOFF,
) -> dict:
    """探测单只股票的筹码分布（akshare.stock_cyq_em）。

    默认按分叉 B 的线上策略执行：注入反爬头 + 最多重试 ``max_retries`` 次。
    这样 canary 验证的就是修复后的真实链路，而不是裸调用。

    Args:
        symbol: 股票代码
        akshare: 注入的 akshare 模块（测试用），None 表示惰性真实导入
        timeout: 单次调用超时（秒）
        max_retries: 失败重试次数（0 = 不重试）；空结果不重试
        force_user_agent: 是否注入 UA/Referer
        referer: 注入的 Referer
        retry_backoff: 指数退避基数（秒）

    Returns:
        结构化结果 dict，含 status / attempts / anti_crawl 等字段
    """
    started = time.monotonic()
    try:
        ak = akshare if akshare is not None else _import_akshare()
    except Exception as exc:
        return _result("skip", reason="akshare_missing", error=str(exc), latency_ms=0.0, symbol=symbol)

    anti_crawl = {
        "force_user_agent": bool(force_user_agent),
        "referer": referer if force_user_agent else "",
        "max_retries": int(max(0, max_retries)),
    }
    attempts_allowed = max(1, int(max_retries) + 1)
    last_error = ""

    for attempt in range(attempts_allowed):
        try:
            with chip_anti_crawl_patch(
                force_user_agent=force_user_agent, referer=referer, timeout=timeout
            ):
                df = _run_with_timeout(lambda: ak.stock_cyq_em(symbol=symbol), timeout)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts_allowed - 1:
                time.sleep(max(0.0, retry_backoff) * (2 ** attempt))
                continue
            return _result(
                "error",
                error=last_error,
                latency_ms=_latency_ms(started),
                symbol=symbol,
                attempts=attempt + 1,
                anti_crawl=anti_crawl,
            )

        # 空结果是「接口通但没内容」，重试无意义，直接短路
        if df is None or (hasattr(df, "empty") and df.empty) or (not hasattr(df, "empty") and len(df) == 0):
            return _result(
                "empty",
                latency_ms=_latency_ms(started),
                symbol=symbol,
                attempts=attempt + 1,
                anti_crawl=anti_crawl,
            )

        return _result(
            "ok",
            rows=int(len(df)),
            snippet=_df_snippet(df),
            latency_ms=_latency_ms(started),
            symbol=symbol,
            attempts=attempt + 1,
            anti_crawl=anti_crawl,
        )

    # 理论不可达（循环内所有分支都 return），兜底保证函数永远有返回值
    return _result(
        "error",
        error=last_error or "unreachable",
        latency_ms=_latency_ms(started),
        symbol=symbol,
        attempts=attempts_allowed,
        anti_crawl=anti_crawl,
    )


def probe_news_one(symbol: str, *, akshare=None, timeout: float = DEFAULT_NEWS_TIMEOUT) -> dict:
    """探测单只股票的新闻（akshare.stock_news_em）。"""
    started = time.monotonic()
    try:
        ak = akshare if akshare is not None else _import_akshare()
    except Exception as exc:
        return _result("skip", reason="akshare_missing", error=str(exc), latency_ms=0.0, symbol=symbol)

    try:
        df = _run_with_timeout(lambda: ak.stock_news_em(symbol=symbol), timeout)
    except Exception as exc:
        return _result("error", error=str(exc), latency_ms=_latency_ms(started), symbol=symbol)

    if df is None or (hasattr(df, "empty") and df.empty) or (not hasattr(df, "empty") and len(df) == 0):
        return _result("empty", latency_ms=_latency_ms(started), symbol=symbol)

    return _result(
        "ok",
        items=int(len(df)),
        snippet=_df_snippet(df),
        latency_ms=_latency_ms(started),
        symbol=symbol,
    )


def probe_raw_one(
    name: str,
    url: str,
    *,
    requests_mod=None,
    timeout: float = DEFAULT_RAW_TIMEOUT,
    params=None,
) -> dict:
    """用裸 requests 直连东财某个后端，验证底层通道可达性。"""
    started = time.monotonic()
    try:
        req = requests_mod if requests_mod is not None else _import_requests()
    except Exception as exc:
        return _result("skip", reason="requests_missing", error=str(exc), latency_ms=0.0, name=name)

    try:
        resp = req.get(url, params=params, timeout=timeout, headers={"User-Agent": "dsa-test-probe/1.0"})
        status = getattr(resp, "status_code", None)
        text = getattr(resp, "text", "") or ""
        snippet = text[:200]
        if status is not None and 200 <= status < 300:
            return _result("ok", http_status=int(status), snippet=snippet, latency_ms=_latency_ms(started), name=name)
        return _result("error", http_status=status, snippet=snippet, latency_ms=_latency_ms(started), name=name)
    except Exception as exc:
        return _result("error", error=str(exc), latency_ms=_latency_ms(started), name=name)


# ─────────────────────────────── 编排 + 汇总 ───────────────────────────────
def _collect_env() -> dict:
    """收集运行环境快照（是否有库、是否走代理），用于复盘反爬/接口变更。"""
    env = {
        "has_akshare": True,
        "has_requests": True,
        "http_proxy": os.getenv("http_proxy") or os.getenv("HTTP_PROXY") or None,
        "https_proxy": os.getenv("https_proxy") or os.getenv("HTTPS_PROXY") or None,
    }
    try:
        _import_akshare()
    except Exception:
        env["has_akshare"] = False
    try:
        _import_requests()
    except Exception:
        env["has_requests"] = False
    return env


def _is_ok(entry: dict) -> bool:
    return entry.get("status") == "ok"


def _summarize(targets: dict) -> dict:
    """根据各维度探测结果汇总出整体结论。"""
    chip = targets.get("chip", {})
    news = targets.get("news", {})
    raw = targets.get("raw_eastmoney", {})

    chip_ok = sum(1 for e in chip.values() if _is_ok(e))
    chip_empty = sum(1 for e in chip.values() if e.get("status") == "empty")
    chip_error = sum(1 for e in chip.values() if e.get("status") == "error")
    chip_skip = sum(1 for e in chip.values() if e.get("status") == "skip")

    news_ok = sum(1 for e in news.values() if _is_ok(e))
    news_empty = sum(1 for e in news.values() if e.get("status") == "empty")
    news_error = sum(1 for e in news.values() if e.get("status") == "error")
    news_skip = sum(1 for e in news.values() if e.get("status") == "skip")

    raw_ok = sum(1 for e in raw.values() if _is_ok(e))
    raw_error = sum(1 for e in raw.values() if e.get("status") == "error")
    raw_skip = sum(1 for e in raw.values() if e.get("status") == "skip")

    total_ok = chip_ok + news_ok + raw_ok
    total_targets = len(chip) + len(news) + len(raw)
    if total_ok == 0:
        overall = "all_failed"
    elif total_ok == total_targets:
        overall = "all_ok"
    else:
        overall = "partial"

    return {
        "chip_ok": chip_ok,
        "chip_empty": chip_empty,
        "chip_error": chip_error,
        "chip_skip": chip_skip,
        "news_ok": news_ok,
        "news_empty": news_empty,
        "news_error": news_error,
        "news_skip": news_skip,
        "raw_ok": raw_ok,
        "raw_error": raw_error,
        "raw_skip": raw_skip,
        "overall": overall,
    }


def run_probes(
    *,
    akshare=None,
    requests_mod=None,
    chip_symbols=CHIP_SYMBOLS,
    news_symbols=NEWS_SYMBOLS,
    chip_timeout: float = DEFAULT_CHIP_TIMEOUT,
    news_timeout: float = DEFAULT_NEWS_TIMEOUT,
    raw_timeout: float = DEFAULT_RAW_TIMEOUT,
    chip_max_retries: int = DEFAULT_CHIP_MAX_RETRIES,
    chip_force_user_agent: bool = DEFAULT_CHIP_FORCE_USER_AGENT,
    chip_referer: str = DEFAULT_CHIP_REFERER,
) -> dict:
    """执行全部探测，返回结构化报告（targets + summary）。"""
    targets = {
        "chip": {
            sym: probe_chip_one(
                sym,
                akshare=akshare,
                timeout=chip_timeout,
                max_retries=chip_max_retries,
                force_user_agent=chip_force_user_agent,
                referer=chip_referer,
            )
            for sym in chip_symbols
        },
        "news": {
            sym: probe_news_one(sym, akshare=akshare, timeout=news_timeout)
            for sym in news_symbols
        },
        "raw_eastmoney": {
            "quote": probe_raw_one(
                "quote", EASTMONEY_QUOTE_URL, requests_mod=requests_mod,
                timeout=raw_timeout, params=QUOTE_PARAMS,
            ),
            "news": probe_raw_one(
                "news", EASTMONEY_NEWS_URL, requests_mod=requests_mod,
                timeout=raw_timeout, params=NEWS_PARAMS,
            ),
        },
    }
    return {
        "tool": "probe_datasource_ci",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "python_version": platform.python_version(),
        "environment": _collect_env(),
        "targets": targets,
        "summary": _summarize(targets),
    }


def to_json(report: dict) -> str:
    """把报告序列化为 JSON 字符串（ensure_ascii=False 保留中文）。"""
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)


def _atomic_write(path: str, text: str) -> None:
    """原子写入：先写 .tmp 再 os.replace，避免并发/中断写坏文件。"""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(argv=None) -> int:
    """CI 入口：永远返回 0（失败只记录在 JSON 里）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="P1.6 数据源 CI 实证探测（永不非零退出）")
    parser.add_argument("--out", default=None, help="把 JSON 报告写到该路径（原子写入）")
    parser.add_argument("--chip-timeout", type=float, default=DEFAULT_CHIP_TIMEOUT)
    parser.add_argument("--news-timeout", type=float, default=DEFAULT_NEWS_TIMEOUT)
    parser.add_argument("--raw-timeout", type=float, default=DEFAULT_RAW_TIMEOUT)
    parser.add_argument(
        "--chip-max-retries", type=int, default=DEFAULT_CHIP_MAX_RETRIES,
        help="筹码探测失败重试次数（默认与 config.yaml chip.max_retries 一致）",
    )
    parser.add_argument(
        "--chip-referer", default=DEFAULT_CHIP_REFERER,
        help="筹码探测注入的 Referer（默认与 config.yaml chip.referer 一致）",
    )
    parser.add_argument(
        "--no-chip-anti-crawl", action="store_true",
        help="关闭反爬头注入，用于对照实验（复现修复前的裸调用行为）",
    )
    args = parser.parse_args(argv)

    try:
        report = run_probes(
            chip_timeout=args.chip_timeout,
            news_timeout=args.news_timeout,
            raw_timeout=args.raw_timeout,
            chip_max_retries=args.chip_max_retries,
            chip_force_user_agent=not args.no_chip_anti_crawl,
            chip_referer=args.chip_referer,
        )
        text = to_json(report)
        if args.out:
            _atomic_write(args.out, text)
        print(text)
        summary = report["summary"]
        print(
            f"[probe] overall={summary['overall']} "
            f"chip_ok={summary['chip_ok']} news_ok={summary['news_ok']} raw_ok={summary['raw_ok']}",
            file=sys.stderr,
        )
    except Exception as exc:  # 兜底：任何未预期的异常也不让 CI 挂
        logger.exception("probe 发生未预期异常")
        fallback = {
            "tool": "probe_datasource_ci",
            "fatal_error": str(exc),
            "overall": "error",
        }
        print(to_json(fallback))
    return 0


if __name__ == "__main__":
    # ────────────────────────────────────────────────────────────────
    # 强制退出（BUG-2 修复）
    # ────────────────────────────────────────────────────────────────
    # 现象：main() 已返回 rc=0 且报告已落盘（主逻辑约 5s 跑完），但进程挂死不退。
    # 根因：akshare 内部依赖 py_mini_racer，其 `_running_event_loop` 守护线程在
    #       解释器退出阶段被 atexit 钩子 join，永远等不到事件循环结束
    #       （faulthandler 栈停在 py_mini_racer/_mini_racer.py:368）。
    # 对策：本脚本是「只读探测 + 原子写产物」的 canary，退出时没有需要 flush 的
    #       业务状态。因此先手工 flush 两个标准流（保证 JSON 产物与 summary 不丢），
    #       再用 os._exit() 跳过 atexit / 线程 join 直接返回退出码。
    # 约束：不改 main() 内部逻辑与返回值语义 —— rc 依旧原样透传给 shell。
    try:
        _rc = main()
    except SystemExit as _exc:
        # argparse 的 --help（code=0）/ 参数错误（code=2）走这里，
        # 同样要经由 os._exit 强制退出，避免任何路径落回 atexit。
        _rc = _exc.code if isinstance(_exc.code, int) else 0
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
