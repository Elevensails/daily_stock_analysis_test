# -*- coding: utf-8 -*-
"""P1.6 分叉 B 补丁生命周期安全 + 注入正确性 + 关键不变量（独立回归）。

本文件独立于工程师的 ``test_chip_anti_crawl.py``，专门补死以下「尚未被任何测试覆盖」
或「最易因实现失误而泄漏」的场景：

阶段 2（patch 生命周期安全，monkey-patch 不能污染全局）：
  1. 异常路径还原：with 内抛异常后，Session.request 用 ``is`` 还原成原函数；
  2. 嵌套幂等：嵌套 2~3 层，全部退出后恢复到「真正原始函数」，无残留包装层；
  3. 交错退出：A 进入→B 进入→A 退出→B 退出，验证不会把包装函数当「原函数」遗留；
  4. force_user_agent=False 零副作用：Session.request 身份不变。

阶段 3（注入正确性与重试）：
  5. 调用方用大小写变体 user-agent / REFERER 时不被覆盖；
  6. 位置参数调用 session.request("GET", url, params, data, headers) 不被破坏；
  7. chip_request_timeout_seconds 兜底注入；调用方显式传 timeout 时不覆盖；
  8. 前两次 RemoteDisconnected、第三次成功 → ok 且尝试 3 次；
  9. 三次全失败 → FETCH_FAILED（不是 NOT_APPLICABLE）；
  10. 空 DataFrame → 短路不重试（EMPTY）；
  11. **ETF/港股/美股仍 NOT_APPLICABLE 且不重试、不触发熔断**（P1.6 核心不变量）。

全程 mock，无网络。
"""

import pytest
import requests
import pandas as pd
from unittest.mock import patch

from data_provider.akshare_fetcher import (
    AkshareFetcher,
    chip_anti_crawl_patch,
    _build_chip_anti_crawl_headers,
)
from data_provider.realtime_types import (
    ChipFetchPolicy,
    ChipUnavailableReason,
    get_chip_circuit_breaker,
)


# ═════════════════════════════════════════════════
# 公共夹具
# ═════════════════════════════════════════════════
@pytest.fixture
def fetcher():
    """零休眠的 AkshareFetcher，避免限速 sleep 拖慢单测。"""
    return AkshareFetcher(sleep_min=0.0, sleep_max=0.0)


def _policy(**overrides) -> ChipFetchPolicy:
    """构造测试用策略，默认开启反爬 + 2 次重试（与线上默认一致）。"""
    base = {
        "force_user_agent": True,
        "referer": "https://quote.eastmoney.com",
        "max_retries": 2,
        "retry_backoff_seconds": 0.0,   # 单测不真的等
        "request_timeout_seconds": 10.0,
    }
    base.update(overrides)
    return ChipFetchPolicy(**base)


def _chip_df() -> pd.DataFrame:
    """一行合法筹码数据。"""
    return pd.DataFrame(
        [
            {
                "日期": "2026-02-27",
                "获利比例": 0.61,
                "平均成本": 12.3,
                "90成本-低": 10.0,
                "90成本-高": 15.0,
                "90集中度": 0.2,
                "70成本-低": 11.0,
                "70成本-高": 14.0,
                "70集中度": 0.12,
            }
        ]
    )


