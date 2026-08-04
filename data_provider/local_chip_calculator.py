# -*- coding: utf-8 -*-
"""
===================================
本地筹码分布计算器（零网络依赖）
===================================

背景：
    东方财富筹码接口（akshare.stock_cyq_em）IP 被封、tushare.cyq_chips 需 5000 积分，
    因此改为「本地计算」：仅依赖腾讯/新浪日 K 线（这些接口未被封），
    用业界通用的「三角形分布 + 换手率衰减」模型估算筹码分布。

算法要点：
    1. 价格轴分箱：[min(low), max(high)] 切成 N 个等宽 bin，每个 bin 维护一个筹码累加器。
    2. 逐日推进（时间升序）：
       a. 换手率衰减：chip_b *= (1 - turnover_i)，模拟老筹码被换手置换出去；
       b. 三角形派发：当日成交量按三角形分布摊到 [low_i, high_i]，
          峰值价取重心价 peak = (high + low + 2*close) / 4。
    3. 汇总：获利比例 / 平均成本 / 90%(5%~95%) 与 70%(15%~85%) 成本区间与集中度。

工程约束：
    - 纯标准库实现（math / bisect / logging / dataclasses），不依赖 numpy/pandas/akshare/requests；
    - 可在无网沙箱与 CI 中直接单测；
    - 复用既有契约 data_provider.realtime_types.ChipDistribution，source 固定为 "local"。
"""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from data_provider.realtime_types import ChipDistribution
except ImportError:  # pragma: no cover - 以脚本方式直接运行本文件时的兜底
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from data_provider.realtime_types import ChipDistribution

logger = logging.getLogger(__name__)

__all__ = [
    "KLineBar",
    "ChipProfile",
    "build_chip_profile",
    "compute_chip_distribution",
]

# 单日最大换手率上限（防止脏量导致筹码被一次性清空）
MAX_DAILY_TURNOVER: float = 0.95
# bin 数量安全区间
MIN_BINS: int = 20
MAX_BINS: int = 2000
# 默认 bin 数（未指定 bins 且价格步长自适应失败时使用）
DEFAULT_BINS: int = 200

# 常见列名别名（兼容不同 fetcher 的字段命名）
_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "date": ("date", "day", "trade_date", "日期"),
    "open": ("open", "open_price", "开盘"),
    "high": ("high", "high_price", "最高"),
    "low": ("low", "low_price", "最低"),
    "close": ("close", "close_price", "收盘"),
    "volume": ("volume", "vol", "成交量"),
}


# ============================================
# 基础数据结构
# ============================================

@dataclass
class KLineBar:
    """标准化后的单日 K 线。"""

    date: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class ChipProfile:
    """离散化的筹码分布剖面（供统计汇总与后续可视化复用）。"""

    price_min: float = 0.0
    bin_width: float = 0.0
    prices: List[float] = field(default_factory=list)   # 各 bin 中心价（升序）
    weights: List[float] = field(default_factory=list)  # 各 bin 筹码权重
    total: float = 0.0                                  # 权重合计
    last_date: str = ""
    last_close: float = 0.0

    def get_distribution_array(self) -> List[Tuple[float, float]]:
        """返回 [(价格, 筹码占比)] 列表，占比归一到 1，便于绘图。"""
        if self.total <= 0:
            return []
        return [(p, w / self.total) for p, w in zip(self.prices, self.weights)]

    def avg_cost(self) -> float:
        """加权平均成本。"""
        if self.total <= 0:
            return 0.0
        return sum(p * w for p, w in zip(self.prices, self.weights)) / self.total

    def profit_ratio(self, current_price: float) -> float:
        """获利比例：成本不高于现价的筹码占比。

        比较时放宽半个 bin（``+ bin_width / 2``）：即「价格区间覆盖现价的那个 bin」
        算作获利盘。否则 bin 中心恒高于其左边界，会系统性低估获利比例最多半个 bin
        的筹码量。

        .. warning::
            **这半个 bin 不是可有可无的容差，删掉会导致一字板/长期停牌静默误报。**
            此类标的的筹码会退化到单个 bin，且该 bin 中心恒高于现价（20.0 一口价
            → bin 中心 20.005），严格比较 ``p_b <= current_price`` 会得到
            profit_ratio=0.0（"全员套牢"），而正确答案是 1.0（成本等于现价、全员保本）。
            对拍实测：正常数据严格/放宽两种算法仅差 0.0030（可忽略），
            一字板则是 0.0 vs 1.0 的定性错误。
            与 ``build_chip_profile()`` 中的一口价撑开分支配套存在，
            回归测试见 ``test_half_bin_rule_rescues_degenerate_single_point``。
        """
        if self.total <= 0 or not self.prices:
            return 0.0
        idx = bisect.bisect_right(self.prices, current_price + self.bin_width / 2.0)
        return sum(self.weights[:idx]) / self.total

    def quantile(self, q: float) -> float:
        """累计权重分位对应的价格（bin 内线性插值，结果单调）。"""
        if self.total <= 0 or not self.weights:
            return 0.0
        target = max(0.0, min(1.0, q)) * self.total
        acc = 0.0
        for i, w in enumerate(self.weights):
            if w <= 0.0:
                continue
            if acc + w >= target:
                frac = (target - acc) / w
                return self.price_min + (i + frac) * self.bin_width
            acc += w
        return self.price_min + len(self.weights) * self.bin_width


