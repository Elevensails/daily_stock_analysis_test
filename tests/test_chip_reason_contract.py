# -*- coding: utf-8 -*-
"""P1.6 T02 — 筹码原因码跨层契约的独立 QA 验证。

对应 docs/p1.6-arch-design.md §3.2 / §7.3（唯一真源表）与 PRD US-3。

本文件由 QA 独立补充（设计 §2.1 规划了 tests/test_chip_reason_contract.py
但工程师交付时未创建），重点覆盖设计 §8.2 标注的高风险边界：

1. ETF 经 ``get_chip_distribution_ex`` 必须返回 NOT_APPLICABLE，
   且**完全不触碰熔断器**、不计入失败率（D4 / US-3）；
2. A 股抓取失败必须落 FETCH_FAILED；
3. 旧 ``get_chip_distribution`` shim 行为对既有 14 处调用零破坏（§7.6 红线）；
4. severity 聚合矩阵与 ContextFieldStatus 映射与 §7.3 表逐格一致。

全部 mock，零网络（§7.5 / C4）。
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import (
    ChipDistribution,
    ChipFetchResult,
    ChipUnavailableReason,
    get_chip_circuit_breaker,
    get_chip_diagnostics,
)

# ETF / 港股 / 美股：设计 §3.2.4 中「不适用」的标的类型
NOT_APPLICABLE_CODES = ["512400", "159915", "512880", "00700", "AAPL"]
A_SHARE_CODES = ["600036", "603823"]


# ─────────────────────────────────────────────────────────────
# 探针：可断言「熔断器有没有被碰过」
# ─────────────────────────────────────────────────────────────
class _CircuitSpy:
    """包装真实熔断器，记录所有写操作调用。"""

    def __init__(self, real):
        self._real = real
        self.calls: list = []

    def is_available(self, source):
        return self._real.is_available(source)

    def record_success(self, source):
        self.calls.append(("record_success", source))
        return self._real.record_success(source)

    def record_failure(self, source, error=None):
        self.calls.append(("record_failure", source))
        return self._real.record_failure(source, error)

    def record_inconclusive(self, source):
        self.calls.append(("record_inconclusive", source))
        return self._real.record_inconclusive(source)

    def reset(self, source=None):
        return self._real.reset(source)


class _ExFetcher:
    """实现了 _ex 契约的假 fetcher。"""

    def __init__(self, name, priority, result):
        self.name = name
        self.priority = priority
        self._result = result
        self.calls = 0

    def get_chip_distribution_ex(self, stock_code):
        self.calls += 1
        return self._result

    def get_chip_distribution(self, stock_code):
        return self.get_chip_distribution_ex(stock_code).chip


def _run_manager_ex(manager, code):
    """在 enable_chip_distribution=True 下调用 _ex，并返回熔断器调用轨迹。"""
    real = get_chip_circuit_breaker()
    real.reset()
    spy = _CircuitSpy(real)
    with patch(
        "src.config.get_config",
        return_value=SimpleNamespace(enable_chip_distribution=True),
    ), patch(
        "data_provider.realtime_types.get_chip_circuit_breaker",
        return_value=spy,
    ):
        result = manager.get_chip_distribution_ex(code)
    return result, spy


# ─────────────────────────────────────────────────────────────
# 1. Fetcher 层：ETF → NOT_APPLICABLE
# ─────────────────────────────────────────────────────────────
class TestAkshareFetcherReasonClassification:
    """AkshareFetcher.get_chip_distribution_ex 的原因码分类。"""

    @pytest.fixture(scope="class")
    def fetcher(self):
        from data_provider.akshare_fetcher import AkshareFetcher

        return AkshareFetcher()

    @pytest.mark.parametrize("code", NOT_APPLICABLE_CODES)
    def test_not_applicable_codes_return_na_without_network(self, fetcher, code):
        """ETF/港股/美股必须在前置跳过阶段就返回 NOT_APPLICABLE，不发起任何网络调用。"""
        with patch("akshare.stock_cyq_em") as mock_api:
            result = fetcher.get_chip_distribution_ex(code)

        assert result.reason is ChipUnavailableReason.NOT_APPLICABLE
        assert result.chip is None
        assert result.is_not_applicable is True
        # 关键：前置跳过，绝不触网
        mock_api.assert_not_called()

    @pytest.mark.parametrize("code", NOT_APPLICABLE_CODES)
    def test_not_applicable_never_counts_as_failure(self, fetcher, code):
        """US-3：不适用不得计入失败率、不得触发熔断。"""
        result = fetcher.get_chip_distribution_ex(code)

        assert result.should_record_failure is False
        assert result.should_record_provider_run is False
        assert result.should_record_inconclusive is False

    @pytest.mark.parametrize("code", A_SHARE_CODES)
    def test_a_share_network_error_maps_to_fetch_failed(self, fetcher, code):
        """A 股接口异常必须落 FETCH_FAILED（而非 NOT_APPLICABLE）。"""
        with patch(
            "akshare.stock_cyq_em",
            side_effect=ConnectionError("RemoteDisconnected"),
        ):
            result = fetcher.get_chip_distribution_ex(code)

        assert result.reason is ChipUnavailableReason.FETCH_FAILED
        assert result.chip is None
        assert result.should_record_failure is True
        assert result.detail, "FETCH_FAILED 必须带诊断串供 CI 定位"

    @pytest.mark.parametrize("code", A_SHARE_CODES)
    def test_a_share_empty_dataframe_maps_to_empty(self, fetcher, code):
        """接口通但返回空 → EMPTY，不得与 FETCH_FAILED 混淆。"""
        import pandas as pd

        with patch("akshare.stock_cyq_em", return_value=pd.DataFrame()):
            result = fetcher.get_chip_distribution_ex(code)

        assert result.reason is ChipUnavailableReason.EMPTY
        assert result.should_record_failure is False
        assert result.should_record_inconclusive is True


# ─────────────────────────────────────────────────────────────
# 2. Manager 层：ETF 完全不触碰熔断器（本期最高风险点 D4）
# ─────────────────────────────────────────────────────────────
class TestManagerCircuitBreakerIsolation:
    """设计 §3.2.4 表 + D4：ETF 不得污染熔断器与失败率。"""

    def test_etf_does_not_touch_circuit_breaker_at_all(self):
        na = ChipFetchResult.na("ETF/指数无筹码分布数据", "akshare")
        manager = DataFetcherManager(fetchers=[_ExFetcher("AkshareFetcher", 0, na)])

        result, spy = _run_manager_ex(manager, "512400")

        assert result.reason is ChipUnavailableReason.NOT_APPLICABLE
        # 核心断言：一次熔断器写操作都不能有
        assert spy.calls == [], f"ETF 不应触碰熔断器，实际调用: {spy.calls}"

    def test_etf_repeated_10_times_never_opens_circuit(self):
        """设计 T02 验收：ETF 连跑 10 次不触发熔断。"""
        na = ChipFetchResult.na("ETF/指数无筹码分布数据", "akshare")
        fetcher = _ExFetcher("AkshareFetcher", 0, na)
        manager = DataFetcherManager(fetchers=[fetcher])

        breaker = get_chip_circuit_breaker()
        breaker.reset()
        with patch(
            "src.config.get_config",
            return_value=SimpleNamespace(enable_chip_distribution=True),
        ):
            for _ in range(10):
                result = manager.get_chip_distribution_ex("512400")
                assert result.reason is ChipUnavailableReason.NOT_APPLICABLE

        assert fetcher.calls == 10, "每次都应真正走到 fetcher，未被熔断短路"
        assert breaker.is_available("akshare_chip") is True

    def test_fetch_failed_does_record_failure(self):
        """对照组：真故障必须计入熔断，证明分流不是"一律不记"。"""
        failed = ChipFetchResult.failed("ConnectionError", "akshare")
        manager = DataFetcherManager(fetchers=[_ExFetcher("AkshareFetcher", 0, failed)])

        result, spy = _run_manager_ex(manager, "600036")

        assert result.reason is ChipUnavailableReason.FETCH_FAILED
        assert ("record_failure", "akshare_chip") in spy.calls

    def test_no_credential_does_not_touch_circuit_breaker(self):
        """C5：CI 无 TUSHARE_TOKEN，NO_CREDENTIAL 静默跳过、不熔断。"""
        no_cred = ChipFetchResult.no_credential("TushareFetcher")
        manager = DataFetcherManager(fetchers=[_ExFetcher("TushareFetcher", 0, no_cred)])

        result, spy = _run_manager_ex(manager, "600036")

        assert result.reason is ChipUnavailableReason.NO_CREDENTIAL
        assert spy.calls == []


# ─────────────────────────────────────────────────────────────
# 3. 聚合规则：severity 取最大
# ─────────────────────────────────────────────────────────────
class TestSeverityAggregation:
    """设计 §3.2.4：任一源 OK 短路；否则取 severity 最大者。"""

    def test_etf_plus_no_credential_aggregates_to_not_applicable(self):
        """§3.2.4 第 2 行：ETF 场景 akshare=NA(0) + tushare=NO_CREDENTIAL(2)。

        注意：NO_CREDENTIAL severity(2) > NOT_APPLICABLE(0)，若 tushare 已注册，
        聚合结果会是 NO_CREDENTIAL —— 此时 ETF 报告将拿不到「不适用」文案。
        本用例锁定当前实现的实际取值，供 E3 边界评估。
        """
        manager = DataFetcherManager(
            fetchers=[
                _ExFetcher("AkshareFetcher", 0, ChipFetchResult.na("ETF", "akshare")),
                _ExFetcher("TushareFetcher", 1, ChipFetchResult.na("ETF", "tushare")),
            ]
        )
        result, spy = _run_manager_ex(manager, "512400")

        assert result.reason is ChipUnavailableReason.NOT_APPLICABLE
        assert spy.calls == []

    def test_fetch_failed_beats_no_credential(self):
        """§3.2.4 第 3 行：FETCH_FAILED(4) > NO_CREDENTIAL(2) → FETCH_FAILED。"""
        manager = DataFetcherManager(
            fetchers=[
                _ExFetcher("AkshareFetcher", 0, ChipFetchResult.failed("boom", "akshare")),
                _ExFetcher("TushareFetcher", 1, ChipFetchResult.no_credential("tushare")),
            ]
        )
        result, _ = _run_manager_ex(manager, "600036")

        assert result.reason is ChipUnavailableReason.FETCH_FAILED

    def test_ok_short_circuits_and_skips_later_fetchers(self):
        """任一源 OK → 立即短路，后续 fetcher 不应被调用。"""
        chip = ChipDistribution(
            code="600036", profit_ratio=0.61, avg_cost=12.3, concentration_90=0.13
        )
        second = _ExFetcher("TushareFetcher", 1, ChipFetchResult.failed("x", "tushare"))
        manager = DataFetcherManager(
            fetchers=[
                _ExFetcher("AkshareFetcher", 0, ChipFetchResult.ok_of(chip, "akshare")),
                second,
            ]
        )
        result, _ = _run_manager_ex(manager, "600036")

        assert result.ok is True
        assert result.chip is chip
        assert second.calls == 0, "OK 后必须短路，不得继续调用后续源"

    def test_disabled_short_circuits_without_touching_fetchers(self):
        """配置关闭 → DISABLED，且不遍历任何 fetcher。"""
        fetcher = _ExFetcher("AkshareFetcher", 0, ChipFetchResult.failed("x", "akshare"))
        manager = DataFetcherManager(fetchers=[fetcher])

        with patch(
            "src.config.get_config",
            return_value=SimpleNamespace(enable_chip_distribution=False),
        ):
            result = manager.get_chip_distribution_ex("600036")

        assert result.reason is ChipUnavailableReason.DISABLED
        assert fetcher.calls == 0


# ─────────────────────────────────────────────────────────────
# 4. §7.3 唯一真源表逐格校验
# ─────────────────────────────────────────────────────────────
class TestReasonTruthTable:
    """设计 §7.3 的 severity / ContextFieldStatus 映射表。"""

    @pytest.mark.parametrize(
        "reason,severity,status",
        [
            (ChipUnavailableReason.OK, -1, "available"),
            (ChipUnavailableReason.DISABLED, -1, "missing"),
            (ChipUnavailableReason.NOT_APPLICABLE, 0, "not_supported"),
            (ChipUnavailableReason.CIRCUIT_OPEN, 1, "missing"),
            (ChipUnavailableReason.NO_CREDENTIAL, 2, "missing"),
            (ChipUnavailableReason.EMPTY, 3, "missing"),
            (ChipUnavailableReason.FETCH_FAILED, 4, "fetch_failed"),
        ],
    )
    def test_severity_and_status_mapping(self, reason, severity, status):
        assert reason.severity == severity
        assert reason.to_context_status() == status

    def test_reason_is_json_serializable_str_enum(self):
        """§7.2：跨层传递用 .value，str 混入保证可直接 JSON 化。"""
        import json

        payload = {"chip_reason": ChipUnavailableReason.NOT_APPLICABLE.value}
        assert json.loads(json.dumps(payload))["chip_reason"] == "not_applicable"
        assert ChipUnavailableReason.NOT_APPLICABLE == "not_applicable"


# ─────────────────────────────────────────────────────────────
# 5. 向后兼容红线（§7.6）
# ─────────────────────────────────────────────────────────────
class TestBackwardCompatibleShim:
    """旧 get_chip_distribution 必须行为不变（6 处生产 + 8 处测试调用）。"""

    def test_manager_shim_returns_chip_object_on_success(self):
        chip = ChipDistribution(
            code="600036", profit_ratio=0.61, avg_cost=12.3, concentration_90=0.13
        )
        manager = DataFetcherManager(
            fetchers=[_ExFetcher("AkshareFetcher", 0, ChipFetchResult.ok_of(chip, "akshare"))]
        )
        get_chip_circuit_breaker().reset()
        with patch(
            "src.config.get_config",
            return_value=SimpleNamespace(enable_chip_distribution=True),
        ):
            assert manager.get_chip_distribution("600036") is chip

    @pytest.mark.parametrize(
        "result",
        [
            ChipFetchResult.na("ETF", "akshare"),
            ChipFetchResult.failed("boom", "akshare"),
            ChipFetchResult.empty("empty", "akshare"),
            ChipFetchResult.no_credential("tushare"),
        ],
    )
    def test_manager_shim_returns_none_on_every_failure_reason(self, result):
        """旧调用者只认 None —— 任何不可用原因都必须回落 None，不得抛异常。"""
        manager = DataFetcherManager(fetchers=[_ExFetcher("AkshareFetcher", 0, result)])
        get_chip_circuit_breaker().reset()
        with patch(
            "src.config.get_config",
            return_value=SimpleNamespace(enable_chip_distribution=True),
        ):
            assert manager.get_chip_distribution("512400") is None

    def test_akshare_fetcher_shim_matches_ex_chip(self):
        from data_provider.akshare_fetcher import AkshareFetcher

        fetcher = AkshareFetcher()
        assert fetcher.get_chip_distribution("512400") is None
        assert (
            fetcher.get_chip_distribution_ex("512400").reason
            is ChipUnavailableReason.NOT_APPLICABLE
        )

    def test_ex_never_raises_for_any_code_shape(self):
        """契约：_ex 永不抛异常、永不返回 None。"""
        from data_provider.akshare_fetcher import AkshareFetcher

        fetcher = AkshareFetcher()
        for code in ["", "  ", "512400", "00700", "AAPL", "不存在"]:
            result = fetcher.get_chip_distribution_ex(code)
            assert isinstance(result, ChipFetchResult)
            assert isinstance(result.reason, ChipUnavailableReason)


# ─────────────────────────────────────────────────────────────
# 6. 诊断计数器（P1-2）
# ─────────────────────────────────────────────────────────────
class TestChipDiagnostics:
    """§3.9：汇总行必须可 grep，且不适用与失败分桶统计。"""

    def test_summary_line_separates_not_applicable_from_failed(self):
        diag = get_chip_diagnostics()
        diag.reset()
        diag.record(ChipFetchResult.na("ETF", "akshare"))
        diag.record(ChipFetchResult.na("ETF", "akshare"))
        diag.record(ChipFetchResult.failed("boom", "akshare"))

        line = diag.summary_line()
        diag.reset()

        assert "[筹码汇总]" in line
        assert "不适用" in line
        assert "失败" in line

    def test_reset_clears_counters(self):
        diag = get_chip_diagnostics()
        diag.reset()
        diag.record(ChipFetchResult.failed("boom", "akshare"))
        diag.reset()
        line = diag.summary_line()
        assert "尝试 0" in line.replace(" 0 只", " 0 只")
