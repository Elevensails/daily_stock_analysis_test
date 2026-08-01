# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — 财报检索子模块
===================================

策略链：neodata CLI（首选）→ efinance（回退）→ 空 block

- neodata：通过 subprocess 调用 ``query.py``，超时 8s
- efinance：复用 data_provider 已有封装
- 所有解析异常走回退，不抛异常
"""

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ._neodata_resolver import resolve_neodata_script

logger = logging.getLogger(__name__)


def _resolve_neodata_script(config: Any = None) -> Optional[Path]:
    """解析 neodata ``query.py`` 路径，探测不到时返回 None。

    P1.6 修复：原实现硬编码了开发者机器的绝对路径，换机 / CI 必然失败。
    现统一复用 :func:`src.rag._neodata_resolver.resolve_neodata_script`
    的三级解析（config → env → 家目录候选），与 ``news.py`` 行为一致。

    Args:
        config: Config 实例，可为 None

    Returns:
        存在的脚本路径；探测不到返回 None
    """
    return resolve_neodata_script(config, log_prefix="RAG.financial")


def retrieve_financial(
    code: str,
    stock_name: str,
    *,
    config: Any = None,
) -> str:
    """检索财报关键指标。

    首选 neodata CLI，失败回退 efinance。

    Args:
        code: 股票代码
        stock_name: 股票名称
        config: Config 实例（用于超时配置）

    Returns:
        格式化为 markdown 表格的财报数据字符串（≤400 chars），失败返回 ""
    """
    timeout = getattr(config, 'rag_neodata_timeout_seconds', 8.0) if config else 8.0

    # ── 首选：neodata CLI ──
    neodata_text = _neodata_financial(code, stock_name, timeout=timeout, config=config)
    if neodata_text:
        return neodata_text

    # ── 回退：efinance ──
    efinance_text = _efinance_fallback(code)
    if efinance_text:
        return efinance_text

    logger.warning("[RAG.financial] 财报检索全部数据源失败，返回空 block")
    return ""


def _neodata_financial(
    code: str,
    stock_name: str,
    *,
    timeout: float = 8.0,
    config: Any = None,
) -> str:
    """通过 neodata CLI 检索财报数据。

    Args:
        code: 股票代码
        stock_name: 股票名称
        timeout: subprocess 超时秒数
        config: Config 实例（用于解析 neodata 脚本路径），可为 None

    Returns:
        格式化的 markdown 表格字符串，失败返回 ""
    """
    script_path = _resolve_neodata_script(config)
    if not script_path:
        # CI / 纯服务器环境没有 neodata skill 是预期内的正常降级，
        # 用 debug 而非 warning，避免刷屏（与 news.py 行为对齐）。
        logger.debug("[RAG.financial] neodata 脚本未配置或不存在，跳过该数据源")
        return ""

    query = f"{stock_name} 最新财报 市盈率 市净率 ROE 营收增速 净利润 净利润增速 毛利率 资产负债率"
    cmd = [
        sys.executable, str(script_path),
        "--query", query,
    ]

    try:
        logger.debug("[RAG.financial] 调用 neodata: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            logger.warning("[RAG.financial] neodata 返回非零: rc=%s stderr=%s",
                           result.returncode, result.stderr[:200])
            return ""

        raw_output = result.stdout.strip()
        if not raw_output:
            return ""

        return _parse_neodata_financial(raw_output, stock_name, code)

    except subprocess.TimeoutExpired:
        logger.warning("[RAG.financial] neodata 超时 (%.1fs)", timeout)
        return ""
    except Exception as exc:
        logger.warning("[RAG.financial] neodata 调用异常: %s", exc)
        return ""


def _parse_neodata_financial(raw: str, stock_name: str, code: str) -> str:
    """解析 neodata 返回文本，提取关键财务指标。

    neodata 返回的是自然语言文本（非结构化 JSON），
    需要用正则提取 PE/PB/ROE/营收增速/净利润 等数值。
    """
    try:
        lines: list = []

        # 提取 PE（市盈率）
        pe_match = re.search(r'市[盈赢]率[：:\s]*([\d.]+)', raw)
        if pe_match:
            lines.append(f"| 市盈率(PE) | {pe_match.group(1)} |")

        # 提取 PB（市净率）
        pb_match = re.search(r'市净率[：:\s]*([\d.]+)', raw)
        if pb_match:
            lines.append(f"| 市净率(PB) | {pb_match.group(1)} |")

        # 提取 ROE
        roe_match = re.search(r'ROE[：:\s]*([\d.]+%?)', raw, re.IGNORECASE)
        if roe_match:
            lines.append(f"| ROE | {roe_match.group(1)} |")

        # 提取营收增速
        rev_match = re.search(r'营收[增速增长][：:\s]*([\d.\-+]+%?)', raw)
        if rev_match:
            lines.append(f"| 营收增速 | {rev_match.group(1)} |")

        # 提取净利润
        np_match = re.search(r'净利润[增速增长]?[：:\s]*([\d.\-+]+%?)', raw)
        if np_match:
            label = "净利润增速" if "增速" in raw[np_match.start():np_match.start()+10] else "净利润"
            lines.append(f"| {label} | {np_match.group(1)} |")

        # 提取毛利率
        gm_match = re.search(r'毛利率[：:\s]*([\d.]+%?)', raw)
        if gm_match:
            lines.append(f"| 毛利率 | {gm_match.group(1)} |")

        # 提取资产负债率
        dr_match = re.search(r'资产负债率[：:\s]*([\d.]+%?)', raw)
        if dr_match:
            lines.append(f"| 资产负债率 | {dr_match.group(1)} |")

        if not lines:
            # 兜底：取前 300 字符作为纯文本
            snippet = raw[:300].replace("\n", " ").strip()
            if len(snippet) > 20:
                lines.append(f"| 原始数据 | {snippet} |")

        if not lines:
            return ""

        header = f"### 财报速览\n| 指标 | 数值 |\n|------|------|\n"
        body = "\n".join(lines)
        result = header + body

        # 截断到 400 字符
        if len(result) > 400:
            result = result[:397] + "..."

        return result

    except Exception as exc:
        logger.warning("[RAG.financial] 解析 neodata 返回失败: %s", exc)
        return ""


def _efinance_fallback(code: str) -> str:
    """通过 efinance 获取财报数据作为回退。

    Returns:
        格式化的 markdown 表格字符串，失败返回 ""
    """
    try:
        from data_provider.base import DataFetcherManager
        manager = DataFetcherManager()

        # 尝试获取股票基本信息
        info = manager.get_stock_info(code)
        if not info or not isinstance(info, dict):
            return ""

        lines: list = []
        pe = info.get("pe") or info.get("PE")
        pb = info.get("pb") or info.get("PB")
        roe = info.get("roe") or info.get("ROE")

        if pe is not None:
            lines.append(f"| 市盈率(PE) | {pe} |")
        if pb is not None:
            lines.append(f"| 市净率(PB) | {pb} |")
        if roe is not None:
            lines.append(f"| ROE | {roe} |")

        if not lines:
            return ""

        header = "### 财报速览\n| 指标 | 数值 |\n|------|------|\n"
        body = "\n".join(lines)
        result = header + body + "\n> ⚠️ 数据来源：efinance（回退）"

        # 截断到 400 字符
        if len(result) > 400:
            result = result[:397] + "..."

        return result

    except ImportError:
        logger.debug("[RAG.financial] data_provider 不可用，跳过 efinance 回退")
        return ""
    except Exception as exc:
        logger.warning("[RAG.financial] efinance 回退失败: %s", exc)
        return ""