# ============================================
# 输入标准化
# ============================================

def _to_float(value: Any, default: float = 0.0) -> float:
    """安全转 float（None / 空串 / NaN / 非法值统一落默认值）。"""
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if value in ("", "-", "--"):
            return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _pick(record: Dict[str, Any], key: str) -> Any:
    """按别名表从记录里取字段。"""
    for alias in _FIELD_ALIASES[key]:
        if alias in record:
            return record[alias]
    return None


def _records_from_klines(klines: Any) -> List[Dict[str, Any]]:
    """把 klines 统一成 list[dict]（兼容 pandas.DataFrame）。"""
    if klines is None:
        return []
    # DataFrame：有 columns 且有 to_dict 才认定，避免误判普通 dict
    if hasattr(klines, "columns") and hasattr(klines, "to_dict"):
        try:
            return list(klines.to_dict("records"))
        except Exception as exc:  # pragma: no cover - 依赖外部对象行为
            logger.debug("[本地筹码] DataFrame 转 records 失败: %s", exc)
            return []
    if isinstance(klines, dict):
        return [klines]
    if isinstance(klines, Iterable):
        return [r for r in klines if isinstance(r, dict)]
    return []


def _normalize_bars(
    klines: Any = None,
    prices: Optional[Sequence[float]] = None,
    volumes: Optional[Sequence[float]] = None,
    dates: Optional[Sequence[str]] = None,
) -> List[KLineBar]:
    """把多种输入形态统一成按时间升序的 KLineBar 列表，并剔除脏数据。

    分派策略：先把 klines 归一成 records，**按 records 是否非空**决定走哪条分支，
    而不是判 ``klines is not None``。这样 ``klines=[]``（空列表）能正确回落到
    prices/volumes 兜底，而不是被静默屏蔽。

    注意不能简写成 ``if klines:``——pandas.DataFrame 的布尔求值会抛
    ``ValueError: The truth value of a DataFrame is ambiguous``。
    """
    bars: List[KLineBar] = []
    records = _records_from_klines(klines)

    if records:
        for i, rec in enumerate(records):
            high = _to_float(_pick(rec, "high"))
            low = _to_float(_pick(rec, "low"))
            close = _to_float(_pick(rec, "close"))
            open_ = _to_float(_pick(rec, "open"), default=close)
            volume = max(0.0, _to_float(_pick(rec, "volume")))
            date = str(_pick(rec, "date") or "")

            # 缺 high/low 时退化为收盘价单点
            if high <= 0 and close > 0:
                high = close
            if low <= 0 and close > 0:
                low = close
            if close <= 0 and high > 0 and low > 0:
                close = (high + low) / 2.0

            if close <= 0 or high <= 0 or low <= 0:
                logger.debug("[本地筹码] 第 %d 根 K 线价格非法，跳过: %s", i, rec)
                continue
            if high < low:
                logger.debug("[本地筹码] 第 %d 根 K 线 high<low，跳过: %s", i, rec)
                continue
            # close 越界时夹回区间，避免三角形峰值跑到区间外
            close = min(max(close, low), high)
            bars.append(
                KLineBar(date=date, open=open_, high=high, low=low, close=close, volume=volume)
            )
        return bars

    if prices:
        price_list = [_to_float(p) for p in prices]
        vol_list = [max(0.0, _to_float(v)) for v in volumes] if volumes else []
        date_list = [str(d) for d in dates] if dates else []
        for i, price in enumerate(price_list):
            if price <= 0:
                logger.debug("[本地筹码] 第 %d 个收盘价非法，跳过: %r", i, price)
                continue
            volume = vol_list[i] if i < len(vol_list) else 1.0
            date = date_list[i] if i < len(date_list) else ""
            bars.append(
                KLineBar(date=date, open=price, high=price, low=price, close=price, volume=volume)
            )
    return bars


