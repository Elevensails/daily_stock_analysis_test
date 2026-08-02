# -*- coding: utf-8 -*-
"""
U14 长期记忆 — Embedding 双路 + 遥测 单测

覆盖（对应设计 §7 测试策略 T02 部分）：
1. 本地词法 provider：shape / dtype / L2 范数 / **跨实例确定性**
2. 空文本 / 空列表 边界
3. litellm provider：mock 成功路径的形状与归一化
4. litellm provider：mock ``ConnectionError`` → auto 链降级到 local（不抛给上层）
5. 工厂三种模式（local / litellm / auto）的 degraded 与 reason
6. ``probe_embedding_endpoint`` 断网下不抛异常
7. ``append_recall_record`` 写 JSONL 的字段齐全性 + 失败静默
8. **A7 铁律守卫**：``src/memory/`` 全包不得出现 tiktoken

全部用例零网络：litellm 通过注入 ``sys.modules["litellm"]`` 桩件模拟。
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.memory.embedding_provider import (  # noqa: E402
    FACTORY_REASON_EXPLICIT_LOCAL,
    FACTORY_REASON_LITELLM_UNAVAILABLE,
    FACTORY_REASON_NO_API_KEY,
    FACTORY_REASON_NONE,
    LiteLLMEmbeddingProvider,
    LocalLexicalEmbeddingProvider,
    build_embedding_provider,
    build_local_provider,
    probe_embedding_endpoint,
)
from src.memory.models import (  # noqa: E402
    LOCAL_MODEL_ID,
    PROVIDER_LITELLM,
    PROVIDER_LOCAL,
    DegradeReason,
    RecallItem,
    RecallResult,
)
from src.memory.telemetry import RECALL_LOG_FIELDS, append_recall_record  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #

class _FakeConfig:
    """最小 Config 替身（只带 provider 关心的属性）。"""

    def __init__(self, **kwargs: object) -> None:
        self.ltm_embedding_provider = "auto"
        self.ltm_embedding_model = "openai/text-embedding-3-small"
        self.ltm_embedding_dim = 1024
        self.ltm_embed_timeout_seconds = 8.0
        self.openai_api_keys: list = []
        self.openai_base_url = ""
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture(autouse=True)
def _isolate_api_key(monkeypatch: pytest.MonkeyPatch):
    """默认清空 OPENAI_API_KEY，保证用例不受本机环境影响。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch):
    """注入 litellm 桩件；返回该模块对象供用例改写 ``embedding``。"""
    module = types.ModuleType("litellm")
    module.calls = []  # type: ignore[attr-defined]

    def _embedding(**kwargs):
        module.calls.append(kwargs)  # type: ignore[attr-defined]
        batch = list(kwargs.get("input") or [])
        # 造一个与文本长度相关、但非归一化的确定性向量（维度 4，便于断言）
        return {
            "data": [
                {"embedding": [float(len(text)), 1.0, 2.0, 3.0]}
                for text in batch
            ]
        }

    module.embedding = _embedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", module)
    return module


# --------------------------------------------------------------------------- #
# 1. 本地词法 provider
# --------------------------------------------------------------------------- #

def test_local_provider_shape_dtype_and_l2_norm():
    """输出形状 / dtype / 逐行 L2 范数必须严格满足契约。"""
    provider = LocalLexicalEmbeddingProvider(dim=256)
    texts = ["今日涨停回调，建议减仓观望", "AI 算力板块放量突破 20 日均线", "sentiment 65 hold"]

    matrix = provider.embed(texts)

    assert matrix.shape == (3, 256)
    assert matrix.dtype == np.float32
    norms = np.linalg.norm(matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)
    assert provider.model_id == LOCAL_MODEL_ID
    assert provider.provider_name == PROVIDER_LOCAL
    assert provider.is_available() is True


def test_local_provider_is_deterministic_across_instances():
    """同一文本在不同实例上必须产出逐位一致的向量（blake2b 而非内置 hash）。"""
    text = "沪指缩量震荡，主线切换至有色金属"
    first = LocalLexicalEmbeddingProvider(dim=128).embed([text])
    second = LocalLexicalEmbeddingProvider(dim=128).embed([text])

    assert np.array_equal(first, second)
    # 自相似度必须为 1
    assert float(first[0] @ second[0]) == pytest.approx(1.0, abs=1e-5)


