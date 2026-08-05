# -*- coding: utf-8 -*-
"""
U12 语义缓存 — 行为覆盖 + 主链路端到端接入回归

与 ``tests/test_semantic_cache_partition.py`` 的分工：

    test_semantic_cache_partition.py  →  **安全底线**（铁律 #1/#3/#4，绝不误命中）
    test_semantic_cache.py（本文件）  →  **功能正确性**（该命中要命中、该省钱要省钱、
                                          该降级要静默降级、开关要真的能关）

本文件覆盖两层：

A. 门面行为（``SemanticCache``）
   B1  Tier-0 精确命中返回缓存文本
   B2  未命中返回 None（主链路据此走真实 LLM）
   B3  put 真正回写（行数 / 内容可核对）
   B4  ``enabled=False`` 时**不查也不写**（且不建任何 DB 连接）
   B5  存储异常 → 静默降级为未命中 / 未写入，绝不抛错
   B6  向量化异常 → 仍可精确命中（Tier-0 不依赖向量）
   B7  guards：响应过短 / 敏感 prompt 不进缓存
   B8  命中 usage 契约：三个 token 计数归零 + ``cache_hit=True``
   B9  ``should_persist_usage_telemetry`` 不会因 ``total_tokens=0`` 丢弃命中遥测
        （用户决策 ②，本条是"命中率可观测性"的唯一保障）

B. 主链路端到端（``GeminiAnalyzer._call_litellm``）
   E1  同 (code, trade_date, time_slot, prompt) 第二次调用命中，
       ``backend.generate`` 调用次数 == 1（真的没发网络请求）
   E2  跨 time_slot 第二次调用**不命中**（三层防御在真实链路上生效）
   E3  跨 trade_date 第二次调用**不命中**
   E4  ``sem_cache_enabled=False`` 时不命中且**一行都不写**
   E5  存储异常注入 → 主链路正常完成，只是退化为每次都调 LLM
   E6  ``cache_context=None``（``generate_text`` 路径）完全不碰缓存
   E7  ``stream=True`` 命中时进度回调恰好补一次
   E8  命中文本未通过 ``response_validator`` ⇒ 丢弃缓存、回落真实调用

无网络依赖：全部通过 mock ``GenerationBackend`` 与临时 SQLite 完成。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 与既有测试保持一致：缺少重量级运行时依赖时打桩，保证可独立运行
for _mod in ("litellm", "google.generativeai", "google.genai", "anthropic"):
    if _mod not in sys.modules:  # pragma: no cover - 环境相关
        try:
            __import__(_mod)
        except Exception:
            sys.modules[_mod] = MagicMock()

from src.cache.cache_store import SqliteSemanticCacheStore  # noqa: E402
from src.cache.models import (  # noqa: E402
    SEMCACHE_MODEL_ID,
    SEMCACHE_VECTOR_VERSION,
    CacheKey,
)
from src.cache.semantic_cache import SemanticCache  # noqa: E402

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()

PROMPT = "请基于以下技术面与资金面数据分析该标的的短期走势，并严格输出 JSON 结论。"
#: 必须超过默认 min_response_chars=200，否则会被 guards 判为不值得缓存
RESPONSE = '{"conclusion": "短期震荡整理，量能温和，建议观望。"}' + "补充说明。" * 60
SECOND_RESPONSE = '{"conclusion": "第二次真实调用的结论，与首次不同。"}' + "补充说明。" * 60

GEN_CONFIG: Dict[str, Any] = {"temperature": 0.3, "max_output_tokens": 4096}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch):
    """隔离的临时 SQLite 库（绝不触碰工程默认库）。"""
    from src.config import Config
    from src.storage import DatabaseManager

    workdir = tempfile.mkdtemp(prefix="semcache_behavior_")
    db_path = os.path.join(workdir, "test_semcache.db")

    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("ENV_FILE", os.path.join(workdir, ".env.absent"))

    Config._instance = None
    DatabaseManager.reset_instance()
    manager = DatabaseManager.get_instance()
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()
        Config._instance = None
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture
def store(temp_db) -> SqliteSemanticCacheStore:
    return SqliteSemanticCacheStore(
        SEMCACHE_MODEL_ID,
        db_manager=temp_db,
        vector_version=SEMCACHE_VECTOR_VERSION,
        max_candidates=64,
        store_prompt_text=True,
    )


@pytest.fixture
def cache(store, tmp_path) -> SemanticCache:
    """启用状态的门面（Tier-0 exact）。"""
    return SemanticCache(
        enabled=True,
        mode="exact",
        ttl_hours=12,
        min_response_chars=200,
        store=store,
        log_path=str(tmp_path / "semcache.jsonl"),
    )


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _key(
    cache_obj: SemanticCache,
    *,
    code: str = "600519",
    trade_date: str = TODAY,
    time_slot: str = "1800",
    report_type: str = "stock_analysis",
    llm_model: str = "deepseek/deepseek-chat",
    backend_id: str = "litellm",
    prompt: str = PROMPT,
    system_prompt: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
) -> CacheKey:
    key = cache_obj.build_key(
        prompt=prompt,
        system_prompt=system_prompt,
        generation_config=generation_config if generation_config is not None else GEN_CONFIG,
        code=code,
        trade_date=trade_date,
        time_slot=time_slot,
        report_type=report_type,
        llm_model=llm_model,
        backend_id=backend_id,
    )
    assert key is not None, "分区维度齐备时 build_key 不应返回 None"
    return key


class _ExplodingStore(SqliteSemanticCacheStore):
    """故障注入：所有读写路径都炸，用于验证"静默降级"。"""

    def lookup_exact(self, key, now=None):  # type: ignore[override]
        raise RuntimeError("injected: lookup_exact exploded")

    def upsert(self, entry):  # type: ignore[override]
        raise RuntimeError("injected: upsert exploded")

    def stats(self):  # type: ignore[override]
        raise RuntimeError("injected: stats exploded")


# =========================================================================== #
# A. 门面行为
# =========================================================================== #

class TestSemanticCacheBehaviour:
    """门面层的功能正确性（B1 ~ B9）。"""

    # ---- B1 / B2 / B3 ------------------------------------------------- #

    def test_b1_exact_hit_returns_cached_text(self, cache: SemanticCache):
        """Tier-0：写入后用完全相同的 key + prompt 查询，应原样返回缓存文本。"""
        key = _key(cache)
        assert cache.put(key, PROMPT, RESPONSE, response_model="deepseek/deepseek-chat") is True

        hit = cache.get(_key(cache), PROMPT)

        assert hit is not None
        assert hit.response_text == RESPONSE
        assert hit.tier == "exact"
        assert hit.similarity == pytest.approx(1.0)
        assert hit.response_model == "deepseek/deepseek-chat"

    def test_b2_miss_returns_none_for_unknown_prompt(self, cache: SemanticCache):
        """未写入过的 prompt 必须未命中（返回 None，主链路据此走真实 LLM）。"""
        cache.put(_key(cache), PROMPT, RESPONSE)

        other_prompt = PROMPT + "（追加一句不同的要求）"
        assert cache.get(_key(cache, prompt=other_prompt), other_prompt) is None

    def test_b2b_miss_on_empty_cache(self, cache: SemanticCache):
        """空库查询必须未命中且不抛错。"""
        assert cache.get(_key(cache), PROMPT) is None

    def test_b3_put_persists_exactly_one_row_and_is_idempotent(
        self, cache: SemanticCache, store: SqliteSemanticCacheStore
    ):
        """回写真的落盘；重复回写幂等，不产生重复行。"""
        key = _key(cache)
        assert store.count() == 0

        assert cache.put(key, PROMPT, RESPONSE) is True
        assert store.count() == 1

        assert cache.put(_key(cache), PROMPT, RESPONSE) is True
        assert store.count() == 1, "同 (partition_key, prompt_hash) 重复写入必须幂等"

    def test_b3b_put_roundtrip_preserves_usage_snapshot(self, cache: SemanticCache):
        """原始 usage 快照要能回放（成本节省核算的依据）。"""
        original_usage = {"prompt_tokens": 1200, "completion_tokens": 800, "total_tokens": 2000,
                          "provider": "deepseek"}
        cache.put(_key(cache), PROMPT, RESPONSE, usage=original_usage)

        hit = cache.get(_key(cache), PROMPT)

        assert hit is not None
        assert hit.original_usage["total_tokens"] == 2000
        assert hit.original_usage["provider"] == "deepseek"

    # ---- B4 禁用开关 --------------------------------------------------- #

    def test_b4_disabled_cache_neither_reads_nor_writes(self, store, tmp_path):
        """``enabled=False``：get 恒 None、put 恒 False，且一行都不写。"""
        disabled = SemanticCache(
            enabled=False, mode="exact", store=store, log_path=str(tmp_path / "off.jsonl")
        )
        key = _key(disabled)

        assert disabled.put(key, PROMPT, RESPONSE) is False
        assert store.count() == 0, "禁用时绝不允许写入任何行"
        assert disabled.get(key, PROMPT) is None

    def test_b4b_disabled_cache_never_touches_store(self, tmp_path):
        """禁用时连 store 都不应被调用（不建连接、不建表）。"""
        spy_store = MagicMock()
        disabled = SemanticCache(
            enabled=False, mode="exact", store=spy_store, log_path=str(tmp_path / "off.jsonl")
        )
        key = CacheKey(
            code="600519", trade_date=TODAY, time_slot="1800", report_type="stock_analysis",
            llm_model="m", backend_id="litellm", params_fingerprint="fp", prompt_hash="h" * 64,
        )
        key.validate()

        assert disabled.get(key, PROMPT) is None
        assert disabled.put(key, PROMPT, RESPONSE) is False
        spy_store.lookup_exact.assert_not_called()
        spy_store.upsert.assert_not_called()

    def test_b4c_disabled_after_write_stops_serving(self, store, tmp_path):
        """已有缓存数据时把开关关掉，也必须立刻停止命中。"""
        on = SemanticCache(enabled=True, mode="exact", store=store,
                           log_path=str(tmp_path / "on.jsonl"))
        on.put(_key(on), PROMPT, RESPONSE)
        assert on.get(_key(on), PROMPT) is not None

        off = SemanticCache(enabled=False, mode="exact", store=store,
                            log_path=str(tmp_path / "off.jsonl"))
        assert off.get(_key(off), PROMPT) is None

    # ---- B5 / B6 降级 --------------------------------------------------- #

    def test_b5_store_failure_degrades_silently(self, temp_db, tmp_path):
        """存储层异常 ⇒ get 未命中 / put 未写入，**不抛异常**。"""
        broken = _ExplodingStore(SEMCACHE_MODEL_ID, db_manager=temp_db)
        degraded = SemanticCache(
            enabled=True, mode="exact", store=broken, log_path=str(tmp_path / "deg.jsonl")
        )
        key = _key(degraded)

        assert degraded.get(key, PROMPT) is None
        assert degraded.put(key, PROMPT, RESPONSE) is False
        assert isinstance(degraded.stats(), dict)  # stats 也不得抛

    def test_b6_embedding_failure_still_allows_exact_hit(self, store, tmp_path):
        """向量化异常不得影响 Tier-0（精确命中根本不依赖向量）。"""
        broken_provider = MagicMock()
        broken_provider.embed.side_effect = RuntimeError("injected: embed exploded")
        degraded = SemanticCache(
            enabled=True, mode="exact", store=store,
            log_path=str(tmp_path / "embed.jsonl"), embedding_provider=broken_provider,
        )

        assert degraded.put(_key(degraded), PROMPT, RESPONSE) is True
        hit = degraded.get(_key(degraded), PROMPT)

        assert hit is not None
        assert hit.response_text == RESPONSE

    # ---- B7 guards ------------------------------------------------------ #

    def test_b7_short_response_is_rejected(self, cache: SemanticCache, store):
        """低于 min_response_chars 的响应不写缓存（挡住截断 / 错误文案）。"""
        assert cache.put(_key(cache), PROMPT, "太短了") is False
        assert store.count() == 0

    def test_b7b_sensitive_prompt_is_never_cached(self, cache: SemanticCache, store):
        """敏感内容 deny-list 命中 ⇒ 既不查也不写。"""
        sensitive = PROMPT + " 我的账户余额是 1234567 元，请据此给建议。"
        key = _key(cache, prompt=sensitive)

        assert cache.put(key, sensitive, RESPONSE) is False
        assert store.count() == 0
        assert cache.get(key, sensitive) is None

    # ---- B8 / B9 usage 契约 --------------------------------------------- #

    def test_b8_hit_usage_payload_zeroes_tokens_and_flags_cache_hit(self, cache: SemanticCache):
        """用户决策 ②：命中时 token 计数全为 0，并打 ``cache_hit=True`` 标记。"""
        cache.put(_key(cache), PROMPT, RESPONSE,
                  usage={"total_tokens": 2000, "provider": "deepseek"})

        hit = cache.get(_key(cache), PROMPT)
        assert hit is not None
        payload = hit.as_usage_payload()

        assert payload["prompt_tokens"] == 0
        assert payload["completion_tokens"] == 0
        assert payload["total_tokens"] == 0
        assert payload["cost"] == 0.0
        assert payload["cache_hit"] is True
        assert payload["cache_tier"] == "exact"
        assert payload["cached_original_usage"]["total_tokens"] == 2000

    def test_b9_cache_hit_usage_is_not_dropped_by_telemetry_gate(self, cache: SemanticCache):
        """核实盲区：``total_tokens=0`` 不会让命中遥测被 ``should_persist_*`` 丢弃。"""
        from src.llm.usage import is_semantic_cache_hit, should_persist_usage_telemetry

        cache.put(_key(cache), PROMPT, RESPONSE, usage={"total_tokens": 2000})
        hit = cache.get(_key(cache), PROMPT)
        assert hit is not None
        payload = hit.as_usage_payload()

        assert is_semantic_cache_hit(payload) is True
        assert should_persist_usage_telemetry(payload) is True, (
            "命中遥测被丢弃会让命中率完全不可观测（用户决策 ②）"
        )
        # 反向对照：普通的全零 usage（供应商漏报）仍应被丢弃，本改动是纯增量
        assert should_persist_usage_telemetry(
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        ) is False

    # ---- 附加：mode 字段与 Tier-1 预留 ----------------------------------- #

    def test_mode_semantic_is_downgraded_to_exact_in_this_release(self, store, tmp_path):
        """首版 Tier-1 未开放：配了 semantic 也必须按 exact 运行（不得误命中）。"""
        from src.cache.semantic_cache import SEMANTIC_TIER_ENABLED

        assert SEMANTIC_TIER_ENABLED is False, "首版 Tier-1 语义层必须默认关闭"

        semantic = SemanticCache(
            enabled=True, mode="semantic", store=store, log_path=str(tmp_path / "sem.jsonl")
        )
        assert semantic.mode == "semantic", "原始配置值应保留，便于运维排查"
        assert semantic.effective_mode == "exact", "生效模式必须被总闸压回 exact"

        semantic.put(_key(semantic), PROMPT, RESPONSE)
        # 注意：normalize_prompt 会 .strip()/折叠空白，所以 PROMPT+" " 与原串哈希相同，
        # 必须用**内容确实不同**的 prompt 才能验证"精确层不会因近似而命中"。
        near_prompt = PROMPT + "请额外分析当日北向资金净流入方向与强度。"
        assert semantic.get(_key(semantic, prompt=near_prompt), near_prompt) is None


# =========================================================================== #
# B. 主链路端到端（_call_litellm 注入）
# =========================================================================== #

def _make_analyzer():
    """构造一个最小可用的 GeminiAnalyzer（不触发任何真实网络/配置解析）。"""
    from src.analyzer import GeminiAnalyzer

    cfg = MagicMock()
    cfg.litellm_model = "deepseek/deepseek-chat"
    cfg.litellm_fallback_models = []
    cfg.generation_backend = "litellm"
    cfg.generation_fallback_backend = None

    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    analyzer._litellm_available = True
    analyzer._config_override = cfg
    return analyzer


def _backend_returning(*texts: str) -> MagicMock:
    """返回一个按调用次序吐出不同文本的 mock backend。"""
    from src.llm.generation_backend import GenerationBackend

    backend = MagicMock(spec=GenerationBackend)
    backend.generate.side_effect = [
        SimpleNamespace(
            text=text,
            model="deepseek/deepseek-chat",
            provider="deepseek",
            backend="litellm",
            usage={"provider": "deepseek", "total_tokens": 2000},
        )
        for text in texts
    ]
    return backend


def _context(*, code: str = "600519", trade_date: str = TODAY, time_slot: str = "1800") -> Dict[str, Any]:
    return {
        "code": code,
        "trade_date": trade_date,
        "time_slot": time_slot,
        "report_type": "stock_analysis",
        "llm_model": "deepseek/deepseek-chat",
        "backend_id": "litellm",
    }


class _AnalyzerHarness:
    """把「analyzer + mock backend + 注入的缓存」组装起来的上下文管理器。"""

    def __init__(self, cache_obj: Any, backend: MagicMock):
        self.analyzer = _make_analyzer()
        self.backend = backend
        self._patches = [
            patch.object(self.analyzer, "get_generation_backend_config_error", return_value=None),
            patch.object(self.analyzer, "_resolve_generation_backend_config",
                         return_value=("litellm", None)),
            patch.object(self.analyzer, "_get_generation_backend", return_value=backend),
            patch("src.cache.build_semantic_cache", return_value=cache_obj),
        ]

    def __enter__(self) -> "_AnalyzerHarness":
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc_info) -> None:
        for item in reversed(self._patches):
            item.stop()

    def call(self, *, cache_context: Optional[Dict[str, Any]] = None, prompt: str = PROMPT, **kwargs):
        return self.analyzer._call_litellm(
            prompt,
            dict(GEN_CONFIG),
            system_prompt=None,
            cache_context=cache_context,
            **kwargs,
        )


class TestAnalyzerCacheIntegration:
    """主链路端到端（E1 ~ E8）—— 全程零网络。"""

    # ---- E1 命中 -------------------------------------------------------- #

    def test_e1_second_identical_call_hits_cache_and_skips_llm(self, cache: SemanticCache):
        """同分区同 prompt 的第二次调用必须命中，且**不再调用 backend**。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        with _AnalyzerHarness(cache, backend) as harness:
            first_text, first_model, first_usage = harness.call(cache_context=_context())
            second_text, _second_model, second_usage = harness.call(cache_context=_context())

        assert first_text == RESPONSE
        assert first_usage.get("cache_hit") is not True, "首次调用不应被标记为命中"

        assert backend.generate.call_count == 1, "第二次调用仍然打了 LLM —— 缓存没生效"
        assert second_text == RESPONSE, "命中应原样返回首次响应"
        assert second_usage["total_tokens"] == 0
        assert second_usage["prompt_tokens"] == 0
        assert second_usage["completion_tokens"] == 0
        assert second_usage["cache_hit"] is True
        assert second_usage["cache_tier"] == "exact"
        assert first_model == "deepseek/deepseek-chat"

    def test_e1b_hit_usage_survives_telemetry_gate_in_main_path(self, cache: SemanticCache):
        """端到端确认命中 usage 会被遥测保留（而非因全零被丢）。"""
        from src.llm.usage import should_persist_usage_telemetry

        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=_context())
            _text, _model, usage = harness.call(cache_context=_context())

        assert should_persist_usage_telemetry(usage) is True

    # ---- E2 / E3 跨分区不命中 ------------------------------------------- #

    def test_e2_cross_time_slot_does_not_hit(self, cache: SemanticCache):
        """铁律 #1：09:30 写入的结论绝不能在 18:00 被复用。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=_context(time_slot="0930"))
            text, _model, usage = harness.call(cache_context=_context(time_slot="1800"))

        assert backend.generate.call_count == 2, "跨时槽命中 = 铁律 #1 被击穿"
        assert text == SECOND_RESPONSE
        assert usage.get("cache_hit") is not True

    def test_e3_cross_trade_date_does_not_hit(self, cache: SemanticCache):
        """铁律 #1：昨天的结论绝不能在今天被复用。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=_context(trade_date=YESTERDAY))
            text, _model, _usage = harness.call(cache_context=_context(trade_date=TODAY))

        assert backend.generate.call_count == 2, "跨交易日命中 = 铁律 #1 被击穿"
        assert text == SECOND_RESPONSE

    def test_e3b_cross_code_does_not_hit(self, cache: SemanticCache):
        """铁律 #3：跨标的绝不命中（prompt 模板占比高，误命中即随机返回）。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=_context(code="600519"))
            text, _model, _usage = harness.call(cache_context=_context(code="000001"))

        assert backend.generate.call_count == 2
        assert text == SECOND_RESPONSE

    # ---- E4 禁用 -------------------------------------------------------- #

    def test_e4_disabled_switch_never_hits_and_never_writes(self, store, tmp_path):
        """总开关关闭时，主链路逐次真实调用，且缓存表保持为空。"""
        disabled = SemanticCache(
            enabled=False, mode="exact", store=store, log_path=str(tmp_path / "off.jsonl")
        )
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        with _AnalyzerHarness(disabled, backend) as harness:
            harness.call(cache_context=_context())
            text, _model, usage = harness.call(cache_context=_context())

        assert backend.generate.call_count == 2
        assert text == SECOND_RESPONSE
        assert usage.get("cache_hit") is not True
        assert store.count() == 0, "禁用状态下写入了缓存行 —— 开关没关干净"

    # ---- E5 降级 -------------------------------------------------------- #

    def test_e5_store_failure_does_not_break_main_path(self, temp_db, tmp_path):
        """存储异常注入：主链路必须正常返回，只是退化为每次都调 LLM。"""
        broken = _ExplodingStore(SEMCACHE_MODEL_ID, db_manager=temp_db)
        degraded = SemanticCache(
            enabled=True, mode="exact", store=broken, log_path=str(tmp_path / "deg.jsonl")
        )
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)

        with _AnalyzerHarness(degraded, backend) as harness:
            first, _m1, _u1 = harness.call(cache_context=_context())
            second, _m2, _u2 = harness.call(cache_context=_context())

        assert first == RESPONSE
        assert second == SECOND_RESPONSE
        assert backend.generate.call_count == 2

    def test_e5b_cache_construction_failure_does_not_break_main_path(self):
        """连缓存门面都构造失败时，主链路依然照常完成。"""
        backend = _backend_returning(RESPONSE)
        analyzer = _make_analyzer()
        with patch.object(analyzer, "get_generation_backend_config_error", return_value=None), \
             patch.object(analyzer, "_resolve_generation_backend_config", return_value=("litellm", None)), \
             patch.object(analyzer, "_get_generation_backend", return_value=backend), \
             patch("src.cache.build_semantic_cache", side_effect=RuntimeError("injected: boom")):
            text, _model, _usage = analyzer._call_litellm(
                PROMPT, dict(GEN_CONFIG), cache_context=_context()
            )

        assert text == RESPONSE
        assert backend.generate.call_count == 1

    # ---- E6 未声明分区 -------------------------------------------------- #

    def test_e6_no_cache_context_bypasses_cache_entirely(self, cache: SemanticCache):
        """``generate_text`` 那条不传 cache_context 的路径：完全不碰缓存。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        analyzer = _make_analyzer()
        with patch.object(analyzer, "get_generation_backend_config_error", return_value=None), \
             patch.object(analyzer, "_resolve_generation_backend_config", return_value=("litellm", None)), \
             patch.object(analyzer, "_get_generation_backend", return_value=backend), \
             patch("src.cache.build_semantic_cache", return_value=cache) as builder:
            analyzer._call_litellm(PROMPT, dict(GEN_CONFIG))
            text, _model, _usage = analyzer._call_litellm(PROMPT, dict(GEN_CONFIG))

        builder.assert_not_called()
        assert backend.generate.call_count == 2
        assert text == SECOND_RESPONSE
        assert cache.store.count() == 0

    def test_e6b_incomplete_cache_context_is_fail_safe(self, cache: SemanticCache):
        """分区维度残缺（缺 time_slot）⇒ 既不查也不写，宁可不缓存。"""
        broken_context = _context()
        broken_context["time_slot"] = ""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)

        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=broken_context)
            text, _model, _usage = harness.call(cache_context=broken_context)

        assert backend.generate.call_count == 2
        assert text == SECOND_RESPONSE
        assert cache.store.count() == 0, "分区残缺时写入 = 未来可能跨时槽误命中"

    # ---- E7 流式回调 ---------------------------------------------------- #

    def test_e7_stream_progress_callback_fires_once_on_hit(self, cache: SemanticCache):
        """命中时没有真实流，需补一次进度回调，否则进度条会卡住。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)
        received: list[int] = []

        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=_context(), stream=True,
                         stream_progress_callback=received.append)
            received.clear()  # 只观察第二次（命中）那一趟
            text, _model, usage = harness.call(cache_context=_context(), stream=True,
                                               stream_progress_callback=received.append)

        assert backend.generate.call_count == 1
        assert usage["cache_hit"] is True
        assert received == [len(RESPONSE)], "命中时进度回调应恰好补一次总字符数"
        assert text == RESPONSE

    # ---- E8 脏缓存回落 -------------------------------------------------- #

    def test_e8_invalid_cached_text_falls_back_to_real_llm(self, cache: SemanticCache):
        """缓存文本没通过 response_validator ⇒ 丢弃命中，回落真实调用。"""
        backend = _backend_returning(RESPONSE, SECOND_RESPONSE)

        def _reject_cached(text: str) -> None:
            if text == RESPONSE:
                raise ValueError("injected: cached payload is not valid JSON")

        with _AnalyzerHarness(cache, backend) as harness:
            harness.call(cache_context=_context())
            text, _model, usage = harness.call(
                cache_context=_context(), response_validator=_reject_cached
            )

        assert backend.generate.call_count == 2, "脏缓存必须回落到真实 LLM"
        assert text == SECOND_RESPONSE
        assert usage.get("cache_hit") is not True


# =========================================================================== #
# C. 全局一致性（接口签名 ↔ 调用点 ↔ 配置 ↔ 建表）
# =========================================================================== #

class TestGlobalConsistency:
    """静态一致性核查：签名 / 配置项 / DDL / 隔离约束。"""

    def test_config_exposes_all_sem_cache_fields_with_safe_defaults(self):
        """配置项齐备且默认保守（总开关默认关、阈值不沿用 ltm 的 0.25）。"""
        from src.config import Config

        defaults = {
            "sem_cache_enabled": False,
            "sem_cache_mode": "exact",
            "sem_cache_min_similarity": 0.95,
            "sem_cache_ttl_hours": 12,
            "sem_cache_max_candidates": 64,
            "sem_cache_min_response_chars": 200,
            "sem_cache_store_prompt_text": True,
        }
        annotations = getattr(Config, "__annotations__", {})
        for name, expected in defaults.items():
            assert name in annotations, f"config.py 缺少字段 {name}"
            assert getattr(Config, name) == expected, f"{name} 默认值与设计 §7.3 不符"

        assert getattr(Config, "sem_cache_min_similarity") != 0.25, (
            "绝不可沿用 ltm 的 0.25 阈值（设计 §7.2）"
        )

    def test_analyzer_call_litellm_accepts_cache_context_kwarg(self):
        """调用点契约：``_call_litellm`` 必须有且仅新增 ``cache_context`` 这一个参数。"""
        import inspect

        from src.analyzer import GeminiAnalyzer

        signature = inspect.signature(GeminiAnalyzer._call_litellm)
        assert "cache_context" in signature.parameters
        assert signature.parameters["cache_context"].default is None
        assert signature.parameters["cache_context"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_facade_public_methods_match_design_contract(self):
        """门面签名与设计 §3.2 一致（analyzer 只依赖这三个方法）。"""
        import inspect

        for name in ("build_key", "get", "put", "stats"):
            assert callable(getattr(SemanticCache, name)), f"门面缺少 {name}"

        get_sig = inspect.signature(SemanticCache.get)
        assert list(get_sig.parameters)[:3] == ["self", "key", "prompt"]
        put_sig = inspect.signature(SemanticCache.put)
        assert list(put_sig.parameters)[:4] == ["self", "key", "prompt", "response_text"]

    def test_orm_columns_cover_all_partition_dimensions(self, temp_db):
        """建表 DDL 必须逐列冗余落盘七个分区维度（第 2/3 层防御的比对基础）。"""
        from src.storage import LlmSemanticCache

        columns = set(LlmSemanticCache.__table__.columns.keys())
        required = {
            "partition_key", "code", "trade_date", "time_slot", "report_type",
            "llm_model", "backend_id", "params_fp", "prompt_hash", "response_text",
            "model_id", "vector_version", "expires_at", "hit_count",
        }
        assert required.issubset(columns), f"DDL 缺少列: {sorted(required - columns)}"
        assert LlmSemanticCache.__tablename__ == "llm_semantic_cache"

    def test_semcache_namespace_is_isolated_from_ltm(self):
        """铁律 #4：命名空间与 U14 严格区分。"""
        from src.memory.models import LOCAL_MODEL_ID

        assert SEMCACHE_MODEL_ID == "local:semcache-v1"
        assert SEMCACHE_MODEL_ID != LOCAL_MODEL_ID

    def test_cache_package_does_not_import_ltm_vector_store_class(self):
        """静态依赖白名单：只准复用 encode/decode 纯函数，禁止 import SqliteVectorStore。

        仅扫描**真实的 import 语句**，忽略注释/文档字符串里对 U14 的"不 import …"措辞。
        """
        import re

        cache_dir = REPO_ROOT / "src" / "cache"
        import_line = re.compile(r"^\s*(?:from|import)\s+\S+", re.MULTILINE)
        for path in cache_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for line in import_line.findall(source):
                # 真实 import 语句里不得出现 U14 的符号
                assert "SqliteVectorStore" not in line, f"{path.name} 反向依赖了 U14 存储层: {line!r}"
                assert "analysis_memory_vector" not in line, f"{path.name} 触碰了 U14 的表: {line!r}"

    def test_ltm_package_never_depends_on_cache_package(self):
        """反向依赖检查：U14 绝不可 import U12。"""
        memory_dir = REPO_ROOT / "src" / "memory"
        for path in memory_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "src.cache" not in source, f"{path.name} 反向依赖了 U12 缓存包"

    def test_writing_cache_leaves_ltm_table_untouched(self, cache: SemanticCache, temp_db):
        """物理隔离实证：写缓存后 ``analysis_memory_vector`` 仍为 0 行。"""
        from src.storage import AnalysisMemoryVector

        for index in range(5):
            prompt = f"{PROMPT}#{index}"
            cache.put(_key(cache, prompt=prompt), prompt, RESPONSE)

        assert cache.store.count() == 5
        with temp_db.session_scope() as session:
            assert session.query(AnalysisMemoryVector).count() == 0
