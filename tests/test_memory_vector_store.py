# -*- coding: utf-8 -*-
"""
U14 长期记忆 — 向量存储 / 召回 / 写入 单测

覆盖（对应设计 §7 测试策略 T03 部分）：
1. BLOB 编解码往返（float32 / 长度校验 / 脏数据返回 None）
2. UPSERT 幂等：同内容重复写零变更；内容变更才覆盖
3. ``(model_id, vector_version)`` 强过滤：切换 model 后召回 0 条且 ``model_mismatch``
4. TopK 正确性：与朴素全排序结果逐条一致
5. 时间衰减、``min_similarity`` 阈值、``exclude_history_ids``、``scope``
6. 候选缓存命中与写入后自动失效
7. ``MemoryWriter``：三字段模板 / 身份 token 剥离 / 截断 / stage 零网络 / flush 落盘
8. fail-open：store 抛异常时 ``search`` 返回空结果而非上抛

所有用例使用 **临时 SQLite 文件库**，不触碰工程默认库。
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 与既有测试保持一致：缺少 litellm 运行时依赖时打桩，保证可独立运行
try:  # pragma: no cover - 环境相关
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["litellm"] = MagicMock()

from src.memory.embedding_provider import LocalLexicalEmbeddingProvider  # noqa: E402
from src.memory.models import (  # noqa: E402
    LOCAL_MODEL_ID,
    SCOPE_GLOBAL,
    SCOPE_SAME_STOCK,
    VECTOR_VERSION,
    DegradeReason,
    MemoryRecord,
    RecallQuery,
)
from src.memory.vector_store import (  # noqa: E402
    SqliteVectorStore,
    apply_time_decay,
    decode_vector,
    encode_vector,
)
from src.memory.writer import MemoryWriter  # noqa: E402

DIM = 32

#: 区分"未传向量"与"显式传 None（= 只落文本）"
_AUTO_VECTOR = object()


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch):
    """构造一个隔离的临时 SQLite 库并返回其 DatabaseManager。"""
    from src.config import Config
    from src.storage import DatabaseManager

    workdir = tempfile.mkdtemp(prefix="ltm_test_")
    db_path = os.path.join(workdir, "test_memory.db")

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
def store(temp_db) -> SqliteVectorStore:
    return SqliteVectorStore(
        LOCAL_MODEL_ID,
        db_manager=temp_db,
        vector_version=VECTOR_VERSION,
        max_candidates=1000,
        cache_ttl_seconds=0.0,
    )


def _unit(seed: int, dim: int = DIM) -> np.ndarray:
    """确定性的单位向量。"""
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dim).astype(np.float32)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def _record(
    history_id: int,
    *,
    code: str = "600519",
    name: str = "贵州茅台",
    trade_date: str = "",
    text: str = "",
    vector: Any = _AUTO_VECTOR,
    report_type: str = "daily",
    time_slot: str = "1800",
) -> MemoryRecord:
    """构造一条测试记录。

    ``trade_date`` 缺省取**今天**：默认回看窗口是 90 天，写死日期会随时间流逝
    自然过期，导致用例在未来某天集体失败。
    ``vector=None`` 表示"只落文本"，与"未传向量"（自动生成）严格区分。
    """
    embedding = _unit(history_id) if vector is _AUTO_VECTOR else vector
    return MemoryRecord(
        history_id=history_id,
        code=code,
        name=name,
        report_type=report_type,
        trade_date=trade_date or date.today().isoformat(),
        time_slot=time_slot,
        conclusion_text=text or f"[趋势] 情境-{history_id}",
        embedding=embedding,
        sentiment_score=60,
        operation_advice="观望",
    )


def _make_config(**kwargs) -> SimpleNamespace:
    base = dict(
        ltm_enabled=True,
        ltm_write_enabled=True,
        ltm_write_mode="batch_end",
        ltm_embedding_provider="local",
        ltm_embedding_model="openai/text-embedding-3-small",
        ltm_embedding_dim=256,
        ltm_top_k=3,
        ltm_min_similarity=0.75,
        ltm_scope="same_stock",
        ltm_lookback_days=90,
        ltm_halflife_days=60,
        ltm_conclusion_max_chars=500,
        ltm_embed_timeout_seconds=8.0,
        ltm_max_candidates=20000,
        openai_api_keys=[],
        openai_base_url="",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# 1. BLOB 编解码
# --------------------------------------------------------------------------- #

def test_blob_roundtrip_preserves_float32_bits():
    original = _unit(7)

    restored = decode_vector(encode_vector(original), DIM)

    assert restored is not None
    assert restored.dtype == np.float32
    assert np.array_equal(restored, original)


def test_decode_vector_rejects_dirty_payloads():
    assert decode_vector(None) is None
    assert decode_vector(b"") is None
    assert decode_vector(b"abc") is None                 # 长度非 4 的倍数
    assert decode_vector(encode_vector(_unit(1)), 999) is None  # 维度不符


def test_apply_time_decay_semantics():
    assert apply_time_decay(0.9, 0, 60) == pytest.approx(0.9)
    assert apply_time_decay(0.9, 60, 60) == pytest.approx(0.45)
    assert apply_time_decay(0.9, 120, 60) == pytest.approx(0.225)
    # halflife<=0 表示关闭衰减
    assert apply_time_decay(0.9, 999, 0) == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# 2. UPSERT 幂等
# --------------------------------------------------------------------------- #

def test_upsert_is_idempotent_for_identical_content(store: SqliteVectorStore, temp_db):
    from src.storage import AnalysisMemoryVector

    records = [_record(1), _record(2)]
    store.upsert_many(records)
    store.upsert_many([_record(1), _record(2)])  # 同内容重复写

    with temp_db.session_scope() as session:
        rows = session.query(AnalysisMemoryVector).order_by(AnalysisMemoryVector.history_id).all()
        assert len(rows) == 2
        assert [row.history_id for row in rows] == [1, 2]
        assert rows[0].model_id == LOCAL_MODEL_ID
        assert rows[0].vector_version == VECTOR_VERSION
        assert rows[0].dim == DIM


def test_upsert_overwrites_when_text_hash_changes(store: SqliteVectorStore, temp_db):
    from src.storage import AnalysisMemoryVector

    store.upsert_many([_record(1, text="[趋势] 旧结论", vector=_unit(11))])
    store.upsert_many([_record(1, text="[趋势] 新结论", vector=_unit(22))])

    with temp_db.session_scope() as session:
        rows = session.query(AnalysisMemoryVector).all()
        assert len(rows) == 1
        assert rows[0].conclusion_text == "[趋势] 新结论"
        assert np.allclose(decode_vector(rows[0].embedding, DIM), _unit(22))


def test_upsert_skips_invalid_records(store: SqliteVectorStore):
    stats = store.upsert_many([
        _record(1),
        MemoryRecord(history_id=0, conclusion_text="无效"),       # history_id 非法
        MemoryRecord(history_id=9, conclusion_text="   "),        # 文本为空
    ])

    assert stats["received"] == 3
    assert stats["written"] == 1
    assert stats["skipped"] == 2
    assert store.count() == 1


def test_upsert_dedupes_same_history_id_within_batch(store: SqliteVectorStore, temp_db):
    from src.storage import AnalysisMemoryVector

    store.upsert_many([
        _record(1, text="[趋势] 第一版"),
        _record(1, text="[趋势] 第二版"),
    ])

    # 必须在 session 内读属性：session_scope 退出即 commit+close，实例会 detach
    with temp_db.session_scope() as session:
        rows = session.query(AnalysisMemoryVector).all()
        assert len(rows) == 1
        assert rows[0].conclusion_text == "[趋势] 第二版"


def test_text_only_row_can_be_backfilled_later(store: SqliteVectorStore):
    """text_only 先落文本、后续 backfill 补向量，走的是同一个冲突键。"""
    store.upsert_many([_record(1, text="[趋势] 待补向量", vector=None)])
    assert store.count() == 1
    assert len(store.list_missing_embedding()) == 1

    # 文本不变时 WHERE 子句拦截 → 仍无向量（这正是幂等的代价，backfill 需改 hash 或直接 UPDATE）
    store.upsert_many([_record(1, text="[趋势] 待补向量-已回填", vector=_unit(5))])
    assert store.list_missing_embedding() == []


# --------------------------------------------------------------------------- #
# 3. 版本强过滤
# --------------------------------------------------------------------------- #

def test_model_mismatch_when_store_has_only_other_model(temp_db):
    """切换 embedding 模型但未 backfill → 召回 0 条且明确上报 model_mismatch。"""
    old_store = SqliteVectorStore(LOCAL_MODEL_ID, db_manager=temp_db, cache_ttl_seconds=0.0)
    old_store.upsert_many([_record(1), _record(2)])

    new_store = SqliteVectorStore(
        "litellm:openai/text-embedding-3-small", db_manager=temp_db, cache_ttl_seconds=0.0
    )
    query = RecallQuery(stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0)
    result = new_store.search(query)

    assert result.hit_count == 0
    assert result.degraded is True
    assert result.degrade_reason == DegradeReason.MODEL_MISMATCH.value
    assert result.model_id == "litellm:openai/text-embedding-3-small"


def test_vector_version_bump_also_triggers_model_mismatch(temp_db):
    SqliteVectorStore(
        LOCAL_MODEL_ID, db_manager=temp_db, vector_version=1, cache_ttl_seconds=0.0
    ).upsert_many([_record(1)])

    v2 = SqliteVectorStore(
        LOCAL_MODEL_ID, db_manager=temp_db, vector_version=2, cache_ttl_seconds=0.0
    )
    result = v2.search(RecallQuery(stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0))

    assert result.hit_count == 0
    assert result.degrade_reason == DegradeReason.MODEL_MISMATCH.value


def test_empty_store_reports_empty_store(store: SqliteVectorStore):
    result = store.search(RecallQuery(stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0))

    assert result.hit_count == 0
    assert result.degrade_reason == DegradeReason.EMPTY_STORE.value


def test_dim_mismatch_reports_model_mismatch(store: SqliteVectorStore):
    store.upsert_many([_record(1)])

    result = store.search(
        RecallQuery(stock_code="600519", query_embedding=_unit(1, dim=DIM * 2), min_similarity=-1.0)
    )

    assert result.hit_count == 0
    assert result.degrade_reason == DegradeReason.MODEL_MISMATCH.value


def test_missing_query_embedding_reports_embed_failed(store: SqliteVectorStore):
    result = store.search(RecallQuery(stock_code="600519", query_text="有文本但没向量"))

    assert result.hit_count == 0
    assert result.degrade_reason == DegradeReason.EMBED_FAILED.value


# --------------------------------------------------------------------------- #
# 4. TopK 正确性
# --------------------------------------------------------------------------- #

def test_topk_matches_naive_full_sort(store: SqliteVectorStore):
    """argpartition 快路径必须与朴素全排序逐条一致。"""
    vectors = {index: _unit(index) for index in range(1, 41)}
    store.upsert_many([
        _record(index, vector=vector) for index, vector in vectors.items()
    ])

    query_vector = _unit(1)
    result = store.search(RecallQuery(
        stock_code="600519",
        query_embedding=query_vector,
        top_k=5,
        min_similarity=-1.0,
        apply_decay=False,
    ))

    expected = sorted(
        ((float(vec @ query_vector), hid) for hid, vec in vectors.items()),
        key=lambda pair: (-pair[0], pair[1]),
    )[:5]

    assert result.candidate_count == 40
    assert result.hit_count == 5
    assert [item.history_id for item in result.items] == [hid for _, hid in expected]
    for item, (score, _) in zip(result.items, expected):
        assert item.similarity == pytest.approx(score, abs=1e-5)
        assert item.final_score == pytest.approx(score, abs=1e-5)
    # 自身向量必然排第一且相似度为 1
    assert result.items[0].history_id == 1
    assert result.items[0].similarity == pytest.approx(1.0, abs=1e-5)


def test_topk_when_k_exceeds_candidate_count(store: SqliteVectorStore):
    store.upsert_many([_record(1), _record(2)])

    result = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1), top_k=10, min_similarity=-1.0
    ))

    assert result.hit_count == 2
    assert result.items[0].similarity >= result.items[1].similarity


def test_min_similarity_threshold_filters_hits(store: SqliteVectorStore):
    store.upsert_many([_record(1), _record(2), _record(3)])

    strict = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1), top_k=5, min_similarity=0.99
    ))

    assert strict.hit_count == 1               # 只有自身命中
    assert strict.items[0].history_id == 1
    assert strict.degraded is False            # 零命中不等于降级
    assert strict.candidate_count == 3


def test_zero_hits_is_not_degraded(store: SqliteVectorStore):
    store.upsert_many([_record(1)])

    result = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(99), top_k=3, min_similarity=0.999
    ))

    assert result.hit_count == 0
    assert result.degraded is False
    assert result.degrade_reason is None
    assert result.enabled is True


def test_exclude_history_ids_removes_self(store: SqliteVectorStore):
    store.upsert_many([_record(1), _record(2)])

    result = store.search(RecallQuery(
        stock_code="600519",
        query_embedding=_unit(1),
        top_k=5,
        min_similarity=-1.0,
        exclude_history_ids=[1],
    ))

    assert [item.history_id for item in result.items] == [2]


def test_time_decay_reorders_by_recency(store: SqliteVectorStore):
    """旧记录即便相似度略高，也可能被更新的记录反超。"""
    today = date.today()
    base = _unit(1)
    stale = (base * 0.99 + _unit(2) * 0.01)
    stale = (stale / np.linalg.norm(stale)).astype(np.float32)

    store.upsert_many([
        _record(1, vector=base, trade_date=(today - timedelta(days=180)).isoformat()),
        _record(2, vector=stale, trade_date=today.isoformat()),
    ])

    decayed = store.search(RecallQuery(
        stock_code="600519", query_embedding=base, top_k=2,
        min_similarity=-1.0, apply_decay=True, halflife_days=60, lookback_days=365,
    ))
    raw = store.search(RecallQuery(
        stock_code="600519", query_embedding=base, top_k=2,
        min_similarity=-1.0, apply_decay=False, lookback_days=365,
    ))

    assert [item.history_id for item in raw.items] == [1, 2]        # 未衰减：旧的更相似
    assert [item.history_id for item in decayed.items] == [2, 1]    # 衰减后：新的胜出
    assert decayed.items[1].age_days >= 180
    assert decayed.items[0].final_score <= decayed.items[0].similarity + 1e-6


def test_decay_disabled_when_halflife_zero(store: SqliteVectorStore):
    old_date = (date.today() - timedelta(days=300)).isoformat()
    store.upsert_many([_record(1, trade_date=old_date)])

    result = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0,
        halflife_days=0, lookback_days=0,
    ))

    assert result.items[0].final_score == pytest.approx(result.items[0].similarity, abs=1e-6)


# --------------------------------------------------------------------------- #
# 5. 过滤维度：scope / lookback / report_type
# --------------------------------------------------------------------------- #

def test_scope_same_stock_excludes_other_codes(store: SqliteVectorStore):
    store.upsert_many([
        _record(1, code="600519", name="贵州茅台"),
        _record(2, code="000001", name="平安银行"),
    ])

    same = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1),
        scope=SCOPE_SAME_STOCK, top_k=5, min_similarity=-1.0,
    ))
    glob = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1),
        scope=SCOPE_GLOBAL, top_k=5, min_similarity=-1.0,
    ))

    assert [item.history_id for item in same.items] == [1]
    assert sorted(item.history_id for item in glob.items) == [1, 2]


def test_lookback_window_filters_old_rows(store: SqliteVectorStore):
    today = date.today()
    store.upsert_many([
        _record(1, trade_date=today.isoformat()),
        _record(2, trade_date=(today - timedelta(days=200)).isoformat()),
    ])

    result = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1),
        top_k=5, min_similarity=-1.0, lookback_days=90,
    ))

    assert [item.history_id for item in result.items] == [1]
    assert result.candidate_count == 1


def test_report_type_filter(store: SqliteVectorStore):
    store.upsert_many([
        _record(1, report_type="daily"),
        _record(2, report_type="intraday"),
    ])

    result = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(2),
        top_k=5, min_similarity=-1.0, report_type="intraday",
    ))

    assert [item.history_id for item in result.items] == [2]


def test_recall_item_carries_render_fields(store: SqliteVectorStore):
    store.upsert_many([_record(1, text="[趋势] 缩量回踩", time_slot="0930")])

    item = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0
    )).items[0]

    assert item.stock_code == "600519"
    assert item.stock_name == "贵州茅台"
    assert item.time_slot == "0930"
    assert item.conclusion_text == "[趋势] 缩量回踩"
    assert item.sentiment_score == 60
    assert item.operation_advice == "观望"
    assert item.outcome is None


# --------------------------------------------------------------------------- #
# 6. 缓存
# --------------------------------------------------------------------------- #

def test_candidate_cache_hits_and_invalidates_on_write(temp_db):
    cached_store = SqliteVectorStore(
        LOCAL_MODEL_ID, db_manager=temp_db, cache_ttl_seconds=300.0
    )
    cached_store.upsert_many([_record(1)])

    query = RecallQuery(stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0)
    first = cached_store.load_candidates(query)
    second = cached_store.load_candidates(query)
    assert second is first                       # 命中缓存返回同一对象

    cached_store.upsert_many([_record(2)])       # 写入后必须失效
    third = cached_store.load_candidates(query)
    assert third is not first
    assert len(third) == 2


def test_truncation_flag_set_when_over_max_candidates(temp_db):
    small = SqliteVectorStore(
        LOCAL_MODEL_ID, db_manager=temp_db, max_candidates=3, cache_ttl_seconds=0.0
    )
    small.upsert_many([_record(index) for index in range(1, 8)])

    result = small.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1), top_k=2, min_similarity=-1.0
    ))

    assert result.candidate_count == 3
    assert result.truncated is True


# --------------------------------------------------------------------------- #
# 7. fail-open
# --------------------------------------------------------------------------- #

def test_search_is_fail_open_on_store_error(store: SqliteVectorStore, monkeypatch):
    store.upsert_many([_record(1)])

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "_query_candidates", _boom)
    store.invalidate_cache()

    result = store.search(RecallQuery(
        stock_code="600519", query_embedding=_unit(1), min_similarity=-1.0
    ))

    assert result.hit_count == 0
    assert result.degraded is True
    assert result.degrade_reason == DegradeReason.STORE_ERROR.value


# --------------------------------------------------------------------------- #
# 8. MemoryWriter
# --------------------------------------------------------------------------- #

def test_build_conclusion_text_three_field_template():
    writer = MemoryWriter.__new__(MemoryWriter)   # 只测纯函数，跳过 IO 组装
    writer.conclusion_max_chars = 500

    text = writer.build_conclusion_text(
        trend="缩量回踩 20 日线", advice="观望", summary="主线切换至有色",
    )

    assert text == "[趋势] 缩量回踩 20 日线\n[建议] 观望\n[要点] 主线切换至有色"


def test_build_conclusion_text_skips_blank_fields():
    writer = MemoryWriter.__new__(MemoryWriter)
    writer.conclusion_max_chars = 500

    assert writer.build_conclusion_text(trend="放量突破") == "[趋势] 放量突破"
    assert writer.build_conclusion_text(advice="减仓") == "[建议] 减仓"
    assert writer.build_conclusion_text() == ""


def test_build_conclusion_text_strips_identity_tokens():
    """设计 §0 A8：名称与代码必须剥离，避免身份 token 拉高虚假相似度。"""
    writer = MemoryWriter.__new__(MemoryWriter)
    writer.conclusion_max_chars = 500

    text = writer.build_conclusion_text(
        trend="贵州茅台(600519) 缩量回踩",
        advice="sh600519 建议观望",
        summary="600519.SH 与白酒板块同步走弱",
        code="sh600519",
        name="贵州茅台",
    )

    assert "贵州茅台" not in text
    assert "600519" not in text
    assert "缩量回踩" in text
    assert "白酒板块同步走弱" in text


def test_build_conclusion_text_truncates_and_protects_head():
    """预算不足时优先保住 [趋势] / [建议]，压缩 [要点]。"""
    writer = MemoryWriter.__new__(MemoryWriter)
    writer.conclusion_max_chars = 60

    text = writer.build_conclusion_text(
        trend="缩量回踩", advice="观望", summary="要" * 500,
    )

    assert len(text) <= 60
    assert text.startswith("[趋势] 缩量回踩\n[建议] 观望\n[要点] ")
    assert text.endswith("…")


def test_build_conclusion_text_drops_summary_when_no_budget():
    writer = MemoryWriter.__new__(MemoryWriter)
    writer.conclusion_max_chars = 32

    text = writer.build_conclusion_text(trend="趋" * 40, summary="要点被挤掉")

    assert len(text) <= 32
    assert "[要点]" not in text


def test_writer_stage_and_flush_persists_vectors(temp_db):
    config = _make_config(ltm_embedding_dim=DIM)
    writer = MemoryWriter(
        config,
        provider=LocalLexicalEmbeddingProvider(dim=DIM),
        db_manager=temp_db,
    )

    assert writer.stage(
        history_id=101, code="600519", name="贵州茅台",
        trade_date=date.today().isoformat(), time_slot="1800",
        trend="缩量回踩", advice="观望", summary="资金分歧加大",
        sentiment_score=55, operation_advice="观望",
    ) is not None
    assert writer.pending_count == 1

    stats = writer.flush()

    assert stats["pending"] == 1
    assert stats["embedded"] == 1
    assert stats["written"] == 1
    assert stats["degraded"] is False
    assert writer.pending_count == 0
    assert writer.store.count() == 1

    result = writer.store.search(RecallQuery(
        stock_code="600519",
        query_embedding=writer.provider.embed(["[趋势] 缩量回踩"])[0],
        min_similarity=-1.0,
    ))
    assert result.items[0].history_id == 101


def test_writer_respects_master_switch(temp_db):
    writer = MemoryWriter(
        _make_config(ltm_enabled=False),
        provider=LocalLexicalEmbeddingProvider(dim=DIM),
        db_manager=temp_db,
    )

    assert writer.enabled is False
    assert writer.stage(history_id=1, code="600519", trend="任意") is None
    assert writer.flush()["written"] == 0
    assert writer.store.count() == 0


def test_writer_text_only_mode_skips_embedding(temp_db):
    writer = MemoryWriter(
        _make_config(ltm_write_mode="text_only"),
        provider=LocalLexicalEmbeddingProvider(dim=DIM),
        db_manager=temp_db,
    )

    writer.stage(history_id=7, code="600519", trend="放量突破")
    stats = writer.flush()

    assert stats["embedded"] == 0
    assert stats["written"] == 1
    assert stats["degraded"] is True
    assert stats["reason"] == "text_only"
    assert len(writer.store.list_missing_embedding()) == 1


def test_writer_falls_back_to_text_only_on_embed_failure(temp_db):
    class _BrokenProvider(LocalLexicalEmbeddingProvider):
        def embed(self, texts):
            raise ConnectionError("embedding endpoint unreachable")

    writer = MemoryWriter(
        _make_config(), provider=_BrokenProvider(dim=DIM), db_manager=temp_db
    )
    writer.stage(history_id=9, code="600519", trend="放量突破")

    stats = writer.flush()

    assert stats["degraded"] is True
    assert stats["reason"] == "embed_failed"
    assert stats["written"] == 1          # 文本仍然落盘，向量交给 backfill
    assert len(writer.store.list_missing_embedding()) == 1


def test_writer_flush_never_raises_on_store_failure(temp_db):
    writer = MemoryWriter(
        _make_config(), provider=LocalLexicalEmbeddingProvider(dim=DIM), db_manager=temp_db
    )
    writer.stage(history_id=11, code="600519", trend="放量突破")

    broken = MagicMock()
    broken.upsert_many.side_effect = RuntimeError("disk I/O error")
    writer.store = broken

    stats = writer.flush()

    assert stats["degraded"] is True
    assert stats["reason"] == "store_error"
    assert stats["written"] == 0
    assert writer.pending_count == 0      # 缓冲区已清空，不会无限堆积


def test_writer_flush_on_empty_buffer_is_noop(temp_db):
    writer = MemoryWriter(
        _make_config(), provider=LocalLexicalEmbeddingProvider(dim=DIM), db_manager=temp_db
    )

    stats = writer.flush()

    assert stats == {
        "pending": 0, "embedded": 0, "written": 0, "skipped": 0,
        "degraded": False, "reason": "", "elapsed_ms": stats["elapsed_ms"],
    }


def test_writer_batches_multiple_stocks_into_one_flush(temp_db):
    writer = MemoryWriter(
        _make_config(), provider=LocalLexicalEmbeddingProvider(dim=DIM), db_manager=temp_db
    )
    for index, (code, name) in enumerate(
        [("600519", "贵州茅台"), ("000001", "平安银行"), ("300750", "宁德时代")], start=1
    ):
        writer.stage(
            history_id=index, code=code, name=name,
            trend=f"情境 {index}", advice="观望",
        )

    stats = writer.flush()

    assert stats["pending"] == 3
    assert stats["embedded"] == 3
    assert stats["written"] == 3
    assert writer.store.count() == 3


def test_writer_coerces_sentiment_and_truncates_columns(temp_db):
    writer = MemoryWriter(
        _make_config(), provider=LocalLexicalEmbeddingProvider(dim=DIM), db_manager=temp_db
    )

    record = writer.stage(
        history_id=1, code="600519", name="名" * 80,
        trend="放量突破", sentiment_score="87.6", operation_advice="建议" * 30,
    )

    assert record is not None
    assert record.sentiment_score == 88
    assert len(record.name) <= 50
    assert len(record.operation_advice) <= 20
    assert writer._coerce_sentiment("abc") is None
    assert writer._coerce_sentiment(-5) == 0
    assert writer._coerce_sentiment(999) == 100