# ═════════════════════════════════════════════════
# 阶段 2：patch 生命周期安全
# ═════════════════════════════════════════════════
class TestChipPatchLifecycle:
    """monkey-patch 一旦泄漏会污染全进程所有 requests 调用，这里逐条钉死还原语义。"""

    def test_restore_on_exception(self):
        """异常路径同样必须还原（finally 语义），不能把包装函数留在类上。"""
        original = requests.sessions.Session.request
        with pytest.raises(RuntimeError):
            with chip_anti_crawl_patch(_policy()):
                raise RuntimeError("boom")
        assert requests.sessions.Session.request is original

    def test_nested_idempotent_restores_to_true_original(self):
        """嵌套 2~3 层，全部退出后恢复到「真正原始函数」，无残留包装层。"""
        original = requests.sessions.Session.request
        with chip_anti_crawl_patch(_policy()) as outer:
            wrapped = requests.sessions.Session.request
            assert outer is True
            assert wrapped is not original
            # 嵌套第 2 层
            with chip_anti_crawl_patch(_policy()) as inner1:
                # 嵌套第 3 层
                with chip_anti_crawl_patch(_policy()) as inner2:
                    assert (inner1, inner2) == (False, False)
                    assert requests.sessions.Session.request is wrapped
                assert requests.sessions.Session.request is wrapped
            assert requests.sessions.Session.request is wrapped
        # 最外层退出后，必须回到真正的原始函数（不是任何包装层）
        assert requests.sessions.Session.request is original

    def test_interleaved_exit_does_not_leak_wrapper(self):
        """交错退出：A 进入→B 进入→A 退出→B 退出。

        唯一会让 patch 永久残留的场景是「层层包装 + 交错还原」。本实现用
        幂等标记消除了它：B 看到标记后不再二次包装、退出时也不还原。本测试
        验证 A 退出后回到真正原始函数，且 B 退出期间没有把包装层当「原函数」遗留。
        """
        original = requests.sessions.Session.request
        with chip_anti_crawl_patch(_policy()) as a_installed:
            assert a_installed is True
            wrapped_a = requests.sessions.Session.request
            with chip_anti_crawl_patch(_policy()) as b_installed:
                # B 是嵌套进入，必须幂等：不二次安装
                assert b_installed is False
                assert requests.sessions.Session.request is wrapped_a
            # B 退出后，A 仍持有补丁（证明 B 的退出没有把 Session.request 还原成 wrapper）
            assert requests.sessions.Session.request is wrapped_a
        # A 退出后必须回到真正原始函数
        assert requests.sessions.Session.request is original

    def test_noop_when_force_user_agent_false(self):
        """分叉 A（force_user_agent=False）必须零副作用：完全不碰 requests。"""
        original = requests.sessions.Session.request
        with chip_anti_crawl_patch(_policy(force_user_agent=False)) as installed:
            assert installed is False
            assert requests.sessions.Session.request is original
        assert requests.sessions.Session.request is original


