# -*- coding: utf-8 -*-
"""P1.6 分叉 B（反爬）回归测试：让 ChipFetchPolicy 的反爬字段真正生效。

背景
----
CI 实证：GitHub Actions 上 ``ak.stock_cyq_em()`` 抛 ``RemoteDisconnected``，
而同一 runner 裸 requests 直连东财 raw 接口返回 200 —— 判定为 akshare 封装层
被反爬识别。修复要点有三条，本文件逐条钉死：

1. ``Config`` 真的有 chip_* 字段，且 ``ChipFetchPolicy.from_config()`` 读得到；
2. 反爬补丁真的改写了 ``requests.sessions.Session.request`` 注入的 headers，
   且退出作用域后**必须还原**（不能污染其他数据源的请求）；
3. ``get_chip_distribution_ex`` 在网络类失败时按 ``max_retries`` 真的重试。

全程 mock，无网络。
"""

import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

from data_provider.akshare_fetcher import (
    AkshareFetcher,
    chip_anti_crawl_patch,
    _build_chip_anti_crawl_headers,
)
from data_provider.realtime_types import ChipFetchPolicy, ChipUnavailableReason


# ═════════════════════════════════════════════════════════════
# 0. 公共夹具
# ═════════════════════════════════════════════════════════════
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


# ═════════════════════════════════════════════════════════════
# 1. 配置字段真实生效（修复前全部落回默认值）
# ═════════════════════════════════════════════════════════════
class TestChipPolicyFromConfig:
    """Config 必须真的带 chip_* 字段，否则策略永远是默认值、修复不生效。"""

    def test_config_dataclass_declares_chip_fields(self):
        """回归：修复前 Config 上根本没有这些字段，getattr 软读取全落默认值。"""
        from src.config import Config

        names = {f.name for f in Config.__dataclass_fields__.values()}
        assert {
            "chip_force_user_agent",
            "chip_referer",
            "chip_max_retries",
            "chip_retry_backoff_seconds",
            "chip_request_timeout_seconds",
            "chip_allow_tushare_fallback",
            "chip_disable_akshare",
        } <= names

    def test_config_defaults_enable_anti_crawl(self):
        """默认值必须是「分叉 B 已生效」的取值，CI 不配置也能修复。"""
        from src.config import Config

        defaults = {f.name: f.default for f in Config.__dataclass_fields__.values()}
        assert defaults["chip_force_user_agent"] is True
        assert defaults["chip_referer"] == "https://quote.eastmoney.com"
        assert defaults["chip_max_retries"] == 2
        assert defaults["chip_disable_akshare"] is False

    def test_from_config_reads_new_fields(self):
        """核心断言：策略字段来自 Config，而不是类默认值。"""

        class _Cfg:
            chip_force_user_agent = True
            chip_referer = "https://quote.eastmoney.com"
            chip_max_retries = 3
            chip_retry_backoff_seconds = 2.5
            chip_request_timeout_seconds = 7.0
            chip_allow_tushare_fallback = False
            chip_disable_akshare = True

        policy = ChipFetchPolicy.from_config(_Cfg())

        assert policy.force_user_agent is True
        assert policy.referer == "https://quote.eastmoney.com"
        assert policy.max_retries == 3
        assert policy.retry_backoff_seconds == 2.5
        assert policy.request_timeout_seconds == 7.0
        assert policy.allow_tushare_fallback is False
        assert policy.disable_akshare_chip is True

    def test_from_config_survives_missing_and_none_config(self):
        """config 为 None / 缺字段时不得抛异常，必须回落类默认值。"""
        assert ChipFetchPolicy.from_config(None) == ChipFetchPolicy()
        assert ChipFetchPolicy.from_config(object()) == ChipFetchPolicy()

    def test_from_config_survives_garbage_values(self):
        """脏值（不可转 int）必须整体回落默认值，绝不把异常抛给抓取链路。"""

        class _BadCfg:
            chip_max_retries = "not-a-number"

        assert ChipFetchPolicy.from_config(_BadCfg()) == ChipFetchPolicy()

    def test_real_config_load_wires_chip_section(self):
        """端到端：真实 Config 加载链路（.env + config.yaml）能产出生效的策略。"""
        from src.config import Config

        cfg = Config._load_from_env()
        policy = ChipFetchPolicy.from_config(cfg)

        assert policy.force_user_agent is True
        assert policy.max_retries >= 1
        assert policy.referer.startswith("http")

    def test_env_override_wins_over_yaml(self, monkeypatch):
        """env 优先级必须高于 config.yaml（U16 约定），便于 CI 临时对照实验。"""
        from src.config import Config

        monkeypatch.setenv("CHIP_FORCE_USER_AGENT", "false")
        monkeypatch.setenv("CHIP_MAX_RETRIES", "5")
        cfg = Config._load_from_env()
        policy = ChipFetchPolicy.from_config(cfg)

        assert policy.force_user_agent is False
        assert policy.max_retries == 5


