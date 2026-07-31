# -*- coding: utf-8 -*-
"""
===================================
U6 RAG — 技术信号检索子模块
===================================

策略链：westock CLI（首选）→ enhanced_context fallback（回退）

- westock：通过 subprocess 调用 CLI，获取 MACD/KDJ/RSI/BOLL 衍生信号
- enhanced_context：从已有上下文中提取 MA/BIAS 基础数据
- 去重：不返回 enhanced_context 已包含的 MA5/MA10/MA20/BIAS 等字段

P0 处理：westock CLI 不可用时直接走 enhanced_context fallback，
并标记 "衍生信号缺失（数据源未就绪）"。
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# westock-data skill 的潜在 CLI 入口路径
_WESTOCK_PATHS = [
    Path(os.path.expanduser("~/.workbuddy/connectors/westock-mcp/index.js")),
    Path(os.path.expanduser("~/.workbuddy/skills/westock-data/index.js")),
]


def _resolve_westock_cli() -> Optional[Path]:
    """解析 westock CLI 入口，不存在时返回 None。"""
    for p in _WESTOCK_PATHS:
        if p.exists():
            return p
    return None


def retrieve_technical(
    code: str,
    *,
    enhanced_context: Optional[Dict[str, Any]] = None,
    config: Any = None,
) -> str:
    """检索衍生技术信号。

    首选 westock CLI，失败回退 enhanced_context 提取基础 MA/BIAS 数据。

    Args:
        code: 股票代码
        enhanced_context: 已增强的分析上下文（用于回退提取）
        config: Config 实例

    Returns:
        格式化为 markdown 列表的技术信号字符串（≤500 chars），失败返回 ""
    """
    timeout = getattr(config, 'rag_westock_timeout_seconds', 5.0) if config else 5.0

    # ── 首选：westock CLI ──
    westock_text = _westock_technical(code, timeout=timeout)
    if westock_text:
        return westock_text

    # ── 回退：enhanced_context ──
    fallback_text = _enhanced_context_fallback(code, enhanced_context)
    if fallback_text:
        return fallback_text

    logger.warning("[RAG.technical] 技术信号检索全部数据源失败，返回空 block")
    return ""


def _westock_technical(code: str, *, timeout: float = 5.0) -> str:
    """通过 westock CLI 检索衍生技术信号。

    Returns:
        格式化的 markdown 列表字符串，失败返回 ""
    """
    cli_path = _resolve_westock_cli()
    if not cli_path:
        logger.debug("[RAG.technical] westock CLI 不可用，跳过")
        return ""

    cmd = ["node", str(cli_path), "technical", "--code", code]
    try:
        logger.debug("[RAG.technical] 调用 westock: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            logger.warning("[RAG.technical] westock 返回非零: rc=%s", result.returncode)
            return ""

        raw = result.stdout.strip()
        if not raw:
            return ""

        return _parse_westock_technical(raw)

    except subprocess.TimeoutExpired:
        logger.warning("[RAG.technical] westock 超时 (%.1fs)", timeout)
        return ""
    except FileNotFoundError:
        logger.debug("[RAG.technical] node 不可用，跳过 westock")
        return ""
    except Exception as exc:
        logger.warning("[RAG.technical] westock 调用异常: %s", exc)
        return ""


def _parse_westock_technical(raw: str) -> str:
    """解析 westock 返回文本，提取衍生技术信号。

    提取：MACD 金叉/死叉、KDJ 超买/超卖、BOLL 位置、RSI、量能信号
    """
    try:
        lines: list = []
        lower = raw.lower()

        # MACD 信号
        if "金叉" in raw or "golden cross" in lower:
            lines.append("- **MACD**：金叉信号，短期偏多")
        elif "死叉" in raw or "death cross" in lower:
            lines.append("- **MACD**：死叉信号，短期偏空")

        # KDJ 信号
        if "超买" in raw or "overbought" in lower:
            lines.append("- **KDJ**：超买区域，注意回调风险")
        elif "超卖" in raw or "oversold" in lower:
            lines.append("- **KDJ**：超卖区域，存在反弹可能")

        # BOLL 位置
        if "上轨" in raw or "upper band" in lower:
            lines.append("- **BOLL**：价格接近上轨，压力位附近")
        elif "下轨" in raw or "lower band" in lower:
            lines.append("- **BOLL**：价格接近下轨，支撑位附近")

        # RSI
        import re
        rsi_match = re.search(r'RSI[：:\s]*([\d.]+)', raw, re.IGNORECASE)
        if rsi_match:
            rsi_val = float(rsi_match.group(1))
            if rsi_val > 70:
                lines.append(f"- **RSI**：{rsi_val}，超买区域")
            elif rsi_val < 30:
                lines.append(f"- **RSI**：{rsi_val}，超卖区域")
            else:
                lines.append(f"- **RSI**：{rsi_val}，中性区域")

        # 量能信号
        if "放量" in raw or "volume increase" in lower:
            lines.append("- **量能**：放量信号，主力参与度提升")
        elif "缩量" in raw or "volume decrease" in lower:
            lines.append("- **量能**：缩量状态，市场观望情绪浓")

        if not lines:
            snippet = raw[:200].replace("\n", " ").strip()
            if len(snippet) > 20:
                lines.append(f"- 原始信号：{snippet}")

        if not lines:
            return ""

        header = "### 技术信号\n"
        result = header + "\n".join(lines)

        if len(result) > 500:
            result = result[:497] + "..."

        return result

    except Exception as exc:
        logger.warning("[RAG.technical] 解析 westock 返回失败: %s", exc)
        return ""


def _enhanced_context_fallback(
    code: str,
    enhanced_context: Optional[Dict[str, Any]] = None,
) -> str:
    """从 enhanced_context 提取基础 MA/BIAS 数据作为回退。

    只提取 enhanced_context 中已存在的字段，不重复拉取。
    标记 westock CLI 不可用。
    """
    if not enhanced_context or not isinstance(enhanced_context, dict):
        return (
            "### 技术信号\n"
            "- ⚠️ 衍生信号缺失（数据源未就绪），基础均线数据见技术面数据区\n"
        )

    today = enhanced_context.get("today", {})
    if not isinstance(today, dict):
        today = {}

    lines: list = []
    lines.append("- ⚠️ 衍生信号缺失（westock CLI 不可用），以下为基础均线参考：")

    ma5 = today.get("ma5")
    ma10 = today.get("ma10")
    ma20 = today.get("ma20")
    current = today.get("close")

    if ma5 is not None and ma10 is not None:
        try:
            if float(ma5) > float(ma10):
                lines.append(f"- MA5({ma5}) > MA10({ma10})：短期偏多")
            elif float(ma5) < float(ma10):
                lines.append(f"- MA5({ma5}) < MA10({ma10})：短期偏空")
        except (ValueError, TypeError):
            pass

    if ma10 is not None and ma20 is not None:
        try:
            if float(ma10) > float(ma20):
                lines.append(f"- MA10({ma10}) > MA20({ma20})：中期偏多")
            elif float(ma10) < float(ma20):
                lines.append(f"- MA10({ma10}) < MA20({ma20})：中期偏空")
        except (ValueError, TypeError):
            pass

    # BIAS 判断（从 enhanced_context 的 trend_analysis 提取）
    trend = enhanced_context.get("trend_analysis", {})
    if isinstance(trend, dict):
        bias_ma5 = trend.get("bias_ma5")
        if bias_ma5 is not None:
            try:
                bias_val = float(bias_ma5)
                if abs(bias_val) > 5:
                    lines.append(f"- 乖离率(MA5)：{bias_val:+.2f}%，偏离较大")
                else:
                    lines.append(f"- 乖离率(MA5)：{bias_val:+.2f}%，位置可控")
            except (ValueError, TypeError):
                pass

    header = "### 技术信号\n"
    result = header + "\n".join(lines)

    if len(result) > 500:
        result = result[:497] + "..."

    return result
