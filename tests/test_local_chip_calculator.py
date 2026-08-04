# -*- coding: utf-8 -*-
"""本地筹码分布计算器（data_provider.local_chip_calculator）单元测试。

覆盖要点：
    - 获利比例方向性（单调涨/跌）
    - 成本区间嵌套关系与集中度取值域
    - 换手率衰减是否真的让近期成本占主导
    - 边界：空输入 / 单根 K 线 / 全零成交量 / 脏数据 / 显式现价
    - 输入形态等价：list[dict] ↔ pandas.DataFrame ↔ prices/volumes
    - 分布数组归一化
    - 零网络依赖（屏蔽 socket 后仍可计算 + 静态 import 白名单）

全部使用确定性合成数据，不触网、不读盘。
"""

from __future__ import annotations

import ast
import bisect
import math
import os
import socket
from typing import Any, Dict, List

import pytest

from data_provider.local_chip_calculator import (
    ChipProfile,
    KLineBar,
    build_chip_profile,
    compute_chip_distribution,
)


# ============================================
# 合成数据工厂（确定性，零随机）
# ============================================

def _linear_klines(
    start: float,
    end: float,
    days: int = 60,
    volume: float = 8_000_000.0,
    amplitude: float = 0.01,
) -> List[Dict[str, Any]]:
    """生成价格线性变化的 K 线序列（时间升序）。"""
    records: List[Dict[str, Any]] = []
    for i in range(days):
        close = start + (end - start) * i / max(days - 1, 1)
        records.append({
            "date": "2024-01-%02d" % (i + 1),
            "open": round(close, 2),
            "high": round(close * (1 + amplitude), 2),
            "low": round(close * (1 - amplitude), 2),
            "close": round(close, 2),
            "volume": volume,
        })
    return records


def _flat_klines(price: float = 20.0, days: int = 30,
                 volume: float = 1_000_000.0) -> List[Dict[str, Any]]:
    """全程一口价（连续一字板 / 长期停牌）。"""
    return [
        {"date": "2024-02-%02d" % (i + 1), "open": price, "high": price,
         "low": price, "close": price, "volume": volume}
        for i in range(days)
    ]


def _bars_from(records: List[Dict[str, Any]]) -> List[KLineBar]:
    """把 dict 记录转成 KLineBar（用于直接测 profile 层）。"""
    return [
        KLineBar(date=r["date"], open=r["open"], high=r["high"],
                 low=r["low"], close=r["close"], volume=r["volume"])
        for r in records
    ]


def _strict_profit_ratio(profile: ChipProfile, price: float) -> float:
    """不放宽半个 bin 的「严格」获利比例，用于对照验证设计决策 1。"""
    idx = bisect.bisect_right(profile.prices, price)
    return sum(profile.weights[:idx]) / profile.total


# ============================================
# 边界：数据不足 / 全脏
# ============================================

def test_empty_klines_returns_none():
    assert compute_chip_distribution("600000", []) is None


def test_no_input_at_all_returns_none():
    assert compute_chip_distribution("600000") is None


def test_single_bar_returns_none():
    assert compute_chip_distribution("600000", _linear_klines(10.0, 12.0)[:1]) is None


def test_all_rows_dirty_returns_none():
    """全部为 high<low 的脏数据时应返回 None 而不是抛异常。"""
    dirty = [{"date": "d%d" % i, "open": 9.0, "high": 9.0, "low": 11.0,
              "close": 10.0, "volume": 1_000_000.0} for i in range(10)]
    assert compute_chip_distribution("600000", dirty) is None


def test_non_positive_prices_return_none():
    rows = [{"date": "d%d" % i, "open": 0, "high": 0, "low": 0,
             "close": 0, "volume": 1_000_000.0} for i in range(10)]
    assert compute_chip_distribution("600000", rows) is None


# ============================================
# 获利比例方向性
# ============================================

def test_uptrend_gives_high_profit_ratio():
    """60 日单调上涨：绝大多数筹码成本低于现价 → profit_ratio 接近 1。"""
    chip = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    assert chip.profit_ratio > 0.9, "单调上涨的获利比例应接近 1，实际 %s" % chip.profit_ratio
    assert chip.profit_ratio <= 1.0


def test_downtrend_gives_low_profit_ratio():
    """60 日单调下跌：绝大多数筹码被套 → profit_ratio 接近 0。"""
    chip = compute_chip_distribution("600001", _linear_klines(20.0, 10.0, days=60))

    assert chip is not None
    assert chip.profit_ratio < 0.1, "单调下跌的获利比例应接近 0，实际 %s" % chip.profit_ratio
    assert chip.profit_ratio >= 0.0