def _resolve_bin_count(price_min: float, price_max: float, bins: Optional[int]) -> int:
    """确定 bin 数量：显式指定优先，否则按价格量级自适应步长。"""
    if bins is not None and bins > 0:
        return max(MIN_BINS, min(MAX_BINS, int(bins)))

    span = price_max - price_min
    if span <= 0:
        return MIN_BINS
    if price_max < 10:
        step = 0.01
    elif price_max < 50:
        step = 0.02
    elif price_max < 100:
        step = 0.05
    else:
        step = 0.1
    count = int(math.ceil(span / step))
    if count <= 0:
        count = DEFAULT_BINS
    return max(MIN_BINS, min(MAX_BINS, count))


# ============================================
# 三角形分布
# ============================================

def _triangular_cdf(x: float, low: float, peak: float, high: float) -> float:
    """三角形分布 CDF，用于精确积分每个 bin 上的筹码占比。"""
    if x <= low:
        return 0.0
    if x >= high:
        return 1.0
    span = high - low
    if x <= peak:
        left = peak - low
        if left <= 0:
            return 0.0
        return (x - low) ** 2 / (span * left)
    right = high - peak
    if right <= 0:
        return 1.0
    return 1.0 - (high - x) ** 2 / (span * right)


def _distribute_triangle(
    weights: List[float],
    bar: KLineBar,
    price_min: float,
    bin_width: float,
    volume: Optional[float] = None,
) -> None:
    """把当日成交量按三角形分布摊入各 bin（原地累加）。

    Args:
        volume: 派发量；为 None 时取 bar.volume（无量数据可显式传等权值）
    """
    n_bins = len(weights)
    if volume is None:
        volume = bar.volume
    if volume <= 0:
        return

    low, high = bar.low, bar.high
    # 峰值取重心价：收盘价权重加倍，更贴近真实成交密度
    peak = (high + low + 2.0 * bar.close) / 4.0
    peak = min(max(peak, low), high)

    idx_lo = min(max(int((low - price_min) / bin_width), 0), n_bins - 1)
    idx_hi = min(max(int((high - price_min) / bin_width), 0), n_bins - 1)

    # 退化：当日无振幅或落在同一个 bin → 全部集中于该 bin
    if high <= low or idx_lo == idx_hi:
        weights[idx_lo] += volume
        return

    raw: List[float] = []
    prev_cdf = 0.0
    for i in range(idx_lo, idx_hi + 1):
        edge_high = price_min + (i + 1) * bin_width
        cdf = _triangular_cdf(min(edge_high, high), low, peak, high)
        raw.append(max(0.0, cdf - prev_cdf))
        prev_cdf = cdf

    total = sum(raw)
    if total <= 0:
        weights[idx_lo] += volume
        return
    for offset, w in enumerate(raw):
        weights[idx_lo + offset] += volume * (w / total)


# ============================================
# 核心构建
# ============================================

