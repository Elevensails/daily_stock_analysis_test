# -*- coding: utf-8 -*-
"""
===================================================
RAG — neodata query.py 路径解析（唯一真源）
===================================================

背景
----
``src/rag/news.py`` 与 ``src/rag/financial.py`` 都需要定位本地 neodata skill 的
``query.py``。P1.1 时两处各写了一份逻辑，其中 financial.py 里硬编码了开发者机器的
绝对路径（``C:\\Users\\<user>\\...``），导致：

1. 换机 / CI 上必然探测失败；
2. `tests/test_rag_news_akshare.py::test_no_hardcoded_path_anywhere_in_src` 红灯。

本模块把「config → env → 家目录候选」三级解析抽成唯一真源，供两个调用方复用。

依赖方向铁律
------------
本模块**只依赖标准库**，不 import ``news`` / ``financial`` / ``retriever``，
因此 ``news.py`` 与 ``financial.py`` 同时依赖它也不会形成循环导入
（依赖图是 news → _neodata_resolver ← financial 的树形结构）。
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# neodata query.py 候选路径（相对家目录，按序探测）。
# 注意：这里**不得**出现任何开发者机器的用户名 / 绝对路径，
# 真正的绝对路径请通过 ``RAG_NEODATA_SCRIPT_PATH`` 环境变量
# 或 config.yaml 的 ``rag.neodata_script_path`` 指定。
NEODATA_RELATIVE_CANDIDATES = (
    Path(".workbuddy/skills/neodata-financial-search/scripts/query.py"),
    Path(".claude/skills/neodata-financial-search/scripts/query.py"),
)

# 环境变量名（唯一真源，禁止在别处写裸字符串）
NEODATA_SCRIPT_ENV_VAR = "RAG_NEODATA_SCRIPT_PATH"

# Config 属性名（唯一真源）
NEODATA_SCRIPT_CONFIG_ATTR = "rag_neodata_script_path"


def resolve_neodata_script(
    config: Any = None,
    *,
    log_prefix: str = "RAG",
) -> Optional[Path]:
    """解析 neodata ``query.py`` 路径（config/env 驱动，无硬编码用户路径）。

    探测顺序：

    1. ``config.rag_neodata_script_path``
    2. 环境变量 ``RAG_NEODATA_SCRIPT_PATH``
    3. 家目录下的候选相对路径（见 :data:`NEODATA_RELATIVE_CANDIDATES`）

    Args:
        config: Config 实例，可为 None
        log_prefix: 日志前缀，便于区分调用方（``RAG.news`` / ``RAG.financial``）

    Returns:
        存在的脚本路径；全部探测不到返回 None（CI / 纯服务器环境的正常情况，
        调用方应静默降级，不得打 warning）
    """
    explicit: str = ""
    if config is not None:
        explicit = str(getattr(config, NEODATA_SCRIPT_CONFIG_ATTR, "") or "").strip()
    if not explicit:
        explicit = str(os.getenv(NEODATA_SCRIPT_ENV_VAR, "") or "").strip()

    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate
        logger.debug("[%s] 配置的 neodata 脚本路径不存在: %s", log_prefix, candidate)

    try:
        home = Path.home()
    except Exception:  # 某些容器环境无 HOME
        logger.debug("[%s] 无法解析家目录，跳过 neodata 候选路径探测", log_prefix)
        return None

    for relative in NEODATA_RELATIVE_CANDIDATES:
        candidate = home / relative
        if candidate.exists():
            return candidate

    return None