def test_uptrend_profit_ratio_exceeds_downtrend():
    """同一价格区间，涨势的获利比例必须显著高于跌势。"""
    up = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))
    down = compute_chip_distribution("600001", _linear_klines(20.0, 10.0, days=60))

    assert up is not None and down is not None
    assert up.profit_ratio - down.profit_ratio > 0.8


# ============================================
# 成本区间与集中度
# ============================================

def test_cost_bands_are_nested_and_ordered():
    """90% 区间必须包住 70% 区间。"""
    chip = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    assert chip.cost_90_low <= chip.cost_70_low, "cost_90_low 应 <= cost_70_low"
    assert chip.cost_70_low <= chip.cost_70_high, "cost_70_low 应 <= cost_70_high"
    assert chip.cost_70_high <= chip.cost_90_high, "cost_70_high 应 <= cost_90_high"


def test_concentration_values_within_unit_interval():
    chip = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    assert 0.0 <= chip.concentration_90 <= 1.0
    assert 0.0 <= chip.concentration_70 <= 1.0


def test_narrower_band_is_more_concentrated():
    """70% 区间比 90% 区间窄，集中度指标（越小越集中）必须更小。"""
    chip = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    assert chip.concentration_70 <= chip.concentration_90


def test_avg_cost_falls_inside_cost_90_band():
    chip = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    assert chip.cost_90_low <= chip.avg_cost <= chip.cost_90_high


def test_flat_series_is_highly_concentrated():
    """一口价横盘：筹码高度集中，集中度应接近 0。"""
    chip = compute_chip_distribution("600002", _flat_klines(20.0, days=30))

    assert chip is not None
    assert chip.concentration_90 < 0.01


# ============================================
# 换手率衰减（决策：近期筹码应占主导）
# ============================================

def test_high_turnover_pushes_avg_cost_toward_recent_price():
    """同一条上涨路径下，高换手率应让平均成本更贴近近期（更高）价格。"""
    klines = _linear_klines(10.0, 20.0, days=60)

    high_turnover = compute_chip_distribution("600000", klines, typical_turnover=0.20)
    low_turnover = compute_chip_distribution("600000", klines, typical_turnover=0.002)

    assert high_turnover is not None and low_turnover is not None
    assert high_turnover.avg_cost > low_turnover.avg_cost, (
        "高换手率的平均成本应更高（更贴近近期），实际 高=%s 低=%s"
        % (high_turnover.avg_cost, low_turnover.avg_cost)
    )
    # 低换手率≈无衰减，平均成本应接近全区间中位 15；高换手率应明显偏向末端 20
    assert low_turnover.avg_cost < 16.0
    assert high_turnover.avg_cost > 18.0


def test_high_turnover_lowers_avg_cost_on_downtrend():
    """下跌路径下衰减方向相反：高换手率的平均成本应更低。"""
    klines = _linear_klines(20.0, 10.0, days=60)

    high_turnover = compute_chip_distribution("600001", klines, typical_turnover=0.20)
    low_turnover = compute_chip_distribution("600001", klines, typical_turnover=0.002)

    assert high_turnover is not None and low_turnover is not None
    assert high_turnover.avg_cost < low_turnover.avg_cost


def test_smaller_float_shares_means_stronger_decay():
    """显式流通股本越小 → 换手率越高 → 平均成本越贴近近期。"""
    klines = _linear_klines(10.0, 20.0, days=60)

    small = compute_chip_distribution("600000", klines, circulating_shares=2e7)
    large = compute_chip_distribution("600000", klines, circulating_shares=8e10)

    assert small is not None and large is not None
    assert small.avg_cost > large.avg_cost


def test_decay_floor_one_disables_decay():
    """decay_floor=1.0 表示每日全额留存，等价于不做衰减。"""
    klines = _linear_klines(10.0, 20.0, days=60)

    no_decay = compute_chip_distribution(
        "600000", klines, typical_turnover=0.20, decay_floor=1.0
    )
    with_decay = compute_chip_distribution(
        "600000", klines, typical_turnover=0.20, decay_floor=0.0
    )

    assert no_decay is not None and with_decay is not None
    # 不衰减 → 等权 → 平均成本落在路径中点附近
    assert abs(no_decay.avg_cost - 15.0) < 0.1
    assert with_decay.avg_cost > no_decay.avg_cost


