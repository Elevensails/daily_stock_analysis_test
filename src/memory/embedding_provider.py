# -*- coding: utf-8 -*-
"""
===================================
U14 长期记忆 — Embedding Provider（两级降级链）
===================================

职责：
1. ``LiteLLMEmbeddingProvider`` —— 主路，走 ``litellm.embedding()``，
   完全照抄 ``src/analyzer.py::call_rewrite_llm()`` 的模块级薄封装范式
   （不依赖 analyzer 实例 / Router，api_key 由 litellm 按 provider 自动解析）
2. ``LocalLexicalEmbeddingProvider`` —— 兜底，纯标准库 + numpy 的词法向量，
   **零网络、零成本、逐位确定性**
3. ``build_embedding_provider(config)`` —— 工厂 + ``auto`` 降级链
4. ``probe_embedding_endpoint(config)`` —— 端点连通性探针（回应设计 §5.2 U2）

硬约束（设计 §0 A7）：
    **本模块严禁 import tiktoken / 调用 litellm.token_counter()。**
    二者首次调用会联网下载 BPE 词表，会直接摧毁"断网兜底"的成立性。
    所有 token 预算一律采用字符近似 ``len(text)``，与 U6 ``rag_max_prompt_tokens`` 同口径。

契约（设计 §3.2.1）：
    ``embed(texts: list[str]) -> np.ndarray``
    - shape  = ``(len(texts), dim)``
    - dtype  = ``np.float32``
    - 每行 L2 范数 = 1.0 ± 1e-5（零向量特判：全 0，点积恒 0，自然被阈值过滤）
    - **这是 src/memory 中唯一允许抛异常的层**（供工厂 / 门面捕获做降级判定）
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.memory.models import (
    DEFAULT_LITELLM_DIM,
    DEFAULT_LOCAL_DIM,
    LITELLM_MODEL_PREFIX,
    LOCAL_MODEL_ID,
    LOG_PREFIX,
    PROVIDER_LITELLM,
    PROVIDER_LOCAL,
)

logger = logging.getLogger(__name__)

#: 已知 embedding 模型的服务端固定维度（未命中时按 DEFAULT_LITELLM_DIM 预估，
#: 首次 embed 成功后会用真实返回维度回填）
_KNOWN_MODEL_DIMS: Dict[str, int] = {
    "openai/text-embedding-3-small": 1536,
    "text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
    "text-embedding-3-large": 3072,
    "openai/text-embedding-ada-002": 1536,
    "text-embedding-ada-002": 1536,
}

#: 工厂级降级原因（区别于 models.DegradeReason —— 后者描述的是"召回"层降级）
FACTORY_REASON_NONE = ""
FACTORY_REASON_NO_API_KEY = "no_api_key"
FACTORY_REASON_LITELLM_UNAVAILABLE = "litellm_unavailable"
FACTORY_REASON_EXPLICIT_LOCAL = "explicit_local"


def _load_litellm() -> Any:
    """惰性加载 litellm 模块（不可用时返回 ``None``，绝不抛异常）。

    刻意采用"调用时导入"而非模块级 import：
    ① litellm 导入耗时可观，无 key 场景下应完全跳过；
    ② 单测可直接 ``monkeypatch.setattr("litellm.embedding", ...)`` 生效；
    ③ litellm 缺失时本模块仍可正常提供 local provider（断网兜底成立）。
    Python 自身的模块缓存保证重复调用零开销。
    """
    try:
        import litellm  # noqa: PLC0415 —— 惰性导入是刻意设计
        return litellm
    except Exception as exc:  # pragma: no cover - 环境缺失时才会走到
        logger.debug("%s litellm 不可用: %s", LOG_PREFIX, exc)
        return None


# --------------------------------------------------------------------------- #
# 抽象基类
# --------------------------------------------------------------------------- #

class EmbeddingProvider(ABC):
    """Embedding 策略抽象（Strategy）。"""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """向量归属标识，格式 ``<provider>:<model>``，写入 DB 用于版本治理。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。litellm 路在首次 embed 成功前为预估值。"""

    @property
    def provider_name(self) -> str:
        """provider 短名（``litellm`` / ``local``），对齐 PRD §6.2 字段。"""
        return str(self.model_id or "").split(":", 1)[0]

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """把文本批量转为已 L2 归一化的 float32 向量矩阵。

        Args:
            texts: 文本列表。空串 / 纯空白允许存在，对应行返回全零向量。

        Returns:
            ``(len(texts), dim)`` 的 ``np.float32`` 矩阵。

        Raises:
            Exception: 允许抛出（唯一允许抛异常的层），由工厂 / 门面捕获降级。
        """

    def is_available(self) -> bool:
        """provider 是否可用（**不做真实网络探测**，避免每次构造都掏一次 RTT）。"""
        return True

    @staticmethod
    def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
        """按行 L2 归一化；零向量原样保留为全零（点积恒 0，自然被阈值过滤）。"""
        result = np.asarray(matrix, dtype=np.float32)
        if result.ndim == 1:
            result = result.reshape(1, -1)
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.asarray(result / norms, dtype=np.float32)

    @staticmethod
    def _sanitize_texts(texts: Optional[Sequence[str]]) -> List[str]:
        """把入参统一为字符串列表（``None`` → 空串）。"""
        return [("" if item is None else str(item)) for item in (texts or [])]


# --------------------------------------------------------------------------- #
# 主路：litellm
# --------------------------------------------------------------------------- #

class LiteLLMEmbeddingProvider(EmbeddingProvider):
    """走 ``litellm.embedding()`` 的主路 provider。

    照抄 ``src/analyzer.py::call_rewrite_llm()`` 范式：模块级薄封装、不依赖
    analyzer 实例与 Router，api_key 由 litellm 依据 model 自动从对应环境变量解析；
    仅在需要时透传 ``api_base`` / ``extra_headers``（复刻 ``extra_litellm_params()``）。
    """

    def __init__(
        self,
        model: str = "openai/text-embedding-3-small",
        *,
        timeout: float = 8.0,
        batch_size: int = 64,
        api_base: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        api_keys: Optional[Sequence[str]] = None,
    ) -> None:
        self._model = str(model or "openai/text-embedding-3-small").strip()
        self._timeout = float(timeout or 0.0)
        self._batch_size = max(int(batch_size or 64), 1)
        self._api_base = str(api_base or "").strip() or None
        self._extra_headers: Dict[str, str] = dict(extra_headers or {})
        self._api_keys: List[str] = [
            str(key).strip() for key in (api_keys or []) if str(key or "").strip()
        ]
        self._dim = int(_KNOWN_MODEL_DIMS.get(self._model, DEFAULT_LITELLM_DIM))

    @property
    def model_id(self) -> str:
        return f"{LITELLM_MODEL_PREFIX}{self._model}"

    @property
    def dim(self) -> int:
        return int(self._dim)

    @property
    def provider_name(self) -> str:
        return PROVIDER_LITELLM

    def _has_api_key(self) -> bool:
        """是否存在可用的 API key（env ▶ config.openai_api_keys）。"""
        env_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
        if len(env_key) >= 8:
            return True
        return any(len(key) >= 8 for key in self._api_keys)

    def is_available(self) -> bool:
        """可用性 = 有 key **且** litellm 可 import（不发起真实请求）。

        刻意先判 key 再判 import：无 key 场景可完全跳过 litellm 的重量级导入。
        """
        if not self._has_api_key():
            logger.debug("%s litellm provider 不可用：未配置 OPENAI_API_KEY", LOG_PREFIX)
            return False
        return _load_litellm() is not None

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """批量 embedding；空串行不发请求，直接补零向量。"""
        items = self._sanitize_texts(texts)
        if not items:
            return np.zeros((0, self.dim), dtype=np.float32)

        # 空白文本不送服务端（OpenAI /v1/embeddings 会对空串报 400），直接留零向量
        payload_index = [idx for idx, text in enumerate(items) if text.strip()]
        if not payload_index:
            return np.zeros((len(items), self.dim), dtype=np.float32)

        payload = [items[idx] for idx in payload_index]
        rows: List[List[float]] = []
        for start in range(0, len(payload), self._batch_size):
            rows.extend(self._call_litellm(payload[start:start + self._batch_size]))

        if len(rows) != len(payload):
            raise ValueError(
                f"litellm embedding 返回条数不匹配：期望 {len(payload)}，实得 {len(rows)}"
            )

        returned = np.asarray(rows, dtype=np.float32)
        if returned.ndim != 2 or returned.shape[1] <= 0:
            raise ValueError(f"litellm embedding 返回形状非法：{returned.shape}")

        self._dim = int(returned.shape[1])
        matrix = np.zeros((len(items), self._dim), dtype=np.float32)
        matrix[payload_index, :] = returned
        return self._l2_normalize(matrix)

    def _call_litellm(self, batch: List[str]) -> List[List[float]]:
        """单批次调用 ``litellm.embedding()`` 并抽取向量列表。"""
        litellm = _load_litellm()
        if litellm is None:
            raise RuntimeError("litellm 不可用（import 失败）")

        kwargs: Dict[str, Any] = {"model": self._model, "input": batch}
        if self._timeout > 0:
            kwargs["timeout"] = self._timeout
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._extra_headers:
            kwargs["extra_headers"] = dict(self._extra_headers)

        response = litellm.embedding(**kwargs)
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        if not data:
            raise ValueError("litellm embedding 返回空 data")

        vectors: List[List[float]] = []
        for entry in data:
            if isinstance(entry, dict):
                vector = entry.get("embedding")
            else:
                vector = getattr(entry, "embedding", None)
            if vector is None:
                raise ValueError("litellm embedding 返回条目缺少 embedding 字段")
            vectors.append([float(value) for value in vector])
        return vectors


# --------------------------------------------------------------------------- #
# 兜底：本地词法向量（零网络、逐位确定）
# --------------------------------------------------------------------------- #

class LocalLexicalEmbeddingProvider(EmbeddingProvider):
    """本地词法向量 provider（设计 §3.2.1 五步算法）。

    1. 分词：拉丁词 / 数字用正则提取；中文串按**相邻二字 bigram** 切
       （``涨停回调`` → ``涨停`` / ``停回`` / ``回调``），bigram 的中文语义粒度优于单字
    2. 哈希：``blake2b(tok, digest_size=8)`` → 64 位整数
    3. 投影：``idx = h % dim``，``sign = +1 if (h >> 63) & 1 else -1``（signed hashing trick）
    4. 累加：``vec[idx] += sign * (1 + log(tf))``（sublinear TF）
    5. L2 归一化

    **必须用 ``hashlib.blake2b``，禁用内置 ``hash()``** —— 后者受 ``PYTHONHASHSEED``
    随机化影响，会破坏跨进程确定性（同一文本在不同进程算出不同向量 = 向量库报废）。
    """

    #: 拉丁词与数字（含小数）
    _WORD_RE = re.compile(r"[a-zA-Z]+|\d+(?:\.\d+)?")
    #: CJK 统一表意文字（含扩展 A 区）
    _CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

    def __init__(self, dim: int = DEFAULT_LOCAL_DIM) -> None:
        try:
            resolved = int(dim)
        except (TypeError, ValueError):
            resolved = DEFAULT_LOCAL_DIM
        self._dim = max(resolved, 8)

    @property
    def model_id(self) -> str:
        return LOCAL_MODEL_ID

    @property
    def dim(self) -> int:
        return int(self._dim)

    @property
    def provider_name(self) -> str:
        return PROVIDER_LOCAL

    def is_available(self) -> bool:
        """本地 provider 恒可用（无外部依赖）。"""
        return True

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        items = self._sanitize_texts(texts)
        if not items:
            return np.zeros((0, self._dim), dtype=np.float32)
        matrix = np.zeros((len(items), self._dim), dtype=np.float32)
        for row, text in enumerate(items):
            matrix[row] = self._hash_project(self._tokenize(text))
        return self._l2_normalize(matrix)

    def _tokenize(self, text: str) -> List[str]:
        """正则分词：拉丁词/数字 + 中文相邻二字 bigram（单字串保留单字）。"""
        lowered = str(text or "").lower()
        if not lowered:
            return []
        tokens: List[str] = list(self._WORD_RE.findall(lowered))
        for run in self._CJK_RE.findall(lowered):
            if len(run) == 1:
                tokens.append(run)
                continue
            tokens.extend(run[idx:idx + 2] for idx in range(len(run) - 1))
        return tokens

    def _hash_project(self, tokens: Sequence[str]) -> np.ndarray:
        """signed hashing trick + sublinear TF 投影到固定维度（未归一化）。"""
        vector = np.zeros(self._dim, dtype=np.float32)
        if not tokens:
            return vector
        term_freq: Dict[str, int] = {}
        for token in tokens:
            term_freq[token] = term_freq.get(token, 0) + 1
        for token, count in term_freq.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            hashed = int.from_bytes(digest, "big")
            index = hashed % self._dim
            sign = 1.0 if (hashed >> 63) & 1 else -1.0
            vector[index] += np.float32(sign * (1.0 + math.log(count)))
        return vector


# --------------------------------------------------------------------------- #
# 工厂 + auto 降级链（设计 §4.3）
# --------------------------------------------------------------------------- #

def _build_litellm_provider(config: Any = None) -> LiteLLMEmbeddingProvider:
    """按 config 组装 litellm provider（含 api_base / aihubmix 头透传）。"""
    model = str(
        getattr(config, "ltm_embedding_model", "openai/text-embedding-3-small")
        or "openai/text-embedding-3-small"
    ).strip()
    timeout = float(getattr(config, "ltm_embed_timeout_seconds", 8.0) or 8.0)
    api_keys = list(getattr(config, "openai_api_keys", None) or [])

    api_base: Optional[str] = None
    extra_headers: Dict[str, str] = {}
    base_url = str(getattr(config, "openai_base_url", "") or "").strip()
    # 复刻 src/config.py::extra_litellm_params() —— 仅 openai 系模型才透传 api_base
    if base_url and (model.startswith("openai/") or "/" not in model):
        api_base = base_url
        if "aihubmix.com" in base_url:
            extra_headers["APP-Code"] = "GPIJ3886"

    return LiteLLMEmbeddingProvider(
        model,
        timeout=timeout,
        api_base=api_base,
        extra_headers=extra_headers,
        api_keys=api_keys,
    )


def build_embedding_provider(config: Any = None) -> Tuple[EmbeddingProvider, bool, str]:
    """按配置构造 embedding provider，实现 ``auto`` 两级降级链。

    Args:
        config: ``Config`` 实例（或任意具备同名属性的对象）；``None`` 时全走默认值。

    Returns:
        ``(provider, degraded, reason)``：
        - ``degraded`` 仅在"想用 litellm 却只能用 local"时为 ``True``；
          显式 ``provider="local"`` 属于主动选择，``degraded=False``。
        - ``reason`` 取值见 ``FACTORY_REASON_*``。

    Raises:
        RuntimeError: **仅当**显式指定 ``provider="litellm"`` 却不可用时抛出
            （不静默降级，由上层决定如何处置）。
    """
    mode = str(getattr(config, "ltm_embedding_provider", "auto") or "auto").strip().lower()
    if mode not in {"auto", "litellm", "local"}:
        logger.warning("%s 未知的 ltm_embedding_provider=%r，回落 auto", LOG_PREFIX, mode)
        mode = "auto"

    local_dim = int(getattr(config, "ltm_embedding_dim", DEFAULT_LOCAL_DIM) or DEFAULT_LOCAL_DIM)

    if mode == "local":
        logger.info("%s embedding provider=local（显式指定），dim=%d", LOG_PREFIX, local_dim)
        return LocalLexicalEmbeddingProvider(dim=local_dim), False, FACTORY_REASON_EXPLICIT_LOCAL

    litellm_provider = _build_litellm_provider(config)

    if mode == "litellm":
        if not litellm_provider.is_available():
            # 显式指定却不可用 → 抛出，由 LongTermMemory 捕获成 embed_failed
            raise RuntimeError(
                "显式指定 LTM_EMBEDDING_PROVIDER=litellm，但 litellm 不可用"
                "（未配置 OPENAI_API_KEY 或 litellm 未安装）。"
                "如需断网兜底请改用 auto 或 local。"
            )
        logger.info("%s embedding provider=litellm，model_id=%s", LOG_PREFIX, litellm_provider.model_id)
        return litellm_provider, False, FACTORY_REASON_NONE

    # mode == "auto"
    if litellm_provider.is_available():
        logger.info("%s embedding provider=litellm，model_id=%s", LOG_PREFIX, litellm_provider.model_id)
        return litellm_provider, False, FACTORY_REASON_NONE

    reason = (
        FACTORY_REASON_NO_API_KEY
        if not litellm_provider._has_api_key()  # noqa: SLF001 —— 同模块内私有访问
        else FACTORY_REASON_LITELLM_UNAVAILABLE
    )
    logger.info(
        "%s embedding provider=local, degraded=true, reason=%s, dim=%d",
        LOG_PREFIX, reason, local_dim,
    )
    return LocalLexicalEmbeddingProvider(dim=local_dim), True, reason


def build_local_provider(config: Any = None) -> LocalLexicalEmbeddingProvider:
    """直接构造本地兜底 provider（供降级重试路径复用）。"""
    local_dim = int(getattr(config, "ltm_embedding_dim", DEFAULT_LOCAL_DIM) or DEFAULT_LOCAL_DIM)
    return LocalLexicalEmbeddingProvider(dim=local_dim)


# --------------------------------------------------------------------------- #
# 端点连通性探针（设计 §5.2 U2）
# --------------------------------------------------------------------------- #

def probe_embedding_endpoint(
    config: Any = None,
    sample_text: str = "长期记忆语义召回连通性探针",
) -> Dict[str, Any]:
    """探测 ``config.openai_base_url`` 是否真的支持 ``/v1/embeddings`` 端点。

    回应设计 §5.2 U2：部分中转站（aihubmix 等）只代理 ``/v1/chat/completions``，
    上线前需要一次真实往返确认，并核对返回维度是否与预期一致（预期 1536）。

    **本函数不抛异常**，所有失败都写进返回值的 ``error`` 字段。

    Returns:
        ``{"ok", "model_id", "dim", "elapsed_ms", "api_base", "error"}``
    """
    info: Dict[str, Any] = {
        "ok": False,
        "model_id": "",
        "dim": 0,
        "elapsed_ms": 0.0,
        "api_base": str(getattr(config, "openai_base_url", "") or "") or "(litellm 默认)",
        "error": "",
    }
    started = time.perf_counter()
    try:
        provider = _build_litellm_provider(config)
        info["model_id"] = provider.model_id
        if not provider.is_available():
            info["error"] = "litellm 不可用：未配置 OPENAI_API_KEY 或 litellm 未安装"
        else:
            matrix = provider.embed([sample_text])
            info["dim"] = int(matrix.shape[1])
            info["ok"] = True
    except Exception as exc:  # noqa: BLE001 —— 探针必须吞掉一切异常
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        info["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return info


__all__ = [
    "EmbeddingProvider",
    "LiteLLMEmbeddingProvider",
    "LocalLexicalEmbeddingProvider",
    "build_embedding_provider",
    "build_local_provider",
    "probe_embedding_endpoint",
    "FACTORY_REASON_NONE",
    "FACTORY_REASON_NO_API_KEY",
    "FACTORY_REASON_LITELLM_UNAVAILABLE",
    "FACTORY_REASON_EXPLICIT_LOCAL",
]