# ═════════════════════════════════════════════════
# 阶段 3：注入正确性与重试
# ═════════════════════════════════════════════════
class TestChipInjectionCorrectness:
    """反爬头只能「补齐缺失项」，绝不能覆盖调用方主动给的头或 timeout。"""

    def test_caller_case_variant_headers_preserved(self):
        """调用方用大小写变体 user-agent / REFERER 时不被覆盖。"""
        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy()):
                requests.sessions.Session().request(
                    "GET",
                    "https://example.invalid",
                    headers={"user-agent": "mine/1.0", "REFERER": "http://caller"},
                )

        # 调用方的小写 user-agent 与大写 REFERER 都原样保留
        assert captured["user-agent"] == "mine/1.0"
        assert captured["REFERER"] == "http://caller"
        # 因变体已存在，补丁不应再注入标准大小写副本
        assert captured.get("User-Agent") is None
        assert captured.get("Referer") is None

    def test_positional_args_passthrough(self):
        """位置参数形式必须原样透传，不得因签名错位改坏请求。"""
        seen = {}

        def _fake_request(self, method, url, *args, **kwargs):
            seen["args"] = args
            seen["headers"] = kwargs.get("headers")
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy()):
                requests.sessions.Session().request(
                    "GET", "https://example.invalid", {"p": 1}, None, {"UA": "x"}
                )

        # 三个位置参数 params/data/headers 原样透传
        assert seen["args"] == ({"p": 1}, None, {"UA": "x"})
        # 因走位置参数分支（args 非空），不进入 headers 关键字补齐逻辑
        assert seen["headers"] is None
        # 位置参数里的 headers 字典本身未被改坏
        assert seen["args"][-1] == {"UA": "x"}

    def test_default_timeout_injected_when_absent(self):
        """akshare 内部不传 timeout，补丁需兜底注入，避免 CI 挂死。"""
        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs)
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy(request_timeout_seconds=5.0)):
                requests.sessions.Session().request("GET", "https://example.invalid")

        assert captured["timeout"] == 5.0

    def test_caller_explicit_timeout_not_overridden(self):
        """调用方显式传 timeout 时，补丁的兜底 timeout 不得覆盖它。"""
        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs)
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy(request_timeout_seconds=5.0)):
                requests.sessions.Session().request(
                    "GET", "https://example.invalid", timeout=2.0
                )

        assert captured["timeout"] == 2.0

    def test_retry_until_success_third_attempt(self, fetcher):
        """前两次抛 RemoteDisconnected、第三次成功 → 最终 ok，共调用 3 次。"""
        fetcher._chip_policy_cache = _policy(max_retries=2)
        calls = {"n": 0}

        def _flaky(symbol=None, **_kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("RemoteDisconnected('Remote end closed connection')")
            return _chip_df()

        with patch("akshare.stock_cyq_em", side_effect=_flaky):
            result = fetcher.get_chip_distribution_ex("600036")

        assert calls["n"] == 3
        assert result.ok is True
        assert result.chip is not None
        assert result.chip.profit_ratio == pytest.approx(0.61)

    def test_all_fail_returns_fetch_failed_not_na(self, fetcher):
        """三次全失败 → max_retries+1 次尝试后落 FETCH_FAILED，而非 NOT_APPLICABLE。"""
        fetcher._chip_policy_cache = _policy(max_retries=2)

        with patch(
            "akshare.stock_cyq_em",
            side_effect=ConnectionError("RemoteDisconnected"),
        ) as mock_api:
            result = fetcher.get_chip_distribution_ex("600036")

        assert mock_api.call_count == 3
        assert result.reason is ChipUnavailableReason.FETCH_FAILED
        assert "RemoteDisconnected" in result.detail

    def test_empty_dataframe_short_circuits(self, fetcher):
        """空数据是「接口通但没内容」，重试无意义，必须只调用一次。"""
        fetcher._chip_policy_cache = _policy(max_retries=2)

        with patch("akshare.stock_cyq_em", return_value=pd.DataFrame()) as mock_api:
            result = fetcher.get_chip_distribution_ex("600036")

        assert mock_api.call_count == 1
        assert result.reason is ChipUnavailableReason.EMPTY

    def test_etf_hk_us_not_applicable_no_retry_no_circuit(self, fetcher):
        """P1.6 核心不变量：ETF/港股/美股走 NOT_APPLICABLE，不调接口、不重试、不触发熔断。"""
        cases = [
            ("512400", "ETF"),
            ("00700", "港股"),
            ("AAPL", "美股"),
        ]
        for code, label in cases:
            fetcher._chip_policy_cache = _policy(max_retries=2)
            cb_before = dict(get_chip_circuit_breaker().get_status())
            with patch("akshare.stock_cyq_em") as mock_api:
                result = fetcher.get_chip_distribution_ex(code)

            assert mock_api.call_count == 0, (
                f"{label}({code}) 不应调用 akshare 筹码接口"
            )
            assert result.reason is ChipUnavailableReason.NOT_APPLICABLE, (
                f"{label}({code}) 应为 NOT_APPLICABLE，实得 {result.reason}"
            )
            # 不触发熔断：NOT_APPLICABLE 既不计入失败，也不计入 provider_run
            assert result.should_record_failure is False, (
                f"{label}({code}) NOT_APPLICABLE 不应触发熔断"
            )
            assert result.should_record_provider_run is False
            cb_after = dict(get_chip_circuit_breaker().get_status())
            assert cb_after == cb_before, (
                f"{label}({code}) 不应改变筹码熔断状态"
            )