# ============================================
# 决策 2：零成交量优雅降级
# ============================================

def test_zero_volume_degrades_gracefully_instead_of_none():
    """全样本 volume=0 时不应返回 None，而是退化为等权价格分布。"""
    klines = [dict(r, volume=0) for r in _linear_klines(10.0, 20.0, days=60)]

    chip = compute_chip_distribution("600000", klines)

    assert chip is not None, "全零成交量应优雅降级而非返回 None"
    assert chip.source == "local"
    assert chip.avg_cost > 0


def test_zero_volume_uses_equal_weight_without_decay():
    """零成交量分支不做衰减 → 平均成本 = 线性路径中点。"""
    klines = [dict(r, volume=0) for r in _linear_klines(10.0, 20.0, days=60)]

    chip = compute_chip_distribution("600000", klines)

    assert chip is not None
    assert abs(chip.avg_cost - 15.0) < 0.1, (
        "等权无衰减时平均成本应为路径中点 15，实际 %s" % chip.avg_cost
    )


def test_missing_volume_field_still_computes():
    """完全不提供 volume 字段时同样降级可用。"""
    klines = [
        {"date": "d%d" % i, "open": 10 + 0.1 * i, "high": 10 + 0.1 * i + 0.05,
         "low": 10 + 0.1 * i - 0.05, "close": 10 + 0.1 * i}
        for i in range(30)
    ]

    chip = compute_chip_distribution("600000", klines)

    assert chip is not None
    assert chip.avg_cost > 0


# ============================================
# 决策 3 + 决策 1：一口价 / 单点退化
# ============================================

def test_flat_price_series_does_not_crash():
    """全程一口价：对称撑开极小区间后仍能给出有效分布。"""
    chip = compute_chip_distribution("600002", _flat_klines(20.0, days=30))

    assert chip is not None
    assert chip.cost_90_low > 0
    assert chip.cost_90_high >= chip.cost_90_low
    assert abs(chip.avg_cost - 20.0) < 0.05


def test_half_bin_rule_rescues_degenerate_single_point():
    """决策 1 验证：筹码退化为单点时，严格比较会误报 0，放宽半个 bin 后不再误报。"""
    profile = build_chip_profile(_bars_from(_flat_klines(20.0, days=30)))

    assert profile is not None
    strict = _strict_profit_ratio(profile, 20.0)
    relaxed = profile.profit_ratio(20.0)

    assert strict == 0.0, "对照组：严格比较在单点退化场景确实误报为 0"
    assert relaxed > 0.9, "放宽半个 bin 后应正确识别为获利盘，实际 %s" % relaxed


def test_half_bin_relaxation_overcounts_at_most_one_bin():
    """决策 1 的副作用必须有界：最多多算「现价所在的那一个 bin」。"""
    profile = build_chip_profile(_bars_from(_linear_klines(10.0, 20.0, days=60)))

    assert profile is not None
    price = profile.last_close
    strict = _strict_profit_ratio(profile, price)
    relaxed = profile.profit_ratio(price)
    max_single_bin_ratio = max(profile.weights) / profile.total

    assert relaxed >= strict
    assert relaxed - strict <= max_single_bin_ratio + 1e-9, (
        "放宽幅度超过一个 bin 的权重：delta=%s, max_bin=%s"
        % (relaxed - strict, max_single_bin_ratio)
    )
    # 真实数据上该偏差应远小于 1 个百分点
    assert relaxed - strict < 0.01


def test_profit_ratio_is_monotonic_in_price():
    """现价越高，获利比例只能不降。"""
    profile = build_chip_profile(_bars_from(_linear_klines(10.0, 20.0, days=60)))

    assert profile is not None
    ratios = [profile.profit_ratio(p) for p in (9.0, 12.0, 15.0, 18.0, 25.0)]

    assert ratios == sorted(ratios)
    assert ratios[0] == 0.0
    assert abs(ratios[-1] - 1.0) < 1e-9


# ============================================
# 脏数据健壮性
# ============================================