def test_local_provider_empty_inputs_return_zero_rows():
    """空列表返回 (0, dim)；空串行返回零向量而不是 NaN。"""
    provider = LocalLexicalEmbeddingProvider(dim=64)

    empty = provider.embed([])
    assert empty.shape == (0, 64)
    assert empty.dtype == np.float32

    blanks = provider.embed(["", "   ", None])  # type: ignore[list-item]
    assert blanks.shape == (3, 64)
    assert not np.isnan(blanks).any()
    assert np.allclose(blanks, 0.0)


def test_local_provider_similar_text_scores_higher_than_unrelated():
    """词法向量应保有基本可辨识度：近义句相似度 > 无关句相似度。"""
    provider = LocalLexicalEmbeddingProvider(dim=1024)
    matrix = provider.embed([
        "涨停后回调，建议减仓观望，情绪转弱",
        "涨停后回调，建议减仓，情绪偏弱",
        "港口物流稳步扩张，海外订单同比大增",
    ])

    similar = float(matrix[0] @ matrix[1])
    unrelated = float(matrix[0] @ matrix[2])
    assert similar > unrelated
    assert similar > 0.5


def test_local_provider_tokenizer_uses_cjk_bigram():
    """中文按相邻二字 bigram 切分；单字串保留单字。"""
    provider = LocalLexicalEmbeddingProvider(dim=64)

    assert provider._tokenize("涨停回调") == ["涨停", "停回", "回调"]
    assert provider._tokenize("多") == ["多"]
    assert provider._tokenize("MACD 金叉 12.5") == ["macd", "12.5", "金叉"]


def test_local_provider_dim_is_clamped():
    """非法维度回落到安全下界，绝不产生 0 维矩阵。"""
    assert LocalLexicalEmbeddingProvider(dim=0).dim == 8
    assert LocalLexicalEmbeddingProvider(dim="bad").dim == 1024  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 2. litellm provider
# --------------------------------------------------------------------------- #

def test_litellm_provider_embed_normalizes_and_backfills_dim(
    monkeypatch: pytest.MonkeyPatch, fake_litellm
):
    """mock 成功路径：返回矩阵已归一化，且真实维度被回填。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")
    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-small", timeout=3.0)

    assert provider.is_available() is True
    matrix = provider.embed(["第一条结论", "第二条更长一点的结论文本"])

    assert matrix.shape == (2, 4)
    assert matrix.dtype == np.float32
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    # 服务端真实维度（4）覆盖了预估维度（1536）
    assert provider.dim == 4
    assert provider.provider_name == PROVIDER_LITELLM
    assert provider.model_id == "litellm:openai/text-embedding-3-small"
    # 空白行不送服务端
    assert fake_litellm.calls[0]["input"] == ["第一条结论", "第二条更长一点的结论文本"]
    assert fake_litellm.calls[0]["timeout"] == 3.0


def test_litellm_provider_skips_blank_rows(monkeypatch: pytest.MonkeyPatch, fake_litellm):
    """空白文本不进请求体，对应行补零向量。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")
    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-small")

    matrix = provider.embed(["有内容", "   ", "也有内容"])

    assert matrix.shape == (3, 4)
    assert np.allclose(matrix[1], 0.0)
    assert fake_litellm.calls[0]["input"] == ["有内容", "也有内容"]


def test_litellm_provider_unavailable_without_api_key(fake_litellm):
    """无 key 时 is_available 为 False（且不发起任何调用）。"""
    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-small")

    assert provider.is_available() is False
    assert fake_litellm.calls == []


def test_litellm_provider_raises_on_connection_error(
    monkeypatch: pytest.MonkeyPatch, fake_litellm
):
    """embed 是唯一允许抛异常的层——网络错误必须原样上抛供工厂/门面判定。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")

    def _boom(**kwargs):
        raise ConnectionError("connection refused")

    fake_litellm.embedding = _boom
    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-small")

    with pytest.raises(ConnectionError):
        provider.embed(["任意文本"])


def test_litellm_provider_rejects_malformed_response(
    monkeypatch: pytest.MonkeyPatch, fake_litellm
):
    """返回条数不匹配 / 缺字段时必须显式报错，不得静默产出脏向量。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")
    fake_litellm.embedding = lambda **kwargs: {"data": [{"embedding": [1.0, 2.0]}]}
    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-small")

    with pytest.raises(ValueError):
        provider.embed(["a", "b"])