# ═════════════════════════════════════════════════════════════
# 2. 补丁真的注入 headers，且真的还原
# ═════════════════════════════════════════════════════════════
class TestChipAntiCrawlPatch:
    """反爬补丁的注入点必须是 Session.request（default_headers 已证明无效）。"""

    def test_patch_injects_user_agent_and_referer(self):
        """核心断言：补丁作用域内发出的请求带上了 UA + Referer。"""
        import requests

        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy()) as installed:
                assert installed is True
                requests.sessions.Session().request("GET", "https://example.invalid")

        assert "User-Agent" in captured
        assert captured["User-Agent"]
        assert captured["Referer"] == "https://quote.eastmoney.com"

    def test_patch_injects_default_timeout(self):
        """akshare 内部不传 timeout，补丁需兜底注入，避免 CI 挂死。"""
        import requests

        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs)
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy(request_timeout_seconds=3.5)):
                requests.sessions.Session().request("GET", "https://example.invalid")

        assert captured["timeout"] == 3.5

    def test_patch_does_not_override_caller_headers(self):
        """只补齐缺失的头，调用方显式给的 UA 必须原样保留。"""
        import requests

        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy()):
                requests.sessions.Session().request(
                    "GET", "https://example.invalid", headers={"User-Agent": "mine/1.0"}
                )

        assert captured["User-Agent"] == "mine/1.0"
        assert captured["Referer"] == "https://quote.eastmoney.com"

    def test_patch_restores_original_after_exit(self):
        """退出作用域必须还原，否则会污染所有非筹码请求。"""
        import requests

        original = requests.sessions.Session.request
        with chip_anti_crawl_patch(_policy()):
            assert requests.sessions.Session.request is not original
        assert requests.sessions.Session.request is original

    def test_patch_restores_original_on_exception(self):
        """异常路径同样必须还原（finally 语义）。"""
        import requests

        original = requests.sessions.Session.request
        with pytest.raises(RuntimeError):
            with chip_anti_crawl_patch(_policy()):
                raise RuntimeError("boom")
        assert requests.sessions.Session.request is original

    def test_patch_is_noop_when_disabled(self):
        """分叉 A（默认关闭）必须零行为变化：完全不碰 requests。"""
        import requests

        original = requests.sessions.Session.request
        with chip_anti_crawl_patch(_policy(force_user_agent=False)) as installed:
            assert installed is False
            assert requests.sessions.Session.request is original
        assert requests.sessions.Session.request is original

    def test_patch_is_idempotent_when_nested(self):
        """嵌套安装必须幂等，避免层层包装导致还原顺序出错。"""
        import requests

        original = requests.sessions.Session.request
        with chip_anti_crawl_patch(_policy()) as outer:
            wrapped_once = requests.sessions.Session.request
            with chip_anti_crawl_patch(_policy()) as inner:
                assert outer is True and inner is False
                assert requests.sessions.Session.request is wrapped_once
            assert requests.sessions.Session.request is wrapped_once
        assert requests.sessions.Session.request is original

    def test_patch_passes_through_positional_args(self):
        """位置参数形式必须原样透传，不得因签名错位改坏请求。"""
        import requests

        seen = {}

        def _fake_request(self, method, url, *args, **kwargs):
            seen["args"] = args
            seen["headers"] = kwargs.get("headers")
            return "resp"

        with patch.object(requests.sessions.Session, "request", _fake_request):
            with chip_anti_crawl_patch(_policy()):
                requests.sessions.Session().request("GET", "https://example.invalid", {"p": 1})

        assert seen["args"] == ({"p": 1},)
        assert seen["headers"] is None  # 未被改写

    def test_build_headers_omits_referer_when_empty(self):
        """referer 为空时不得注入空 Referer 头。"""
        headers = _build_chip_anti_crawl_headers(_policy(referer=""))
        assert "User-Agent" in headers
        assert "Referer" not in headers

    def test_fetcher_method_returns_context_manager(self, fetcher):
        """_apply_chip_anti_crawl_headers 现在返回上下文管理器（旧实现返回 None）。"""
        cm = fetcher._apply_chip_anti_crawl_headers(_policy())
        assert hasattr(cm, "__enter__") and hasattr(cm, "__exit__")
        with cm as installed:
            assert installed is True