def build_chip_profile(
    bars: Sequence[KLineBar],
    *,
    circulating_shares: Optional[float] = None,
    typical_turnover: float = 0.02,
    bins: Optional[int] = None,
    decay_floor: float = 0.0,
) -> Optional[ChipProfile]:
    """由日 K 线序列构建筹码剖面。

    Args:
        bars: 时间升序的 K 线列表（至少 2 根）
        circulating_shares: 流通股本（股）。为空时用 max(volume)/typical_turnover 估算
        typical_turnover: 估算流通股本用的典型日换手率，默认 2%
        bins: 价格轴分箱数量；为空时按价格量级自适应
        decay_floor: 单日衰减后的最低留存系数（0 表示不设下限）

    Returns:
        ChipProfile；数据不足或全部为脏数据时返回 None
    """
    if not bars or len(bars) < 2:
        logger.debug("[本地筹码] K 线不足 2 根，无法计算")
        return None

    price_min = min(b.low for b in bars)
    price_max = max(b.high for b in bars)
    if price_min <= 0 or price_max <= 0:
        logger.debug("[本地筹码] 价格区间非法: [%s, %s]", price_min, price_max)
        return None
    if price_max <= price_min:
        # 全程一口价（长期停牌 / 连续一字板）：以该价为中心对称撑开极小区间，
        # 唯一目的是保证 bin_width > 0、避免除零，并让这团筹码落在价格轴中部
        # 而非贴着边界。
        #
        # 注意：撑开本身**并不能**解决"获利比例误报 0"——撑开后全部筹码仍集中在
        # 单个 bin 内，且该 bin 中心恒高于现价（如 20.0 一口价 → bin 中心 20.005）。
        # 真正把 profit_ratio 从 0.0 救回 1.0 的是 ChipProfile.profit_ratio() 里
        # 放宽半个 bin 的比较规则，两者必须配套存在。
        pad = max(price_min * 0.005, 0.01)
        price_min = max(price_min - pad, 1e-4)
        price_max += pad

    n_bins = _resolve_bin_count(price_min, price_max, bins)
    bin_width = (price_max - price_min) / n_bins
    if bin_width <= 0:
        logger.debug("[本地筹码] bin_width 非法，放弃计算")
        return None

    # 流通股本估算：以样本内最大单日成交量对应 typical_turnover 反推
    max_volume = max((b.volume for b in bars), default=0.0)
    has_volume = max_volume > 0
    shares = circulating_shares if (circulating_shares and circulating_shares > 0) else 0.0
    if shares <= 0 and has_volume:
        turnover_base = typical_turnover if typical_turnover > 0 else 0.02
        shares = max_volume / turnover_base
    if not has_volume:
        # 无量数据（部分源不返回 volume）：退化为每日等权的纯价格分布，不做换手衰减
        logger.debug("[本地筹码] 无有效成交量，退化为等权价格分布")

    floor = min(max(decay_floor, 0.0), 1.0)
    weights: List[float] = [0.0] * n_bins

    for bar in bars:
        # ① 换手率衰减：老筹码按当日换手比例被置换出去
        if has_volume and shares > 0 and bar.volume > 0:
            turnover = min(max(bar.volume / shares, 0.0), MAX_DAILY_TURNOVER)
            retain = max(1.0 - turnover, floor)
            if retain < 1.0:
                for i in range(n_bins):
                    weights[i] *= retain
        # ② 当日新筹码按三角形分布派发（无量数据时每日等权计 1.0）
        _distribute_triangle(
            weights, bar, price_min, bin_width,
            volume=None if has_volume else 1.0,
        )

    total = sum(weights)
    if total <= 0:
        logger.debug("[本地筹码] 累计筹码为 0，无法给出分布")
        return None

    prices = [price_min + (i + 0.5) * bin_width for i in range(n_bins)]
    return ChipProfile(
        price_min=price_min,
        bin_width=bin_width,
        prices=prices,
        weights=weights,
        total=total,
        last_date=bars[-1].date,
        last_close=bars[-1].close,
    )