def test_litellm_provider_passes_api_base_and_aihubmix_header(
    monkeypatch: pytest.MonkeyPatch, fake_litellm
):
    """复刻 extra_litellm_params()：aihubmix 中转需带 APP-Code 头。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")
    config = _FakeConfig(
        ltm_embedding_provider="litellm",
        openai_base_url="https://aihubmix.com/v1",
    )

    provider, degraded, reason = build_embedding_provider(config)
    provider.embed(["探针"])

    assert degraded is False
    assert reason == FACTORY_REASON_NONE
    call = fake_litellm.calls[0]
    assert call["api_base"] == "https://aihubmix.com/v1"
    assert call["extra_headers"]["APP-Code"] == "GPIJ3886"


# --------------------------------------------------------------------------- #
# 3. 工厂 + auto 降级链
# --------------------------------------------------------------------------- #

def test_factory_explicit_local_is_not_degraded():
    """显式 local 是主动选择，不计入降级。"""
    provider, degraded, reason = build_embedding_provider(
        _FakeConfig(ltm_embedding_provider="local", ltm_embedding_dim=512)
    )

    assert isinstance(provider, LocalLexicalEmbeddingProvider)
    assert provider.dim == 512
    assert degraded is False
    assert reason == FACTORY_REASON_EXPLICIT_LOCAL


def test_factory_auto_degrades_to_local_without_api_key(fake_litellm):
    """auto + 无 key → 降级 local，reason=no_api_key（这是 CI 的默认路径）。"""
    provider, degraded, reason = build_embedding_provider(
        _FakeConfig(ltm_embedding_provider="auto")
    )

    assert isinstance(provider, LocalLexicalEmbeddingProvider)
    assert degraded is True
    assert reason == FACTORY_REASON_NO_API_KEY


def test_factory_auto_degrades_when_litellm_import_fails(monkeypatch: pytest.MonkeyPatch):
    """auto + 有 key 但 litellm 不可导入 → 降级 local，reason=litellm_unavailable。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")
    monkeypatch.setitem(sys.modules, "litellm", None)

    provider, degraded, reason = build_embedding_provider(
        _FakeConfig(ltm_embedding_provider="auto")
    )

    assert isinstance(provider, LocalLexicalEmbeddingProvider)
    assert degraded is True
    assert reason == FACTORY_REASON_LITELLM_UNAVAILABLE


def test_factory_auto_prefers_litellm_when_available(
    monkeypatch: pytest.MonkeyPatch, fake_litellm
):
    """auto + 有 key + litellm 可用 → 走主路，不降级。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")

    provider, degraded, reason = build_embedding_provider(
        _FakeConfig(ltm_embedding_provider="auto")
    )

    assert isinstance(provider, LiteLLMEmbeddingProvider)
    assert degraded is False
    assert reason == FACTORY_REASON_NONE


def test_factory_explicit_litellm_raises_when_unavailable(fake_litellm):
    """显式 litellm 却不可用 → 抛 RuntimeError，绝不静默降级。"""
    with pytest.raises(RuntimeError, match="litellm"):
        build_embedding_provider(_FakeConfig(ltm_embedding_provider="litellm"))


def test_factory_unknown_mode_falls_back_to_auto(fake_litellm):
    """未知模式回落 auto（配置层已有校验，此处是运行期兜底）。"""
    provider, degraded, _ = build_embedding_provider(
        _FakeConfig(ltm_embedding_provider="nonsense")
    )

    assert isinstance(provider, LocalLexicalEmbeddingProvider)
    assert degraded is True


def test_build_local_provider_respects_config_dim():
    assert build_local_provider(_FakeConfig(ltm_embedding_dim=333)).dim == 333
    assert build_local_provider(None).dim == 1024


# --------------------------------------------------------------------------- #
# 4. 连通性探针
# --------------------------------------------------------------------------- #

def test_probe_never_raises_without_api_key(fake_litellm):
    """断网/无 key 场景：探针返回 ok=False 且带 error 说明，不抛异常。"""
    info = probe_embedding_endpoint(_FakeConfig())

    assert info["ok"] is False
    assert info["error"]
    assert info["model_id"] == "litellm:openai/text-embedding-3-small"
    assert info["elapsed_ms"] >= 0.0


def test_probe_reports_dim_on_success(monkeypatch: pytest.MonkeyPatch, fake_litellm):
    """探针成功时回报真实维度（用于核对中转站是否真支持 /v1/embeddings）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")

    info = probe_embedding_endpoint(_FakeConfig())

    assert info["ok"] is True
    assert info["dim"] == 4
    assert info["error"] == ""