# ═════════════════════════════════════════════════════════════
# 3. 重试真的按 max_retries 执行
# ═════════════════════════════════════════════════════════════
class TestChipRetryOnRemoteDisconnected:
    """CI 上的失败形态就是 RemoteDisconnected，必须能被重试救回来。"""

    def test_retries_until_success(self, fetcher):
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

    def test_exhausts_retries_and_reports_fetch_failed(self, fetcher):
        """始终失败 → max_retries+1 次尝试后落 FETCH_FAILED，带诊断串。"""
        fetcher._chip_policy_cache = _policy(max_retries=2)

        with patch(
            "akshare.stock_cyq_em",
            side_effect=ConnectionError("RemoteDisconnected"),
        ) as mock_api:
            result = fetcher.get_chip_distribution_ex("600036")

        assert mock_api.call_count == 3
        assert result.reason is ChipUnavailableReason.FETCH_FAILED
        assert "RemoteDisconnected" in result.detail

    def test_no_retry_when_max_retries_zero(self, fetcher):
        """max_retries=0（分叉 A）必须只调用一次，保持旧行为。"""
        fetcher._chip_policy_cache = _policy(max_retries=0)

        with patch("akshare.stock_cyq_em", side_effect=ConnectionError("boom")) as mock_api:
            result = fetcher.get_chip_distribution_ex("600036")

        assert mock_api.call_count == 1
        assert result.reason is ChipUnavailableReason.FETCH_FAILED

    def test_empty_dataframe_short_circuits_retry(self, fetcher):
        """空数据是「接口通但没内容」，重试无意义，必须只调用一次。"""
        fetcher._chip_policy_cache = _policy(max_retries=2)

        with patch("akshare.stock_cyq_em", return_value=pd.DataFrame()) as mock_api:
            result = fetcher.get_chip_distribution_ex("600036")

        assert mock_api.call_count == 1
        assert result.reason is ChipUnavailableReason.EMPTY

    def test_disable_akshare_skips_network_entirely(self, fetcher):
        """分叉 C：整源关闭时一次网络都不发。"""
        fetcher._chip_policy_cache = _policy(disable_akshare_chip=True)

        with patch("akshare.stock_cyq_em") as mock_api:
            result = fetcher.get_chip_distribution_ex("600036")

        mock_api.assert_not_called()
        assert result.reason is ChipUnavailableReason.DISABLED

    def test_patch_restored_after_full_fetch(self, fetcher):
        """抓取结束后全局 Session.request 必须已还原（不污染其他数据源）。"""
        import requests

        fetcher._chip_policy_cache = _policy(max_retries=1)
        original = requests.sessions.Session.request

        with patch("akshare.stock_cyq_em", side_effect=ConnectionError("boom")):
            fetcher.get_chip_distribution_ex("600036")

        assert requests.sessions.Session.request is original

    def test_headers_actually_reach_akshare_call(self, fetcher):
        """端到端：ak.stock_cyq_em 内部发出的请求确实带上了反爬头。"""
        import requests

        captured = {}

        def _fake_request(self, method, url, **kwargs):
            captured.update(kwargs.get("headers") or {})
            raise ConnectionError("stop-here")  # 不需要真的返回数据

        def _fake_cyq(symbol=None, **_kw):
            # 模拟 akshare 内部的 requests 调用
            return requests.sessions.Session().request("GET", "https://push2.eastmoney.com/api")

        fetcher._chip_policy_cache = _policy(max_retries=0)
        with patch.object(requests.sessions.Session, "request", _fake_request):
            with patch("akshare.stock_cyq_em", side_effect=_fake_cyq):
                result = fetcher.get_chip_distribution_ex("600036")

        assert captured.get("User-Agent")
        assert captured.get("Referer") == "https://quote.eastmoney.com"
        assert result.reason is ChipUnavailableReason.FETCH_FAILED


