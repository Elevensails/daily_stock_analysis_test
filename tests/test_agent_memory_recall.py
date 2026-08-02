# -*- coding: utf-8 -*-
"""
===================================
U14 T04 — AgentMemory.recall_similar 薄委托 + LongTermMemory 门面
===================================

覆盖设计 §7.2 T04 验收项：
1. ``AgentMemory.recall_similar`` 纯委托给 ``LongTermMemory.recall_for_stock``
2. **正交性**：``agent_memory_enabled=false`` + ``ltm_enabled=true`` 仍能召回
   （recall_similar 不检查 ``self.enabled``）
3. ``ltm_enabled=false`` → 返回 ``[]``
4. store 抛异常 → ``recall()`` 返回 ``degraded=True / store_error``，**不抛**
5. 门面 ``remember`` / ``flush`` 全链路 fail-open
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent.memory import AgentMemory  # noqa: E402
from src.memory.models import (  # noqa: E402
    SCOPE_GLOBAL,
    SCOPE_SAME_STOCK,
    RecallItem,
    RecallResult,
)
from src.memory.recall import LongTermMemory  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class _FakeConfig:
    """最小 config 替身，只暴露 U14 关心的 ltm_* 字段。"""

    def __init__(self, **overrides):
        self.ltm_enabled = True
        self.ltm_scope = SCOPE_SAME_STOCK
        self.ltm_top_k = 3
        self.ltm_min_similarity = 0.75
        self.ltm_lookback_days = 90
        self.ltm_halflife_days = 60
        self.ltm_write_mode = "batch_end"
        self.ltm_conclusion_max_chars = 500
        self.agent_memory_enabled = False
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeProvider:
    provider_name = "local"
    model_id = "local-lexical-v1"
    dim = 8

    def __init__(self, raise_on_embed: bool = False):
        self.raise_on_embed = raise_on_embed
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.raise_on_embed:
            raise RuntimeError("embed boom")
        return np.ones((len(texts), self.dim), dtype=np.float32)


class _FakeStore:
    def __init__(self, result=None, raise_on_search: bool = False):
        self._result = result
        self.raise_on_search = raise_on_search
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if self.raise_on_search:
            raise RuntimeError("store boom")
        return self._result if self._result is not None else RecallResult.empty(None)


class _FakeWriter:
    def __init__(self, provider, store, stage_raises=False, flush_raises=False):
        self.provider = provider
        self.store = store
        self.staged = []
        self.flushed = 0
        self.stage_raises = stage_raises
        self.flush_raises = flush_raises

    def stage(self, **kwargs):
        if self.stage_raises:
            raise RuntimeError("stage boom")
        self.staged.append(kwargs)
        return kwargs

    def flush(self):
        if self.flush_raises:
            raise RuntimeError("flush boom")
        self.flushed += 1
        return {
            "pending": len(self.staged),
            "embedded": len(self.staged),
            "written": len(self.staged),
            "skipped": 0,
            "degraded": False,
            "reason": None,
            "elapsed_ms": 1.0,
        }


def _make_item(history_id=1, similarity=0.9, code="600036", name="招商银行"):
    return RecallItem(
        history_id=history_id,
        stock_code=code,
        stock_name=name,
        trade_date="2026-07-01",
        time_slot="1500",
        age_days=3,
        similarity=similarity,
        final_score=similarity,
        conclusion_text="趋势：震荡上行；建议：持有观望。",
        sentiment_score=62,
        operation_advice="持有",
    )


def _wire(ltm, provider=None, store=None, writer=None):
    """把 fake 组件直接注入门面，跳过惰性构造。"""
    provider = provider or _FakeProvider()
    store = store or _FakeStore()
    writer = writer or _FakeWriter(provider, store)
    ltm._writer = writer
    ltm._provider = provider
    ltm._store = store
    return ltm


# --------------------------------------------------------------------------- #
# LongTermMemory 门面
# --------------------------------------------------------------------------- #


class TestLongTermMemoryFacade:
    def test_enabled_reads_ltm_enabled_only(self):
        assert LongTermMemory(_FakeConfig(ltm_enabled=True)).enabled is True
        assert LongTermMemory(_FakeConfig(ltm_enabled=False)).enabled is False

    def test_enabled_orthogonal_to_agent_memory_enabled(self):
        """裁决 B：ltm_enabled ⟂ agent_memory_enabled，不得连坐。"""
        cfg = _FakeConfig(ltm_enabled=True, agent_memory_enabled=False)
        assert LongTermMemory(cfg).enabled is True

        cfg2 = _FakeConfig(ltm_enabled=False, agent_memory_enabled=True)
        assert LongTermMemory(cfg2).enabled is False

    def test_missing_attr_defaults_to_disabled(self):
        class _Bare:
            pass

        assert LongTermMemory(_Bare()).enabled is False

    def test_recall_disabled_returns_empty_result(self):
        ltm = LongTermMemory(_FakeConfig(ltm_enabled=False))
        result = ltm.recall_for_stock("600036", "今日震荡")
        assert isinstance(result, RecallResult)
        assert result.enabled is False
        assert result.hit_count == 0
        assert result.items == []
        assert result.degrade_reason == "disabled"

    def test_recall_disabled_does_not_build_writer(self):
        ltm = LongTermMemory(_FakeConfig(ltm_enabled=False))
        ltm.recall_for_stock("600036", "今日震荡")
        assert ltm._writer is None, "关闭时不应触发任何惰性构造"

    def test_recall_happy_path(self):
        expected = RecallResult(
            enabled=True,
            hit_count=1,
            candidate_count=5,
            items=[_make_item()],
        )
        store = _FakeStore(result=expected)
        ltm = _wire(LongTermMemory(_FakeConfig()), store=store)

        result = ltm.recall_for_stock("600036", "今日放量突破")
        assert result.hit_count == 1
        assert result.stock_code == "600036"
        assert result.scope == SCOPE_SAME_STOCK
        assert len(store.queries) == 1
        assert store.queries[0].top_k == 3
        assert store.queries[0].min_similarity == pytest.approx(0.75)

    def test_recall_overrides_take_precedence(self):
        store = _FakeStore()
        ltm = _wire(LongTermMemory(_FakeConfig()), store=store)
        ltm.recall_for_stock(
            "600036", "文本", top_k=7, min_similarity=0.4, scope=SCOPE_GLOBAL
        )
        q = store.queries[0]
        assert q.top_k == 7
        assert q.min_similarity == pytest.approx(0.4)
        assert q.scope == SCOPE_GLOBAL

    def test_recall_empty_query_text_is_not_degraded(self):
        ltm = _wire(LongTermMemory(_FakeConfig()))
        result = ltm.recall_for_stock("600036", "   ")
        assert result.hit_count == 0
        assert result.degraded is False
        assert result.degrade_reason is None

    def test_recall_embed_failure_degrades(self):
        provider = _FakeProvider(raise_on_embed=True)
        ltm = _wire(LongTermMemory(_FakeConfig()), provider=provider)
        result = ltm.recall_for_stock("600036", "今日放量")
        assert result.degraded is True
        assert result.degrade_reason == "embed_failed"
        assert result.items == []

    def test_recall_store_exception_degrades_without_raising(self):
        """T04 验收：store 抛异常 → degraded=True / store_error，且不外抛。"""
        store = _FakeStore(raise_on_search=True)
        ltm = _wire(LongTermMemory(_FakeConfig()), store=store)
        result = ltm.recall_for_stock("600036", "今日放量")
        assert result.degraded is True
        assert result.degrade_reason == "store_error"
        assert result.items == []
        assert result.stock_code == "600036"

    def test_recall_writer_construction_failure_degrades(self):
        cfg = _FakeConfig()
        ltm = LongTermMemory(cfg)

        def _boom():
            raise RuntimeError("writer boom")

        ltm._ensure_writer = _boom  # type: ignore[assignment]
        result = ltm.recall_for_stock("600036", "文本")
        assert result.degraded is True
        assert result.degrade_reason == "store_error"

    def test_recall_alias_points_to_recall_for_stock(self):
        assert LongTermMemory.recall is LongTermMemory.recall_for_stock

    def test_recall_never_raises_on_garbage_input(self):
        ltm = _wire(LongTermMemory(_FakeConfig()))
        for bad in (None, 123, object()):
            result = ltm.recall_for_stock(bad, bad)  # type: ignore[arg-type]
            assert isinstance(result, RecallResult)

    def test_remember_delegates_to_writer(self):
        writer = _FakeWriter(_FakeProvider(), _FakeStore())
        ltm = _wire(LongTermMemory(_FakeConfig()), writer=writer)
        out = ltm.remember(
            history_id=11,
            code="600036",
            name="招商银行",
            trend="震荡上行",
            advice="持有",
            summary="量能温和放大",
        )
        assert out is not None
        assert len(writer.staged) == 1
        assert writer.staged[0]["history_id"] == 11

    def test_remember_disabled_returns_none(self):
        writer = _FakeWriter(_FakeProvider(), _FakeStore())
        ltm = _wire(LongTermMemory(_FakeConfig(ltm_enabled=False)), writer=writer)
        assert ltm.remember(history_id=1, code="600036") is None
        assert writer.staged == []

    def test_remember_swallows_writer_exception(self):
        writer = _FakeWriter(_FakeProvider(), _FakeStore(), stage_raises=True)
        ltm = _wire(LongTermMemory(_FakeConfig()), writer=writer)
        assert ltm.remember(history_id=1, code="600036", summary="x") is None

    def test_flush_delegates_to_writer(self):
        writer = _FakeWriter(_FakeProvider(), _FakeStore())
        ltm = _wire(LongTermMemory(_FakeConfig()), writer=writer)
        ltm.remember(history_id=1, code="600036", summary="x")
        stats = ltm.flush()
        assert stats["written"] == 1
        assert stats["degraded"] is False
        assert writer.flushed == 1

    def test_flush_disabled_short_circuits(self):
        writer = _FakeWriter(_FakeProvider(), _FakeStore())
        ltm = _wire(LongTermMemory(_FakeConfig(ltm_enabled=False)), writer=writer)
        stats = ltm.flush()
        assert stats["reason"] == "disabled"
        assert stats["written"] == 0
        assert writer.flushed == 0

    def test_flush_swallows_writer_exception(self):
        writer = _FakeWriter(_FakeProvider(), _FakeStore(), flush_raises=True)
        ltm = _wire(LongTermMemory(_FakeConfig()), writer=writer)
        stats = ltm.flush()
        assert stats["degraded"] is True
        assert stats["reason"] == "store_error"

    def test_from_config_uses_injected_config(self):
        cfg = _FakeConfig(ltm_enabled=True)
        ltm = LongTermMemory.from_config(cfg)
        assert ltm.enabled is True
        assert ltm._config is cfg

    def test_from_config_lazy_does_not_build_components(self):
        ltm = LongTermMemory.from_config(_FakeConfig())
        assert ltm._writer is None
        assert ltm._provider is None
        assert ltm._store is None

    def test_ensure_writer_caches(self, monkeypatch):
        cfg = _FakeConfig()
        provider = _FakeProvider()
        store = _FakeStore()
        writer = _FakeWriter(provider, store)
        calls = {"n": 0}

        def _fake_build(config):
            calls["n"] += 1
            return writer

        monkeypatch.setattr("src.memory.writer.build_memory_writer", _fake_build)
        ltm = LongTermMemory(cfg)
        assert ltm._ensure_writer() is writer
        assert ltm._ensure_writer() is writer
        assert calls["n"] == 1
        assert ltm._provider is provider
        assert ltm._store is store


# --------------------------------------------------------------------------- #
# AgentMemory.recall_similar 薄委托
# --------------------------------------------------------------------------- #


def _patch_ltm(monkeypatch, ltm):
    """把 src.memory.LongTermMemory.from_config 换成返回指定门面。"""
    import src.memory.recall as recall_mod

    monkeypatch.setattr(
        recall_mod.LongTermMemory, "from_config", classmethod(lambda cls, config=None: ltm)
    )


class TestAgentMemoryRecallSimilar:
    def test_method_exists_and_is_additive(self):
        assert hasattr(AgentMemory, "recall_similar")
        assert callable(AgentMemory.recall_similar)

    def test_returns_safe_dicts(self, monkeypatch):
        expected = RecallResult(enabled=True, hit_count=2, items=[_make_item(1, 0.91), _make_item(2, 0.80)])
        ltm = _wire(LongTermMemory(_FakeConfig()), store=_FakeStore(result=expected))
        _patch_ltm(monkeypatch, ltm)

        mem = AgentMemory(enabled=False)
        out = mem.recall_similar("600036", "今日放量突破")
        assert isinstance(out, list)
        assert len(out) == 2
        assert isinstance(out[0], dict)
        # to_safe_dict 不得泄漏原始向量
        assert "embedding" not in out[0]
        assert out[0]["history_id"] == 1

    def test_orthogonality_agent_memory_disabled_still_recalls(self, monkeypatch):
        """T04 验收：agent_memory_enabled=false + ltm_enabled=true → 照常召回。"""
        expected = RecallResult(enabled=True, hit_count=1, items=[_make_item()])
        ltm = _wire(LongTermMemory(_FakeConfig(agent_memory_enabled=False)),
                    store=_FakeStore(result=expected))
        _patch_ltm(monkeypatch, ltm)

        mem = AgentMemory(enabled=False)  # agent_memory_enabled=false
        assert mem.enabled is False
        out = mem.recall_similar("600036", "今日放量突破")
        assert len(out) == 1, "recall_similar 不得检查 self.enabled"

    def test_ltm_disabled_returns_empty_list(self, monkeypatch):
        ltm = LongTermMemory(_FakeConfig(ltm_enabled=False))
        _patch_ltm(monkeypatch, ltm)
        mem = AgentMemory(enabled=True)
        assert mem.recall_similar("600036", "文本") == []

    def test_store_exception_returns_empty_list(self, monkeypatch):
        ltm = _wire(LongTermMemory(_FakeConfig()), store=_FakeStore(raise_on_search=True))
        _patch_ltm(monkeypatch, ltm)
        mem = AgentMemory(enabled=True)
        assert mem.recall_similar("600036", "文本") == []

    def test_facade_construction_failure_returns_empty_list(self, monkeypatch):
        import src.memory.recall as recall_mod

        def _boom(cls, config=None):
            raise RuntimeError("from_config boom")

        monkeypatch.setattr(recall_mod.LongTermMemory, "from_config", classmethod(_boom))
        mem = AgentMemory(enabled=True)
        assert mem.recall_similar("600036", "文本") == []

    def test_overrides_are_forwarded(self, monkeypatch):
        store = _FakeStore()
        ltm = _wire(LongTermMemory(_FakeConfig()), store=store)
        _patch_ltm(monkeypatch, ltm)

        mem = AgentMemory(enabled=True)
        mem.recall_similar("600036", "文本", top_k=5, min_similarity=0.3, scope=SCOPE_GLOBAL)
        q = store.queries[0]
        assert q.top_k == 5
        assert q.min_similarity == pytest.approx(0.3)
        assert q.scope == SCOPE_GLOBAL

    def test_never_raises(self, monkeypatch):
        ltm = _wire(LongTermMemory(_FakeConfig()))
        _patch_ltm(monkeypatch, ltm)
        mem = AgentMemory(enabled=True)
        for bad in (None, 0, object()):
            assert isinstance(mem.recall_similar(bad, bad), list)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 包级导出
# --------------------------------------------------------------------------- #


class TestPackageExports:
    def test_lazy_exports_resolve(self):
        import src.memory as memory_pkg

        assert memory_pkg.LongTermMemory is LongTermMemory
        from src.memory.formatter import format_memory_recall_section

        assert memory_pkg.format_memory_recall_section is format_memory_recall_section

    def test_all_contains_new_symbols(self):
        import src.memory as memory_pkg

        assert "LongTermMemory" in memory_pkg.__all__
        assert "format_memory_recall_section" in memory_pkg.__all__

    def test_no_tiktoken_import_in_memory_package(self):
        """铁律 ②：src/memory/ 零 tiktoken 依赖。"""
        import ast
        import pathlib

        pkg_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "memory"
        offenders = []
        for py in pkg_dir.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "tiktoken":
                            offenders.append(py.name)
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] == "tiktoken":
                        offenders.append(py.name)
        assert offenders == [], f"src/memory 出现 tiktoken 导入: {offenders}"