def test_probe_swallows_network_error(monkeypatch: pytest.MonkeyPatch, fake_litellm):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-1234567890")

    def _boom(**kwargs):
        raise ConnectionError("dns failure")

    fake_litellm.embedding = _boom

    info = probe_embedding_endpoint(_FakeConfig())

    assert info["ok"] is False
    assert "ConnectionError" in info["error"]


# --------------------------------------------------------------------------- #
# 5. 遥测
# --------------------------------------------------------------------------- #

def _sample_result() -> RecallResult:
    return RecallResult(
        enabled=True,
        degraded=False,
        degrade_reason=None,
        embedding_provider=PROVIDER_LOCAL,
        model_id=LOCAL_MODEL_ID,
        candidate_count=12,
        hit_count=2,
        elapsed_ms=8.125,
        items=[
            RecallItem(history_id=1, similarity=0.91, final_score=0.88, conclusion_text="a"),
            RecallItem(history_id=2, similarity=0.80, final_score=0.71, conclusion_text="b"),
        ],
        stock_code="600519",
        scope="same_stock",
        min_similarity=0.75,
    )


def test_append_recall_record_writes_all_prd_fields(tmp_path: Path):
    """JSONL 单行必须覆盖 PRD §6.3 全字段。"""
    log_path = tmp_path / "nested" / "memory_recall.jsonl"

    append_recall_record(
        _sample_result(),
        log_path=str(log_path),
        run_id="run-1",
        trace_id="trace-1",
        injected_chars=321,
    )

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    for field_name in RECALL_LOG_FIELDS:
        assert field_name in record, f"缺失字段 {field_name}"
    assert record["stock_code"] == "600519"
    assert record["hit_count"] == 2
    assert record["top_similarity"] == pytest.approx(0.91)
    assert record["degraded"] is False
    assert record["degrade_reason"] is None
    assert record["injected_chars"] == 321
    # A7：不得出现 token 口径字段
    assert "injected_tokens" not in record


def test_append_recall_record_appends_and_supports_extra(tmp_path: Path):
    """多次调用为追加；extra 字段合并但不覆盖核心字段。"""
    log_path = tmp_path / "memory_recall.jsonl"

    append_recall_record(_sample_result(), log_path=str(log_path), stage="legacy")
    append_recall_record(
        RecallResult.empty(DegradeReason.MODEL_MISMATCH, stock_code="000001"),
        log_path=str(log_path),
        hit_count=999,  # 试图覆盖核心字段，应被忽略
    )

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["stage"] == "legacy"
    assert second["degraded"] is True
    assert second["degrade_reason"] == "model_mismatch"
    assert second["hit_count"] == 0  # 未被 extra 覆盖


def test_append_recall_record_without_result_writes_skeleton(tmp_path: Path):
    """result=None（功能未启用）时写骨架记录，字段仍然齐全。"""
    log_path = tmp_path / "memory_recall.jsonl"

    append_recall_record(None, log_path=str(log_path))

    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    for field_name in RECALL_LOG_FIELDS:
        assert field_name in record
    assert record["degrade_reason"] == "disabled"


def test_append_recall_record_is_silent_on_failure(tmp_path: Path):
    """路径不可写时静默返回，绝不向主流程抛异常。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")

    # 把文件当目录用 → mkdir 必然失败
    append_recall_record(_sample_result(), log_path=str(blocker / "sub" / "x.jsonl"))
    # 目录本身当文件写 → open("a") 必然失败
    append_recall_record(_sample_result(), log_path=str(tmp_path))


# --------------------------------------------------------------------------- #
# 6. A7 铁律守卫
# --------------------------------------------------------------------------- #

def test_memory_package_never_imports_tiktoken():
    """设计 §0 A7：src/memory 全包禁用 tiktoken / token_counter（会联网下词表）。

    用 AST 而非文本扫描，避免把文档字符串里"严禁 import tiktoken"的说明
    误判成真实依赖。
    """
    package_dir = REPO_ROOT / "src" / "memory"
    offenders = []
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "tiktoken":
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if str(node.module or "").split(".")[0] == "tiktoken":
                    offenders.append(f"{path.name}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "token_counter":
                    offenders.append(f"{path.name}:{node.lineno} token_counter()")
    assert offenders == [], f"检测到 tiktoken 依赖: {offenders}"
