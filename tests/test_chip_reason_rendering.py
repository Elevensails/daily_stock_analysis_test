# -*- coding: utf-8 -*-
"""P1.6 T03 — 原因码消费端（文案分流 + ContextFieldStatus）的独立 QA 验证。

对应 docs/p1.6-arch-design.md §3.2.5 / §7.3、PRD P1-3 与 DoD 第 5 项
「ETF 标的报告使用『不适用』文案，不再显示『数据源暂不可用』」。

本文件由 QA 独立补充（设计 §2.1 规划了 tests/test_chip_reason_rendering.py
但工程师交付时未创建）。

环境说明
--------
``src/analyzer.py`` 依赖三方包 ``json_repair``（已在 requirements.txt 声明、
CI 具备），当前沙箱未安装。这属于**环境缺失而非源码缺陷**，因此本文件用
``sys.modules`` 注入轻量 stub 做隔离，保证分支逻辑仍可被验证。
"""

import sys
import types

import pytest

from src.report_language import get_chip_unavailable_text

LANGUAGES = ["zh", "en", "ko"]


@pytest.fixture(scope="module", autouse=True)
def _stub_json_repair():
    """隔离沙箱缺失的 json_repair，使 analyzer 可被导入（非源码缺陷）。"""
    if "json_repair" not in sys.modules:
        try:
            import json_repair  # noqa: F401
        except ImportError:
            stub = types.ModuleType("json_repair")
            stub.repair_json = lambda s, **kwargs: s
            sys.modules["json_repair"] = stub
    yield


# ─────────────────────────────────────────────────────────────
# 1. 文案分流：不适用 vs 暂不可用（PRD P1-3 / DoD 第 5 项）
# ─────────────────────────────────────────────────────────────
class TestChipTextRouting:
    """核心验收：ETF 的「不适用」与 A 股故障的「暂不可用」必须是不同文案。"""

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_not_applicable_differs_from_fetch_failed(self, language):
        na_text = get_chip_unavailable_text(language, "not_applicable")
        failed_text = get_chip_unavailable_text(language, "fetch_failed")

        assert na_text != failed_text, "两类语义必须使用不同文案（P1-3）"
        assert na_text.strip() and failed_text.strip()

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_not_applicable_no_longer_uses_legacy_shared_text(self, language):
        """DoD 第 5 项：ETF 不得再落到历史那句共用文案。"""
        legacy = get_chip_unavailable_text(language)  # reason=None → 历史通用文案
        na_text = get_chip_unavailable_text(language, "not_applicable")

        assert na_text != legacy

    def test_not_applicable_zh_text_is_not_failure_worded(self):
        """US-3：ETF 是正常现象，措辞不得像故障。"""
        text = get_chip_unavailable_text("zh", "not_applicable")

        assert "不适用" in text
        # 不得出现故障式措辞
        for bad in ["暂不可用", "获取失败", "未启用"]:
            assert bad not in text, f"「不适用」文案不应包含故障措辞: {bad}"

    def test_fetch_failed_zh_text_is_failure_worded(self):
        """对照组：A 股真失败必须明确说失败。"""
        text = get_chip_unavailable_text("zh", "fetch_failed")

        assert "失败" in text or "不可用" in text
        assert "不适用" not in text

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_disabled_has_dedicated_text(self, language):
        """DISABLED 有独立文案，不与故障混淆。"""
        disabled = get_chip_unavailable_text(language, "disabled")
        failed = get_chip_unavailable_text(language, "fetch_failed")

        assert disabled != failed


class TestChipTextBackwardCompatibility:
    """§7.6 红线：get_chip_unavailable_text(language) 单参调用不得破坏。"""

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_single_arg_call_still_works(self, language):
        text = get_chip_unavailable_text(language)
        assert isinstance(text, str) and text.strip()

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_reason_none_equals_legacy_text(self, language):
        assert get_chip_unavailable_text(language, None) == get_chip_unavailable_text(language)

    @pytest.mark.parametrize(
        "reason", ["empty", "no_credential", "circuit_open", "ok", "garbage", ""]
    )
    def test_unmapped_reasons_fall_back_to_generic_text(self, reason):
        """未映射原因码一律回落通用文案，保证零破坏。"""
        assert get_chip_unavailable_text("zh", reason) == get_chip_unavailable_text("zh")

    def test_unknown_language_does_not_raise(self):
        for reason in [None, "not_applicable", "fetch_failed"]:
            text = get_chip_unavailable_text("fr", reason)
            assert isinstance(text, str) and text.strip()