# ═════════════════════════════════════════════════════════════
# 4. probe 脚本默认启用反爬（下次 CI 直接验证修复）
# ═════════════════════════════════════════════════════════════
class TestProbeScriptAntiCrawlDefaults:
    """canary 必须探测「修复后的链路」，否则验证不了修复效果。"""

    SCRIPT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "probe_datasource_ci.py",
    )

    @classmethod
    def _load_probe(cls):
        """按路径动态加载 canary 脚本（scripts 无 __init__.py）。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "probe_datasource_ci_anticrawl", cls.SCRIPT
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_probe_defaults_match_config_defaults(self):
        """probe 的硬编码默认值必须与 config.yaml [chip] 默认值一致。"""
        probe = self._load_probe()
        assert probe.DEFAULT_CHIP_FORCE_USER_AGENT is True
        assert probe.DEFAULT_CHIP_REFERER == "https://quote.eastmoney.com"
        assert probe.DEFAULT_CHIP_MAX_RETRIES == 2

        from src.config import Config

        defaults = {f.name: f.default for f in Config.__dataclass_fields__.values()}
        assert probe.DEFAULT_CHIP_FORCE_USER_AGENT == defaults["chip_force_user_agent"]
        assert probe.DEFAULT_CHIP_REFERER == defaults["chip_referer"]
        assert probe.DEFAULT_CHIP_MAX_RETRIES == defaults["chip_max_retries"]

    def test_probe_chip_retries_and_reports_attempts(self):
        """probe 侧同样要重试，并把 attempts 写进产物供复盘。"""
        probe = self._load_probe()
        calls = {"n": 0}

        class _Flaky:
            def stock_cyq_em(self, symbol=None, **_kw):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise ConnectionError("RemoteDisconnected")
                return [{"日期": "2026-02-27"}]

        res = probe.probe_chip_one("600036", akshare=_Flaky(), retry_backoff=0.0)

        assert res["status"] == "ok"
        assert res["attempts"] == 3
        assert res["anti_crawl"]["force_user_agent"] is True
        assert res["anti_crawl"]["max_retries"] == 2

    def test_probe_chip_patch_restores_requests(self):
        """probe 的补丁同样必须还原，不能影响后续 raw 探测。"""
        probe = self._load_probe()
        import requests

        original = requests.sessions.Session.request
        with probe.chip_anti_crawl_patch():
            assert requests.sessions.Session.request is not original
        assert requests.sessions.Session.request is original

    def test_probe_cli_exposes_anti_crawl_switches(self):
        """--no-chip-anti-crawl / --chip-max-retries 必须存在，供对照实验。"""
        import subprocess

        proc = subprocess.run(
            [sys.executable, self.SCRIPT, "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"stderr={proc.stderr[-500:]}"
        assert "--no-chip-anti-crawl" in proc.stdout
        assert "--chip-max-retries" in proc.stdout
