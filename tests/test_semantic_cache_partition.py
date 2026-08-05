# -*- coding: utf-8 -*-
"""
U12 语义缓存 — 分区铁律回归（P1 ~ P11）

本文件是 U12 的**安全底线测试**，对应铁律 #1 / #3 / #4：

    铁律 #1  严禁跨时槽、跨交易日命中
    铁律 #3  跨标的、跨报告类型、跨模型、跨生成参数不得命中
    铁律 #4  与 U14 长期记忆物理隔离，互不污染

为什么这些用例的优先级高于"命中率"用例：缓存不命中只是多花一次 token；
缓存**误**命中是把早盘 09:30 的结论当作盘后 18:00 的结论、把昨天的结论当作
今天的结论输出给用户 —— 那是直接给出错误的投资建议。因此本文件里任何一条
用例挂掉，都必须视为**阻断发布**级别的问题，不允许通过调阈值绕过。

用例清单：

    P1   跨时槽不命中（0930 vs 1800，其余七维全同）
    P2   跨交易日不命中（昨天 vs 今天）
    P3   跨标的不命中
    P4   跨报告类型不命中
    P5   跨 LLM 模型不命中
    P6   跨生成参数不命中（temperature 变化）
    P7   跨 backend_id 不命中
    P8   partition_key 的确定性与敏感性（七维任一变化即变，同输入恒稳定）
    P9   第 2/3 层防御：伪造行 / 字段错配一律拒绝
    P10  与 U14 零交叉污染（两张表互不可见）
    P11  分区维度残缺时既不查也不写（fail-safe）
    P12  TTL 过期行不得命中（附加：时效性兜底）
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 与既有测试保持一致：缺少 litellm 运行时依赖时打桩，保证可独立运行
try:  # pragma: no cover - 环境相关
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["litellm"] = MagicMock()

from src.cache.cache_store import SqliteSemanticCacheStore  # noqa: E402
from src.cache.models import (  # noqa: E402
    SEMCACHE_MODEL_ID,
    SEMCACHE_VECTOR_VERSION,
    CacheKey,
    compute_params_fingerprint,
)
from src.cache.semantic_cache import SemanticCache  # noqa: E402

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()

PROMPT = "请基于以下技术面数据分析该标的的短期走势，并输出 JSON 结论。"
#: 响应必须超过默认 min_response_chars=200，否则会被判据拒收
RESPONSE = "结论：短期震荡整理，量能温和。" * 30


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch):
    """隔离的临时 SQLite 库（绝不触碰工程默认库）。"""
    from src.config import Config
    from src.storage import DatabaseManager

    workdir = tempfile.mkdtemp(prefix="semcache_test_")
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
    llm_model: str = "gemini/gemini-2.0-flash",
    backend_id: str = "litellm",
    generation_config: Optional[Dict[str, Any]] = None,
    prompt: str = PROMPT,
    system_prompt: Optional[str] = None,
) -> CacheKey:
    """按七个分区维度构造 key；任一参数用于制造"只差一维"的对照组。"""
    key = cache_obj.build_key(
        prompt=prompt,
        system_prompt=system_prompt,
        generation_config=generation_config if generation_config is not None else {"temperature": 0.3},
        code=code,
        trade_date=trade_date,
        time_slot=time_slot,
        report_type=report_type,
        llm_model=llm_model,
        backend_id=backend_id,
    )
    assert key is not None, "构造 key 失败（七维应当完整）"
    return key


def _seed(cache_obj: SemanticCache, key: CacheKey, response: str = RESPONSE) -> None:
    """写入一条基准缓存并确认落盘。"""
    assert cache_obj.put(key, PROMPT, response, response_model="m-1") is True


def _assert_isolated(cache_obj: SemanticCache, other: CacheKey) -> None:
    """对照组必须**未命中**，并且基准组自身仍然命中（排除"整体写坏了"的假阴性）。"""
    assert cache_obj.get(other, PROMPT) is None


# --------------------------------------------------------------------------- #
# P1 ~ P7：七个分区维度逐一验证「只差一维即不命中」
# --------------------------------------------------------------------------- #

def test_p1_cross_time_slot_never_hits(cache: SemanticCache) -> None:
    """P1（最高优先级）：同日同股同 prompt，仅时槽不同 ⇒ 严禁命中。"""
    morning = _key(cache, time_slot="0930")
    evening = _key(cache, time_slot="1800")

    _seed(cache, morning)

    # 早盘写入的结论，盘后必须查不到
    _assert_isolated(cache, evening)
    # 早盘自己仍然命中（证明不是写入失败导致的假阴性）
    hit = cache.get(morning, PROMPT)
    assert hit is not None
    assert hit.response_text == RESPONSE

    # 反向同理：盘后写入的结论，早盘也查不到
    _seed(cache, evening, response="盘后结论。" * 60)
    morning_hit = cache.get(morning, PROMPT)
    assert morning_hit is not None
    assert morning_hit.response_text == RESPONSE, "早盘命中被盘后写入污染"


def test_p2_cross_trade_date_never_hits(cache: SemanticCache) -> None:
    """P2：同股同时槽同 prompt，仅交易日不同 ⇒ 严禁命中。"""
    yesterday = _key(cache, trade_date=YESTERDAY)
    today = _key(cache, trade_date=TODAY)

    _seed(cache, yesterday)

    _assert_isolated(cache, today)
    assert cache.get(yesterday, PROMPT) is not None


def test_p3_cross_code_never_hits(cache: SemanticCache) -> None:
    """P3：仅标的不同 ⇒ 严禁命中。"""
    moutai = _key(cache, code="600519")
    ping_an = _key(cache, code="601318")

    _seed(cache, moutai)

    _assert_isolated(cache, ping_an)
    assert cache.get(moutai, PROMPT) is not None


def test_p4_cross_report_type_never_hits(cache: SemanticCache) -> None:
    """P4：仅报告类型不同 ⇒ 严禁命中（个股分析 vs 大盘复盘不可混用）。"""
    stock = _key(cache, report_type="stock_analysis")
    market = _key(cache, report_type="market")

    _seed(cache, stock)

    _assert_isolated(cache, market)
    assert cache.get(stock, PROMPT) is not None


def test_p5_cross_llm_model_never_hits(cache: SemanticCache) -> None:
    """P5：仅模型不同 ⇒ 严禁命中（换模型就是换了一个"分析师"）。"""
    flash = _key(cache, llm_model="gemini/gemini-2.0-flash")
    sonnet = _key(cache, llm_model="anthropic/claude-sonnet-4")

    _seed(cache, flash)

    _assert_isolated(cache, sonnet)
    assert cache.get(flash, PROMPT) is not None


def test_p6_cross_generation_params_never_hits(cache: SemanticCache) -> None:
    """P6：仅生成参数不同 ⇒ 严禁命中（temperature 会显著改变输出）。"""
    low = _key(cache, generation_config={"temperature": 0.1})
    high = _key(cache, generation_config={"temperature": 0.9})
    assert low.params_fingerprint != high.params_fingerprint

    _seed(cache, low)

    _assert_isolated(cache, high)
    assert cache.get(low, PROMPT) is not None


def test_p7_cross_backend_never_hits(cache: SemanticCache) -> None:
    """P7：仅生成后端不同 ⇒ 严禁命中（本地 CLI 与 litellm 的行为差异极大）。"""
    remote = _key(cache, backend_id="litellm")
    local_cli = _key(cache, backend_id="claude_cli")

    _seed(cache, remote)

    _assert_isolated(cache, local_cli)
    assert cache.get(remote, PROMPT) is not None


def test_p6b_irrelevant_generation_params_do_not_fragment(cache: SemanticCache) -> None:
    """P6 补充：无关参数（stream / 回调）不得让分区碎片化。

    只有真正影响输出的键参与指纹，否则每次调用都换一个分区 ⇒ 命中率恒为 0。
    """
    base = compute_params_fingerprint({"temperature": 0.3})
    noisy = compute_params_fingerprint(
        {"temperature": 0.3, "stream": True, "callback": object()}
    )
    assert base == noisy


# --------------------------------------------------------------------------- #
# P8：partition_key 的确定性与敏感性
# --------------------------------------------------------------------------- #

def test_p8_partition_key_is_deterministic_and_sensitive(cache: SemanticCache) -> None:
    """P8：同输入恒定、任一维度变化即变。

    确定性尤为关键 —— 用内置 ``hash()`` 会因 ``PYTHONHASHSEED`` 随机化导致
    同一请求在不同进程算出不同分区，缓存等于报废。这里正面锁死 sha256 口径。
    """
    baseline = _key(cache)
    assert baseline.partition_key() == _key(cache).partition_key()
    assert len(baseline.partition_key()) == 64  # sha256 hexdigest

    variants = {
        "code": _key(cache, code="000001"),
        "trade_date": _key(cache, trade_date=YESTERDAY),
        "time_slot": _key(cache, time_slot="0930"),
        "report_type": _key(cache, report_type="market"),
        "llm_model": _key(cache, llm_model="openai/gpt-4o"),
        "backend_id": _key(cache, backend_id="codex_cli"),
        "params": _key(cache, generation_config={"temperature": 0.7}),
    }
    seen = {baseline.partition_key()}
    for name, variant in variants.items():
        assert variant.partition_key() != baseline.partition_key(), f"{name} 未进入分区键"
        seen.add(variant.partition_key())
    assert len(seen) == len(variants) + 1, "不同维度组合出现分区键碰撞"


def test_p8b_prompt_hash_not_in_partition_key(cache: SemanticCache) -> None:
    """P8 补充：prompt 内容属于分区**内部**指纹，不参与 partition_key。

    否则同一分区永远只有 1 行，Tier-1 语义检索将来没有候选可用。
    """
    a = _key(cache, prompt="prompt-A")
    b = _key(cache, prompt="prompt-B")
    assert a.partition_key() == b.partition_key()
    assert a.prompt_hash != b.prompt_hash


# --------------------------------------------------------------------------- #
# P9：第 2 / 3 层防御
# --------------------------------------------------------------------------- #

def test_p9_layer2_sql_filter_rejects_forged_row(
    cache: SemanticCache, store: SqliteSemanticCacheStore, temp_db
) -> None:
    """P9a：即便 partition_key 相同，字段不符的行也会被 SQL 等值过滤拦下。

    模拟"分区键构造代码写错了"这一现实风险：直接把库里那行的 time_slot 改掉，
    partition_key 保持不变（伪造哈希碰撞）。第 2 层必须拦住。
    """
    from src.storage import LlmSemanticCache

    key = _key(cache, time_slot="1800")
    _seed(cache, key)
    assert cache.get(key, PROMPT) is not None

    with temp_db.session_scope() as session:
        rows = session.query(LlmSemanticCache).all()
        assert len(rows) == 1
        rows[0].time_slot = "0930"  # 伪造：分区键仍是 1800 那一份

    assert cache.get(key, PROMPT) is None, "第 2 层 SQL 等值过滤未拦住错配行"


def test_p9_layer3_assert_rejects_mismatched_row(
    store: SqliteSemanticCacheStore, cache: SemanticCache, caplog: pytest.LogCaptureFixture
) -> None:
    """P9b：第 3 层 Python 断言必须逐字段复核并以 ERROR 留痕。"""
    key = _key(cache, time_slot="1800", trade_date=TODAY)
    good_row = {
        "code": key.code,
        "trade_date": key.trade_date,
        "time_slot": key.time_slot,
        "report_type": key.report_type,
        "llm_model": key.llm_model,
    }
    assert store._assert_partition(good_row, key) is True

    for field, bad_value in (
        ("time_slot", "0930"),
        ("trade_date", YESTERDAY),
        ("code", "000001"),
        ("report_type", "market"),
        ("llm_model", "openai/gpt-4o"),
    ):
        bad_row = dict(good_row)
        bad_row[field] = bad_value
        with caplog.at_level(logging.ERROR, logger="src.cache.cache_store"):
            caplog.clear()
            assert store._assert_partition(bad_row, key) is False, f"{field} 错配未被拦下"
            assert any("分区越界拦截" in rec.message for rec in caplog.records), (
                f"{field} 错配未产生 ERROR 留痕"
            )


def test_p9_get_same_partition_override_recomputes_key(
    cache: SemanticCache, store: SqliteSemanticCacheStore
) -> None:
    """P9c：``get_same_partition`` 覆写时槽/日期后必须重算分区键，不得串区。"""
    key = _key(cache, time_slot="1800", trade_date=TODAY)
    _seed(cache, key)

    assert store.get_same_partition(key) is not None
    assert store.get_same_partition(key, time_slot="0930") is None
    assert store.get_same_partition(key, trade_date=YESTERDAY) is None
    assert store.get_same_partition(key, prompt_hash="deadbeef") is None


# --------------------------------------------------------------------------- #
# P10：与 U14 零交叉污染
# --------------------------------------------------------------------------- #

def test_p10_no_cross_pollution_with_u14(cache: SemanticCache, temp_db) -> None:
    """P10：U12 只写 ``llm_semantic_cache``，U14 只写 ``analysis_memory_vector``。"""
    from src.storage import AnalysisMemoryVector, LlmSemanticCache

    key = _key(cache)
    _seed(cache, key)

    with temp_db.session_scope() as session:
        assert session.query(LlmSemanticCache).count() == 1
        assert session.query(AnalysisMemoryVector).count() == 0, "U12 写入污染了 U14 向量表"
        row = session.query(LlmSemanticCache).first()
        # 命名空间也必须隔离：绝不能落成 U14 的 local:lexical-v1
        assert row.model_id == SEMCACHE_MODEL_ID
        assert row.model_id != "local:lexical-v1"


def test_p10b_u14_rows_invisible_to_u12(cache: SemanticCache, temp_db) -> None:
    """P10 反向：U14 的向量行不得被 U12 检索到。"""
    from src.memory.models import LOCAL_MODEL_ID
    from src.storage import AnalysisMemoryVector

    with temp_db.session_scope() as session:
        session.add(
            AnalysisMemoryVector(
                history_id=1,
                code="600519",
                name="贵州茅台",
                report_type="stock_analysis",
                trade_date=TODAY,
                time_slot="1800",
                conclusion_text="U14 的历史结论",
                text_hash="x" * 64,
                embedding=None,
                dim=0,
                model_id=LOCAL_MODEL_ID,
                vector_version=1,
            )
        )

    key = _key(cache)
    assert cache.get(key, PROMPT) is None, "U12 查询命中了 U14 的数据"
    assert cache.store.count() == 0


# --------------------------------------------------------------------------- #
# P11：维度残缺 fail-safe
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "overrides",
    [
        {"time_slot": ""},          # 缺时槽
        {"time_slot": "not-a-slot"},  # 时槽非法
        {"trade_date": ""},         # 缺交易日
        {"trade_date": "昨天"},      # 交易日非法
        {"code": ""},               # 缺标的
        {"llm_model": ""},          # 缺模型
        {"backend_id": ""},         # 缺后端
    ],
)
def test_p11_incomplete_key_is_rejected(cache: SemanticCache, overrides: Dict[str, Any]) -> None:
    """P11：任一分区维度缺失/非法 ⇒ ``build_key`` 返回 None ⇒ 既不查也不写。

    这里最危险的是"时槽缺失被静默补成默认 1800"：那会让一个没有时段语义的
    请求直接命中盘后缓存。因此 U12 用了 ``normalize_*_strict``，解析失败一律
    归零为空串，宁可不缓存。
    """
    params: Dict[str, Any] = {
        "code": "600519",
        "trade_date": TODAY,
        "time_slot": "1800",
        "report_type": "stock_analysis",
        "llm_model": "gemini/gemini-2.0-flash",
        "backend_id": "litellm",
    }
    params.update(overrides)
    key = cache.build_key(
        prompt=PROMPT,
        generation_config={"temperature": 0.3},
        **params,
    )
    assert key is None, f"残缺维度 {overrides} 竟构造出了可用的分区键"


def test_p11b_incomplete_key_blocks_store_layer(store: SqliteSemanticCacheStore) -> None:
    """P11 补充：即便有人绕过门面直接塞残缺 key 给存储层，也必须拒绝。"""
    broken = CacheKey(
        code="600519",
        trade_date=TODAY,
        time_slot="",  # 残缺
        report_type="stock_analysis",
        llm_model="gemini/gemini-2.0-flash",
        backend_id="litellm",
        params_fingerprint="abc",
        prompt_hash="d" * 64,
    )
    broken.validate()
    assert broken.is_complete() is False
    assert store.lookup_exact(broken) is None
    assert store.get_same_partition(broken) is None
    assert store.load_partition(broken).is_empty() is True


# --------------------------------------------------------------------------- #
# P12：TTL
# --------------------------------------------------------------------------- #

def test_p12_expired_entry_never_hits(cache: SemanticCache) -> None:
    """P12：过期行不得命中，并可被 ``purge_expired`` 清掉。"""
    key = _key(cache)
    written_at = datetime.now() - timedelta(hours=24)
    assert cache.put(key, PROMPT, RESPONSE, now=written_at) is True

    # ttl_hours=12，24 小时前写入的行现在必然过期
    assert cache.get(key, PROMPT) is None
    assert cache.purge_expired() == 1
    assert cache.store.count() == 0


def test_p12b_ttl_zero_means_never_expire(store: SqliteSemanticCacheStore, tmp_path) -> None:
    """P12 补充：``ttl_hours=0`` 表示永不过期（运维显式选择时才生效）。"""
    forever = SemanticCache(
        enabled=True,
        ttl_hours=0,
        min_response_chars=200,
        store=store,
        log_path=str(tmp_path / "semcache.jsonl"),
    )
    key = _key(forever)
    assert forever.put(key, PROMPT, RESPONSE, now=datetime.now() - timedelta(days=30)) is True
    assert forever.get(key, PROMPT) is not None
    assert forever.purge_expired() == 0