def test_dirty_rows_are_skipped_without_crash():
    """high<low / 全零 / 非法字符串行应被跳过，其余数据照常参与计算。"""
    clean = _linear_klines(10.0, 20.0, days=60)
    dirty = list(clean)
    dirty.insert(10, {"date": "bad-1", "open": 9.0, "high": 9.0, "low": 11.0,
                      "close": 10.0, "volume": 1_000_000.0})
    dirty.insert(20, {"date": "bad-2", "open": 0, "high": 0, "low": 0,
                      "close": 0, "volume": 1_000_000.0})
    dirty.insert(30, {"date": "bad-3", "open": None, "high": "--", "low": "",
                      "close": None, "volume": "x"})

    polluted = compute_chip_distribution("600000", dirty)
    baseline = compute_chip_distribution("600000", clean)

    assert polluted is not None
    assert baseline is not None
    assert polluted.avg_cost == baseline.avg_cost, "脏行未被干净剔除"


def test_nan_values_do_not_break_computation():
    klines = _linear_klines(10.0, 20.0, days=30)
    klines[5]["volume"] = float("nan")
    klines[6]["close"] = float("nan")

    chip = compute_chip_distribution("600000", klines)

    assert chip is not None
    assert not math.isnan(chip.avg_cost)
    assert not math.isnan(chip.profit_ratio)


def test_close_outside_high_low_is_clamped():
    """收盘价越界时夹回 [low, high]，不应把三角峰值顶到区间外。"""
    klines = _linear_klines(10.0, 20.0, days=30)
    klines[3]["close"] = 999.0

    chip = compute_chip_distribution("600000", klines)

    assert chip is not None
    assert chip.cost_90_high < 25.0, "越界收盘价未被夹回，成本区间被污染"


def test_non_dict_rows_are_ignored():
    klines: List[Any] = list(_linear_klines(10.0, 20.0, days=30))
    klines.insert(5, "not-a-record")
    klines.insert(9, None)

    chip = compute_chip_distribution("600000", klines)

    assert chip is not None


# ============================================
# 现价来源
# ============================================

def test_explicit_current_price_takes_effect():
    klines = _linear_klines(10.0, 20.0, days=60)

    below = compute_chip_distribution("600000", klines, current_price=9.0)
    above = compute_chip_distribution("600000", klines, current_price=99.0)

    assert below is not None and above is not None
    assert below.profit_ratio == 0.0, "现价低于全部成本时应为 0"
    assert above.profit_ratio == 1.0, "现价高于全部成本时应为 1"


def test_current_price_defaults_to_last_close():
    klines = _linear_klines(10.0, 20.0, days=60)

    default = compute_chip_distribution("600000", klines)
    explicit = compute_chip_distribution("600000", klines,
                                         current_price=klines[-1]["close"])

    assert default is not None and explicit is not None
    assert default.profit_ratio == explicit.profit_ratio


def test_invalid_current_price_falls_back_to_last_close():
    klines = _linear_klines(10.0, 20.0, days=60)

    default = compute_chip_distribution("600000", klines)
    zero_price = compute_chip_distribution("600000", klines, current_price=0)

    assert default is not None and zero_price is not None
    assert zero_price.profit_ratio == default.profit_ratio


# ============================================
# 输入形态等价
# ============================================

def test_list_and_dataframe_inputs_are_equivalent():
    pd = pytest.importorskip("pandas")
    records = _linear_klines(10.0, 20.0, days=60)

    from_list = compute_chip_distribution("600000", records)
    from_frame = compute_chip_distribution("600000", pd.DataFrame(records))

    assert from_list is not None and from_frame is not None
    assert from_frame.avg_cost == from_list.avg_cost
    assert from_frame.to_dict() == from_list.to_dict()


def test_chinese_column_aliases_are_supported():
    records = _linear_klines(10.0, 20.0, days=30)
    cn_records = [
        {"日期": r["date"], "开盘": r["open"], "最高": r["high"],
         "最低": r["low"], "收盘": r["close"], "成交量": r["volume"]}
        for r in records
    ]

    en_chip = compute_chip_distribution("600000", records)
    cn_chip = compute_chip_distribution("600000", cn_records)

    assert en_chip is not None and cn_chip is not None
    assert cn_chip.avg_cost == en_chip.avg_cost