def compute_chip_distribution(
    code: str,
    klines: Any = None,
    *,
    prices: Optional[Sequence[float]] = None,
    volumes: Optional[Sequence[float]] = None,
    dates: Optional[Sequence[str]] = None,
    current_price: Optional[float] = None,
    circulating_shares: Optional[float] = None,
    typical_turnover: float = 0.02,
    bins: Optional[int] = None,
    decay_floor: float = 0.0,
) -> Optional[ChipDistribution]:
    """本地计算单只标的的筹码分布。

    Args:
        code: 标的代码
        klines: 日 K 线序列（list[dict] 或 pandas.DataFrame），时间升序
        prices: 便捷形态——收盘价序列（与 klines 二选一）
        volumes: 便捷形态——成交量序列（股）
        dates: 便捷形态——日期序列
        current_price: 现价；缺省取最后一根 K 线收盘价
        circulating_shares: 流通股本（股），缺省自动估算
        typical_turnover: 估算流通股本用的典型日换手率
        bins: 价格轴分箱数量，缺省自适应
        decay_floor: 单日衰减最低留存系数

    Returns:
        ChipDistribution（source="local"）；数据不足时返回 None，不抛异常
    """
    try:
        bars = _normalize_bars(klines=klines, prices=prices, volumes=volumes, dates=dates)
        profile = build_chip_profile(
            bars,
            circulating_shares=circulating_shares,
            typical_turnover=typical_turnover,
            bins=bins,
            decay_floor=decay_floor,
        )
        if profile is None:
            return None

        price_now = current_price if current_price and current_price > 0 else profile.last_close
        cost_90_low = profile.quantile(0.05)
        cost_90_high = profile.quantile(0.95)
        cost_70_low = profile.quantile(0.15)
        cost_70_high = profile.quantile(0.85)

        def _concentration(low: float, high: float) -> float:
            """集中度 =(上限-下限)/(上限+下限)，越小越集中。"""
            denominator = high + low
            if denominator <= 0:
                return 0.0
            return max(0.0, (high - low) / denominator)

        return ChipDistribution(
            code=code,
            date=profile.last_date,
            source="local",
            profit_ratio=round(profile.profit_ratio(price_now), 4),
            avg_cost=round(profile.avg_cost(), 4),
            cost_90_low=round(cost_90_low, 4),
            cost_90_high=round(cost_90_high, 4),
            concentration_90=round(_concentration(cost_90_low, cost_90_high), 4),
            cost_70_low=round(cost_70_low, 4),
            cost_70_high=round(cost_70_high, 4),
            concentration_70=round(_concentration(cost_70_low, cost_70_high), 4),
        )
    except Exception as exc:  # 兜底：数据层不应因脏数据抛异常打断主流程
        logger.warning("[本地筹码] %s 计算失败: %s", code, exc, exc_info=True)
        return None


# ============================================
# 离线自测（确定性合成数据，零网络依赖）
# ============================================

def _synth_klines(
    start_price: float,
    end_price: float,
    days: int = 60,
    base_volume: float = 8_000_000.0,
) -> List[Dict[str, Any]]:
    """生成确定性合成 K 线：价格线性漂移 + 正弦波动，量能随波动放大。"""
    records: List[Dict[str, Any]] = []
    for i in range(days):
        drift = start_price + (end_price - start_price) * i / max(days - 1, 1)
        wobble = 0.02 * drift * math.sin(i / 3.0)
        close = round(drift + wobble, 2)
        open_ = round(close - 0.01 * drift * math.sin(i / 5.0), 2)
        high = round(max(open_, close) * 1.012, 2)
        low = round(min(open_, close) * 0.988, 2)
        volume = round(base_volume * (1.0 + 0.35 * math.cos(i / 4.0)), 0)
        records.append({
            "date": f"2024-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    return records


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

    print("=" * 60)
    print("本地筹码分布计算器 · 离线自测（合成数据，无网络依赖）")
    print("=" * 60)

    up_klines = _synth_klines(10.0, 12.0, days=60)
    up_chip = compute_chip_distribution("600000", up_klines)
    print("\n[样本一] 60 日缓涨 10.00 → 12.00")
    print(f"  最新收盘: {up_klines[-1]['close']}")
    print(f"  结果: {up_chip.to_dict() if up_chip else None}")
    if up_chip:
        print(f"  cost_70 区间: [{up_chip.cost_70_low}, {up_chip.cost_70_high}]")
        print(f"  状态解读: {up_chip.get_chip_status(up_klines[-1]['close'])}")

    down_klines = _synth_klines(12.0, 10.0, days=60)
    down_chip = compute_chip_distribution("600001", down_klines)
    print("\n[样本二] 60 日缓跌 12.00 → 10.00")
    print(f"  最新收盘: {down_klines[-1]['close']}")
    print(f"  结果: {down_chip.to_dict() if down_chip else None}")

    flat_prices = [20.0 + 0.1 * math.sin(i / 2.0) for i in range(40)]
    flat_chip = compute_chip_distribution(
        "600002", prices=flat_prices, volumes=[1_000_000.0] * 40
    )
    print("\n[样本三] 40 日横盘 ~20.00（prices/volumes 便捷入参）")
    print(f"  结果: {flat_chip.to_dict() if flat_chip else None}")

    print("\n[边界] 空输入 →", compute_chip_distribution("600003", []))
    print("[边界] 单根 K 线 →", compute_chip_distribution("600004", up_klines[:1]))

    print("\n自测完成。")