# ─────────────────────────────────────────────────────────────
# 2. analysis_context_builder：reason → ContextFieldStatus
# ─────────────────────────────────────────────────────────────
class TestContextStatusMapping:
    """§3.2.5 / §7.3：ETF → NOT_SUPPORTED；A 股失败 → FETCH_FAILED。"""

    @staticmethod
    def _resolve(metadata):
        from src.services.analysis_context_builder import _resolve_chip_missing_status

        return _resolve_chip_missing_status(metadata)

    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("not_applicable", "not_supported"),
            ("fetch_failed", "fetch_failed"),
            ("empty", "missing"),
            ("disabled", "missing"),
            ("no_credential", "missing"),
            ("circuit_open", "missing"),
        ],
    )
    def test_chip_reason_maps_to_expected_status(self, reason, expected):
        status, _ = self._resolve({"chip_reason": reason})
        assert status.value == expected

    def test_not_applicable_missing_reason_stays_legacy_key(self):
        """NOT_SUPPORTED 的 missing_reason 保持历史串，便于下游零改动。"""
        status, missing_reason = self._resolve({"chip_reason": "not_applicable"})
        assert status.value == "not_supported"
        assert missing_reason == "chip_not_supported"

    def test_legacy_chip_not_supported_bool_still_honored(self):
        """§7.6 红线：既有布尔 key 必须继续生效。"""
        status, missing_reason = self._resolve({"chip_not_supported": True})
        assert status.value == "not_supported"
        assert missing_reason == "chip_not_supported"

    def test_empty_metadata_falls_back_to_missing(self):
        status, missing_reason = self._resolve({})
        assert status.value == "missing"
        assert missing_reason == "chip_distribution_missing"

    def test_none_metadata_does_not_raise(self):
        status, _ = self._resolve(None)
        assert status.value == "missing"

    def test_etf_and_a_share_failure_produce_different_status(self):
        """DoD 第 5 项在 context 层的对应断言。"""
        etf_status, _ = self._resolve({"chip_reason": "not_applicable"})
        fail_status, _ = self._resolve({"chip_reason": "fetch_failed"})
        assert etf_status is not fail_status


# ─────────────────────────────────────────────────────────────
# 3. analyzer 层文案分流（json_repair 已 stub 隔离）
# ─────────────────────────────────────────────────────────────
class TestAnalyzerChipBranching:
    """analyzer 把 context['chip_reason'] 正确透传给文案函数。"""

    @staticmethod
    def _fake_result():
        """构造带 dashboard.data_perspective 的最小 AnalysisResult 替身。"""
        return types.SimpleNamespace(
            dashboard={"data_perspective": {"chip_structure": {"foo": 1}}}
        )

    def _render(self, reason=...):
        from src.analyzer import _mark_chip_structure_unavailable

        result = self._fake_result()
        if reason is ...:
            _mark_chip_structure_unavailable(result, "zh")
        else:
            _mark_chip_structure_unavailable(result, "zh", reason)
        return result.dashboard["data_perspective"]

    def test_mark_chip_structure_unavailable_routes_by_reason(self):
        na_text = self._render("not_applicable")["chip_unavailable_reason"]
        failed_text = self._render("fetch_failed")["chip_unavailable_reason"]

        assert na_text == get_chip_unavailable_text("zh", "not_applicable")
        assert failed_text == get_chip_unavailable_text("zh", "fetch_failed")
        assert na_text != failed_text, "analyzer 层必须分流，不得共用一句文案"

    def test_mark_chip_structure_unavailable_reason_optional(self):
        """§7.6：不传 reason 时保持历史行为。"""
        perspective = self._render()
        assert perspective["chip_unavailable_reason"] == get_chip_unavailable_text("zh")

    def test_chip_structure_is_collapsed(self):
        """无论哪种原因码，chip_structure 都应被清空。"""
        for reason in ["not_applicable", "fetch_failed", None]:
            perspective = self._render(reason)
            assert perspective["chip_structure"] == {}


# ─────────────────────────────────────────────────────────────
# 4. 端到端语义一致性：ChipFetchResult → 文案
# ─────────────────────────────────────────────────────────────
class TestReasonToTextEndToEnd:
    """把数据层结果对象直接喂给渲染层，验证跨层契约闭环。"""

    def test_etf_result_renders_not_applicable_text(self):
        from data_provider.realtime_types import ChipFetchResult

        result = ChipFetchResult.na("ETF/指数无筹码分布数据", "akshare")
        text = get_chip_unavailable_text("zh", result.reason.value)

        assert "不适用" in text
        assert result.to_context_status() == "not_supported"

    def test_a_share_failure_renders_unavailable_text(self):
        from data_provider.realtime_types import ChipFetchResult

        result = ChipFetchResult.failed("ConnectionError", "akshare")
        text = get_chip_unavailable_text("zh", result.reason.value)

        assert "不适用" not in text
        assert result.to_context_status() == "fetch_failed"