def test_vol_alias_is_supported():
    records = _linear_klines(10.0, 20.0, days=30)
    aliased = [
        {"date": r["date"], "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "vol": r["volume"]}
        for r in records
    ]

    assert compute_chip_distribution("600000", aliased) is not None


def test_prices_volumes_convenience_input():
    prices = [10.0 + 0.1 * i for i in range(40)]

    chip = compute_chip_distribution("600000", prices=prices,
                                     volumes=[1_000_000.0] * 40)

    assert chip is not None
    assert chip.source == "local"
    assert 10.0 <= chip.avg_cost <= 14.0


def test_prices_without_volumes_uses_equal_weight():
    prices = [10.0 + 0.1 * i for i in range(40)]

    chip = compute_chip_distribution("600000", prices=prices)

    assert chip is not None
    assert chip.avg_cost > 0


# ============================================
# ChipProfile / 分布数组
# ============================================

def test_distribution_array_ratios_sum_to_one():
    profile = build_chip_profile(_bars_from(_linear_klines(10.0, 20.0, days=60)))

    assert profile is not None
    array = profile.get_distribution_array()

    assert len(array) == len(profile.prices)
    assert abs(sum(ratio for _, ratio in array) - 1.0) < 1e-9
    assert all(ratio >= 0.0 for _, ratio in array)


def test_distribution_array_prices_are_ascending():
    profile = build_chip_profile(_bars_from(_linear_klines(10.0, 20.0, days=60)))

    assert profile is not None
    prices = [price for price, _ in profile.get_distribution_array()]

    assert prices == sorted(prices)


def test_empty_profile_returns_empty_distribution_array():
    empty = ChipProfile()

    assert empty.get_distribution_array() == []
    assert empty.avg_cost() == 0.0
    assert empty.profit_ratio(10.0) == 0.0
    assert empty.quantile(0.5) == 0.0


def test_quantile_is_monotonic():
    profile = build_chip_profile(_bars_from(_linear_klines(10.0, 20.0, days=60)))

    assert profile is not None
    values = [profile.quantile(q) for q in (0.05, 0.15, 0.5, 0.85, 0.95)]

    assert values == sorted(values)


def test_build_chip_profile_requires_two_bars():
    assert build_chip_profile([]) is None
    assert build_chip_profile(_bars_from(_linear_klines(10.0, 20.0, days=60))[:1]) is None


def test_bins_are_clamped_to_safe_range():
    bars = _bars_from(_linear_klines(10.0, 20.0, days=60))

    too_few = build_chip_profile(bars, bins=5)
    too_many = build_chip_profile(bars, bins=99999)

    assert too_few is not None and too_many is not None
    assert len(too_few.prices) == 20, "bins 下限应被夹到 MIN_BINS=20"
    assert len(too_many.prices) == 2000, "bins 上限应被夹到 MAX_BINS=2000"


# ============================================
# 契约字段
# ============================================

def test_result_carries_local_source_and_last_date():
    klines = _linear_klines(10.0, 20.0, days=60)

    chip = compute_chip_distribution("600519", klines)

    assert chip is not None
    assert chip.code == "600519"
    assert chip.source == "local"
    assert chip.date == klines[-1]["date"]


def test_result_is_json_serializable_dict():
    chip = compute_chip_distribution("600519", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    payload = chip.to_dict()

    assert payload["source"] == "local"
    for key in ("profit_ratio", "avg_cost", "cost_90_low", "cost_90_high"):
        assert isinstance(payload[key], float)


def test_values_are_rounded_to_four_decimals():
    chip = compute_chip_distribution("600519", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    for value in (chip.profit_ratio, chip.avg_cost, chip.cost_90_low,
                  chip.cost_90_high, chip.concentration_90, chip.concentration_70):
        assert round(value, 4) == value


# ============================================
# 零网络依赖
# ============================================

def test_computation_works_with_sockets_blocked(monkeypatch):
    """屏蔽 socket 后计算仍成功 → 证明运行期零网络访问。"""

    def _blocked(*args, **kwargs):
        raise AssertionError("本地筹码计算不应发起任何网络请求")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    chip = compute_chip_distribution("600000", _linear_klines(10.0, 20.0, days=60))

    assert chip is not None
    assert chip.source == "local"


def test_module_imports_only_stdlib_and_local_contract():
    """静态校验：模块不得 import 任何网络/重依赖库。"""
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data_provider", "local_chip_calculator.py",
    )
    with open(module_path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    allowed = {
        "__future__", "bisect", "logging", "math", "dataclasses",
        "typing", "os", "sys", "data_provider",
    }
    assert imported <= allowed, "出现非白名单依赖: %s" % sorted(imported - allowed)

    forbidden = {"akshare", "requests", "urllib", "urllib3", "httpx",
                 "tushare", "efinance", "pandas", "numpy", "socket"}
    assert not (imported & forbidden), "出现网络/重依赖: %s" % sorted(imported & forbidden)
