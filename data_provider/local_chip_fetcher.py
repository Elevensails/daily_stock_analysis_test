# -*- coding: utf-8 -*-
"""
===================================
本地筹码分布数据源（最低优先级兜底）
===================================

背景：
    东方财富筹码接口（``ak.stock_cyq_em``）IP 被封、``tushare.cyq_chips`` 需 5000 积分，
    导致 A 股个股在真实链路上取不到任何筹码数据。本数据源把上期交付的
    :mod:`data_provider.local_chip_calculator`（纯标准库、零网络）接入
    :class:`~data_provider.base.DataFetcherManager` 的筹码源发现链，
    用「未被封的日 K 线」本地估算筹码分布，补上这个洞。

定位：
    - **只是筹码源，不是行情源**。通过 :meth:`is_available_for_request` 对
      ``daily_data`` / ``realtime_quote`` / ``stock_name`` / ``stock_list``
      等能力一律声明不可用，确保 manager 的 ``_filter_fetchers_by_capability``
      会在进入日线路由前把它剔除，绝不污染既有取数链路。
    - ``priority`` 取 95（默认 99 之下、真实源之上都不是），保证 akshare / tushare
      等真实筹码源全部失败后才轮到它。

循环依赖治理（本模块的核心设计约束）：
    本 fetcher 是 manager 的**成员**，却需要 K 线数据作为输入。若反向调用
    ``manager.get_daily_data()`` 会形成「manager → fetcher → manager」的调用环，
    manager 内部逻辑一旦变动就可能递归。因此这里采用**自持 K 线来源（方案 A）**：
    内部惰性 new 一个 :class:`~data_provider.tencent_fetcher.TencentFetcher`，
    完全自治、不持有也不引用 manager。
    同时保留 ``kline_provider`` 注入口（方案 B），供测试与未来装配方替换，
    默认不使用——即「A 为默认，B 为可选注入」。

    模块级只从 ``.base`` 单向 import；``TencentFetcher`` 采用**函数内延迟 import**，
    ``base.py`` 侧也只在 ``_init_default_fetchers()`` 函数体内 import 本模块，
    因此 import 期不存在任何环。
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from .base import (
    BaseFetcher,
    DataFetchError,
    _is_etf_code,
    _market_tag,
    normalize_stock_code,
)
from .local_chip_calculator import compute_chip_distribution
from .realtime_types import ChipDistribution, ChipFetchResult

logger = logging.getLogger(__name__)

__all__ = ["LocalChipFetcher"]

# 默认回看的交易日数量（任务要求 60~120，取上界以让筹码沉淀更充分）
DEFAULT_KLINE_DAYS: int = 120
# 少于这个根数就认为样本不足，语义落 EMPTY 而非 FETCH_FAILED
MIN_KLINE_BARS: int = 20
# 交易日 → 日历日的放大系数（A 股一年约 242 个交易日 / 365 个日历日）
_CALENDAR_DAYS_RATIO: float = 1.6
# 估算流通股本用的典型日换手率（见 local_chip_calculator.build_chip_profile）
DEFAULT_TYPICAL_TURNOVER: float = 0.02

# 本 fetcher 唯一承认的能力标识；其余能力一律声明不可用
_SUPPORTED_CAPABILITIES = frozenset({"chip", "chip_distribution"})

# 从标准化 DataFrame 里取用的列（其余技术指标列对筹码计算无意义）
_KLINE_COLUMNS = ("date", "open", "high", "low", "close", "volume")


class LocalChipFetcher(BaseFetcher):
    """本地计算筹码分布的兜底数据源。

    仅实现筹码相关接口，其余 :class:`BaseFetcher` 抽象方法以
    :class:`DataFetchError` 明确拒绝（正常情况下不可达，见 :meth:`is_available_for_request`）。

    Attributes:
        name: 数据源名称，manager 会据此生成熔断 key ``localchip_chip``
        priority: 95，低于所有真实数据源，确保只做兜底
    """

    name: str = "LocalChipFetcher"
    priority: int = 95
    allow_empty_daily_data: bool = True

    def __init__(
        self,
        kline_provider: Optional[Callable[[str, int], Any]] = None,
        *,
        kline_days: int = DEFAULT_KLINE_DAYS,
        typical_turnover: float = DEFAULT_TYPICAL_TURNOVER,
    ) -> None:
        """初始化本地筹码数据源。

        Args:
            kline_provider: 可选的 K 线回调 ``(stock_code, days) -> DataFrame|list[dict]``。
                为 ``None`` 时自持一个 ``TencentFetcher``（方案 A）。
                **禁止**传入会回调 manager 的实现，否则重新引入调用环。
            kline_days: 回看的交易日数量，默认 120
            typical_turnover: 估算流通股本用的典型日换手率，默认 2%
        """
        self._kline_provider: Optional[Callable[[str, int], Any]] = kline_provider
        self._kline_days: int = max(MIN_KLINE_BARS, int(kline_days or DEFAULT_KLINE_DAYS))
        self._typical_turnover: float = (
            typical_turnover if typical_turnover and typical_turnover > 0 else DEFAULT_TYPICAL_TURNOVER
        )
        self._owned_kline_fetcher: Optional[BaseFetcher] = None

    # ------------------------------------------------------------------
    # 能力声明：确保它不会被误用为行情源
    # ------------------------------------------------------------------
    def is_available_for_request(self, capability: str = "") -> bool:
        """声明本数据源仅服务筹码能力。

        ``DataFetcherManager._filter_fetchers_by_capability`` /
        ``_get_fetcher_by_name`` 都会走这个探针，返回 ``False`` 即代表
        「本轮请求不要用我」。筹码链路（``get_chip_distribution_ex``）走的是
        ``hasattr`` 发现而非能力探针，因此不受影响。

        Args:
            capability: 能力标识，如 ``daily_data`` / ``realtime_quote``

        Returns:
            仅当 capability 属于筹码语义时为 True
        """
        return (capability or "").strip().lower() in _SUPPORTED_CAPABILITIES

    # ------------------------------------------------------------------
    # BaseFetcher 抽象方法：本源不提供行情，明确拒绝
    # ------------------------------------------------------------------
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """本数据源不提供行情，调用即报错（正常链路不可达）。

        Raises:
            DataFetchError: 恒抛。选 ``DataFetchError`` 而非 ``NotImplementedError``
                是因为它属于数据层既有异常体系，manager 各处循环都能优雅降级，
                不会把「误用」升级成崩溃。
        """
        raise DataFetchError(
            f"[{self.name}] 仅提供筹码分布，不提供 {stock_code} 的日线行情"
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """本数据源不提供行情，调用即报错（正常链路不可达）。

        Raises:
            DataFetchError: 恒抛，理由同 :meth:`_fetch_raw_data`
        """
        raise DataFetchError(
            f"[{self.name}] 仅提供筹码分布，不提供 {stock_code} 的日线标准化"
        )

    # ------------------------------------------------------------------
    # 筹码接口
    # ------------------------------------------------------------------
    def get_chip_distribution(self, stock_code: str) -> Optional[ChipDistribution]:
        """获取筹码分布（向后兼容 shim，丢弃原因码）。

        Args:
            stock_code: 股票代码

        Returns:
            ChipDistribution（``source="local"``），不可用时返回 None
        """
        return self.get_chip_distribution_ex(stock_code).chip

    def get_chip_distribution_ex(self, stock_code: str) -> ChipFetchResult:
        """本地计算筹码分布（带原因码）。

        流程：
            1. 配置开关关闭 → ``DISABLED``
            2. 非 A 股 / ETF / 非个股代码 → ``NOT_APPLICABLE``（静默跳过、不触碰熔断器）
            3. 取近 ``kline_days`` 个交易日日 K；取数异常 → ``FETCH_FAILED``
            4. K 线为空或不足 ``MIN_KLINE_BARS`` 根 → ``EMPTY``
            5. 本地计算；返回 None → ``EMPTY``，否则 ``OK``

        Args:
            stock_code: 股票代码

        Returns:
            ChipFetchResult，永不返回 None、永不抛异常
        """
        source = self.name
        started = time.time()

        def _elapsed_ms() -> int:
            return int((time.time() - started) * 1000)

        try:
            if not self._is_enabled():
                return ChipFetchResult.disabled(
                    source, "config.enable_local_chip_fallback=False"
                )

            code = normalize_stock_code(stock_code)
            not_applicable = self._not_applicable_reason(code)
            if not_applicable:
                logger.debug("[本地筹码源] 跳过 %s: %s", code, not_applicable)
                return ChipFetchResult.na(not_applicable, source, latency_ms=_elapsed_ms())

            try:
                records = self._load_kline_records(code)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning("[本地筹码源] %s K 线获取失败: %s", code, detail)
                return ChipFetchResult.failed(
                    f"K 线获取失败 - {detail}",
                    source,
                    latency_ms=_elapsed_ms(),
                    error_type=type(exc).__name__,
                )

            if len(records) < MIN_KLINE_BARS:
                logger.info(
                    "[本地筹码源] %s K 线样本不足: %d 根 < %d 根",
                    code,
                    len(records),
                    MIN_KLINE_BARS,
                )
                return ChipFetchResult.empty(
                    f"K 线样本不足（{len(records)}/{MIN_KLINE_BARS} 根）",
                    source,
                    latency_ms=_elapsed_ms(),
                )

            chip = compute_chip_distribution(
                code,
                records,
                typical_turnover=self._typical_turnover,
            )
            if chip is None:
                logger.info("[本地筹码源] %s 计算未产出有效分布", code)
                return ChipFetchResult.empty(
                    "本地计算未产出有效分布", source, latency_ms=_elapsed_ms()
                )

            logger.info(
                "[本地筹码源] %s 计算成功: bars=%d, avg_cost=%.3f, profit_ratio=%.4f",
                code,
                len(records),
                chip.avg_cost,
                chip.profit_ratio,
            )
            return ChipFetchResult.ok_of(chip, source, latency_ms=_elapsed_ms())

        except Exception as exc:  # 最外层兜底：数据层永不向上抛异常
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("[本地筹码源] %s 未预期异常: %s", stock_code, detail, exc_info=True)
            return ChipFetchResult.failed(
                detail, source, latency_ms=_elapsed_ms(), error_type=type(exc).__name__
            )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _is_enabled() -> bool:
        """读取 ``config.enable_local_chip_fallback`` 开关。

        Returns:
            开关值；config 读取异常时保守返回 True（不因配置问题丢功能）
        """
        try:
            from src.config import get_config

            return bool(getattr(get_config(), "enable_local_chip_fallback", True))
        except Exception as exc:  # pragma: no cover - config 异常极罕见
            logger.debug("[本地筹码源] 读取配置开关失败，按启用处理: %s", exc)
            return True

    @staticmethod
    def _not_applicable_reason(code: str) -> str:
        """判定标的是否「本就无筹码」。

        Args:
            code: 已归一化的股票代码

        Returns:
            不适用原因串；适用则返回空串
        """
        if not code:
            return "空股票代码"
        market = _market_tag(code)
        if market != "cn":
            return f"{market.upper()} 市场标的无本地筹码计算"
        if _is_etf_code(code):
            return "ETF/指数无筹码分布数据"
        if not (code.isdigit() and len(code) == 6):
            return f"非 A 股个股代码（{code}）"
        return ""

    def _get_kline_fetcher(self) -> BaseFetcher:
        """惰性构建自持的 K 线数据源（方案 A）。

        用腾讯直连接口：未被东财封禁、无需凭证、volume 已由
        ``_lots_to_shares`` 归一为「股」，与筹码计算器的换手率口径一致。

        Returns:
            TencentFetcher 实例（进程内复用）
        """
        if self._owned_kline_fetcher is None:
            from .tencent_fetcher import TencentFetcher  # 延迟 import，杜绝模块级环

            self._owned_kline_fetcher = TencentFetcher()
        return self._owned_kline_fetcher

    def _fetch_kline_frame(self, code: str, days: int) -> Any:
        """按配置取 K 线：优先注入的 provider（方案 B），否则自持源（方案 A）。

        Args:
            code: 已归一化的股票代码
            days: 回看的交易日数量

        Returns:
            DataFrame 或 list[dict]；不做异常吞噬，由调用方归一为 FETCH_FAILED
        """
        if self._kline_provider is not None:
            return self._kline_provider(code, days)

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=int(days * _CALENDAR_DAYS_RATIO) + 20)
        return self._get_kline_fetcher().get_daily_data(
            code,
            start_date=start_dt.strftime("%Y-%m-%d"),
            end_date=end_dt.strftime("%Y-%m-%d"),
            days=days,
        )

    def _load_kline_records(self, code: str) -> List[Dict[str, Any]]:
        """取 K 线并归一为计算器可直接消费的 ``list[dict]``（时间升序）。

        显式做这一步而不是把 DataFrame 直接丢给计算器，是为了：
        1. 丢掉 ma5/volume_ratio 等与筹码无关的列，减少脏字段干扰；
        2. 把 ``_clean_data`` 转成的 ``pd.Timestamp`` 日期还原成 ``YYYY-MM-DD``，
           否则 ``ChipDistribution.date`` 会变成 "2026-08-03 00:00:00"；
        3. 只保留最后 ``kline_days`` 根，避免上游多给数据拉长衰减窗口。

        Args:
            code: 已归一化的股票代码
            days 由 ``self._kline_days`` 决定

        Returns:
            升序的 K 线记录列表；无数据时返回空列表
        """
        raw = self._fetch_kline_frame(code, self._kline_days)
        if raw is None:
            return []

        rows = self._to_row_dicts(raw)
        records: List[Dict[str, Any]] = []
        for row in rows:
            record = self._normalize_kline_row(row)
            if record is not None:
                records.append(record)

        # 已按日期升序：_clean_data 会 sort_values('date')；注入 provider 亦要求升序。
        # 这里只截尾部，保证窗口长度可控。
        if len(records) > self._kline_days:
            records = records[-self._kline_days:]
        return records

    @staticmethod
    def _to_row_dicts(raw: Any) -> List[Dict[str, Any]]:
        """把 DataFrame / 可迭代对象统一成 ``list[dict]``。

        Args:
            raw: K 线原始容器

        Returns:
            行字典列表；无法识别时返回空列表
        """
        if hasattr(raw, "columns") and hasattr(raw, "to_dict"):
            if getattr(raw, "empty", False):
                return []
            return list(raw.to_dict("records"))
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [row for row in raw if isinstance(row, dict)]
        return []

    @classmethod
    def _normalize_kline_row(cls, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """抽取单行的 OHLCV 与日期，非法行返回 None。

        Args:
            row: 单根 K 线的行字典

        Returns:
            只含 ``_KLINE_COLUMNS`` 的干净字典；close 非法时返回 None
        """
        record: Dict[str, Any] = {}
        for column in _KLINE_COLUMNS:
            value = row.get(column)
            if column == "date":
                record["date"] = cls._format_date(value)
            else:
                record[column] = cls._to_float(value)

        if record.get("close", 0.0) <= 0:
            return None
        return record

    @staticmethod
    def _to_float(value: Any) -> float:
        """安全转 float（None / NaN / 非法值统一落 0.0）。"""
        if value is None:
            return 0.0
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result

    @staticmethod
    def _format_date(value: Any) -> str:
        """把日期值格式化为 ``YYYY-MM-DD``（兼容 Timestamp / datetime / str）。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()[:10]
        strftime = getattr(value, "strftime", None)
        if callable(strftime):
            try:
                return strftime("%Y-%m-%d")
            except Exception:  # pragma: no cover - 极端日期对象
                return str(value)[:10]
        return str(value)[:10]
