# -*- coding: utf-8 -*-
"""
===================================
实时行情统一类型定义 & 熔断机制
===================================

设计目标：
1. 统一各数据源的实时行情返回结构
2. 实现熔断/冷却机制，避免连续失败时反复请求
3. 支持多数据源故障切换

使用方式：
- 所有 Fetcher 的 get_realtime_quote() 统一返回 UnifiedRealtimeQuote
- CircuitBreaker 管理各数据源的熔断状态
"""

import logging
import time
from threading import Lock, RLock
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Union
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================
# 通用类型转换工具函数
# ============================================
# 设计说明：
# 各数据源返回的原始数据类型不一致（str/float/int/NaN），
# 使用这些函数统一转换，避免在各 Fetcher 中重复定义。

def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    """
    安全转换为浮点数
    
    处理场景：
    - None / 空字符串 → default
    - pandas NaN / numpy NaN → default
    - 数值字符串 → float
    - 已是数值 → float
    
    Args:
        val: 待转换的值
        default: 转换失败时的默认值
        
    Returns:
        转换后的浮点数，或默认值
    """
    try:
        if val is None:
            return default
        
        # 处理字符串
        if isinstance(val, str):
            val = val.strip()
            if val == "" or val == "-" or val == "--":
                return default
        
        # 处理 pandas/numpy NaN
        # 使用 math.isnan 而不是 pd.isna，避免强制依赖 pandas
        import math
        try:
            if math.isnan(float(val)):
                return default
        except (ValueError, TypeError):
            pass
        
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    """
    安全转换为整数
    
    先转换为 float，再取整，处理 "123.0" 这类情况
    
    Args:
        val: 待转换的值
        default: 转换失败时的默认值
        
    Returns:
        转换后的整数，或默认值
    """
    f_val = safe_float(val, default=None)
    if f_val is not None:
        return int(f_val)
    return default


class RealtimeSource(Enum):
    """实时行情数据源"""
    EFINANCE = "efinance"           # 东方财富（efinance库）
    AKSHARE_EM = "akshare_em"       # 东方财富（akshare库）
    AKSHARE_SINA = "akshare_sina"   # 新浪财经
    AKSHARE_QQ = "akshare_qq"       # 腾讯财经
    TUSHARE = "tushare"             # Tushare Pro
    TICKFLOW = "tickflow"           # TickFlow
    TENCENT = "tencent"             # 腾讯直连
    SINA = "sina"                   # 新浪直连
    STOOQ = "stooq"                 # Stooq 美股兜底
    LONGBRIDGE = "longbridge"       # 长桥（美股/港股兜底）
    FALLBACK = "fallback"           # 降级兜底


@dataclass
class UnifiedRealtimeQuote:
    """
    统一实时行情数据结构
    
    设计原则：
    - 各数据源返回的字段可能不同，缺失字段用 None 表示
    - 主流程使用 getattr(quote, field, None) 获取，保证兼容性
    - source 字段标记数据来源，便于调试
    """
    code: str
    name: str = ""
    source: RealtimeSource = RealtimeSource.FALLBACK

    # === 数据质量元数据（由 DataFetcherManager 统一补齐）===
    fetched_at: Optional[str] = None             # 本系统获取时间（ISO 8601 datetime）
    provider_timestamp: Optional[str] = None     # Provider 真实行情时间（ISO 8601 datetime）
    is_stale: Optional[bool] = None              # provider_timestamp 超过最小 TTL 阈值时为 True
    stale_seconds: Optional[int] = None          # provider_timestamp 距 fetched_at 的秒数
    fallback_from: Optional[str] = None          # 整源 fallback 的失败首选源 token
    market: Optional[str] = None                 # 市场标签（cn/hk/us/jp/kr/tw）
    currency: Optional[str] = None               # 报价币种（JPY/KRW/TWD/USD/HKD/CNY 等）
    data_quality: Optional[str] = None           # ok/partial/unavailable
    missing_fields: Optional[list[str]] = None   # provider 缺失的关键字段
    
    # === 核心价格数据（几乎所有源都有）===
    price: Optional[float] = None           # 最新价
    change_pct: Optional[float] = None      # 涨跌幅(%)
    change_amount: Optional[float] = None   # 涨跌额
    
    # === 量价指标（部分源可能缺失）===
    volume: Optional[int] = None            # 成交量（股，与历史日线口径一致）
    amount: Optional[float] = None          # 成交额（元）
    volume_ratio: Optional[float] = None    # 量比
    turnover_rate: Optional[float] = None   # 换手率(%)
    amplitude: Optional[float] = None       # 振幅(%)
    
    # === 价格区间 ===
    open_price: Optional[float] = None      # 开盘价
    high: Optional[float] = None            # 最高价
    low: Optional[float] = None             # 最低价
    pre_close: Optional[float] = None       # 昨收价
    
    # === 估值指标（仅东财等全量接口有）===
    pe_ratio: Optional[float] = None        # 市盈率(动态)
    pb_ratio: Optional[float] = None        # 市净率
    total_mv: Optional[float] = None        # 总市值(元)
    circ_mv: Optional[float] = None         # 流通市值(元)
    
    # === 其他指标 ===
    change_60d: Optional[float] = None      # 60日涨跌幅(%)
    high_52w: Optional[float] = None        # 52周最高
    low_52w: Optional[float] = None         # 52周最低
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（过滤 None 值）"""
        result = {
            'code': self.code,
            'name': self.name,
            'source': self.source.value,
        }
        # 只添加非 None 的字段
        optional_fields = [
            'fetched_at', 'provider_timestamp', 'is_stale', 'stale_seconds',
            'fallback_from', 'market', 'currency', 'data_quality', 'missing_fields',
            'price', 'change_pct', 'change_amount', 'volume', 'amount',
            'volume_ratio', 'turnover_rate', 'amplitude',
            'open_price', 'high', 'low', 'pre_close',
            'pe_ratio', 'pb_ratio', 'total_mv', 'circ_mv',
            'change_60d', 'high_52w', 'low_52w'
        ]
        for f in optional_fields:
            val = getattr(self, f, None)
            if val is not None:
                result[f] = val
        return result
    
    def has_basic_data(self) -> bool:
        """检查是否有基本的价格数据"""
        return self.price is not None and self.price > 0
    
    def has_volume_data(self) -> bool:
        """检查是否有量价数据"""
        return self.volume_ratio is not None or self.turnover_rate is not None


@dataclass
class ChipDistribution:
    """
    筹码分布数据
    
    反映持仓成本分布和获利情况
    """
    code: str
    date: str = ""
    source: str = "akshare"
    
    # 获利情况
    profit_ratio: float = 0.0     # 获利比例(0-1)
    avg_cost: float = 0.0         # 平均成本
    
    # 筹码集中度
    cost_90_low: float = 0.0      # 90%筹码成本下限
    cost_90_high: float = 0.0     # 90%筹码成本上限
    concentration_90: float = 0.0  # 90%筹码集中度（越小越集中）
    
    cost_70_low: float = 0.0      # 70%筹码成本下限
    cost_70_high: float = 0.0     # 70%筹码成本上限
    concentration_70: float = 0.0  # 70%筹码集中度
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'source': self.source,
            'profit_ratio': self.profit_ratio,
            'avg_cost': self.avg_cost,
            'cost_90_low': self.cost_90_low,
            'cost_90_high': self.cost_90_high,
            'concentration_90': self.concentration_90,
            'concentration_70': self.concentration_70,
        }
    
    def get_chip_status(self, current_price: float) -> str:
        """
        获取筹码状态描述
        
        Args:
            current_price: 当前股价
            
        Returns:
            筹码状态描述
        """
        status_parts = []
        
        # 获利比例分析
        if self.profit_ratio >= 0.9:
            status_parts.append("获利盘极高(获利盘>90%)")
        elif self.profit_ratio >= 0.7:
            status_parts.append("获利盘较高(获利盘70-90%)")
        elif self.profit_ratio >= 0.5:
            status_parts.append("获利盘中等(获利盘50-70%)")
        elif self.profit_ratio >= 0.3:
            status_parts.append("套牢盘中等(套牢盘50-70%)")
        elif self.profit_ratio >= 0.1:
            status_parts.append("套牢盘较高(套牢盘70-90%)")
        else:
            status_parts.append("套牢盘极高(套牢盘>90%)")
        
        # 筹码集中度分析 (90%集中度 < 10% 表示集中)
        if self.concentration_90 < 0.08:
            status_parts.append("筹码高度集中")
        elif self.concentration_90 < 0.15:
            status_parts.append("筹码较集中")
        elif self.concentration_90 < 0.25:
            status_parts.append("筹码分散度中等")
        else:
            status_parts.append("筹码较分散")
        
        # 成本与现价关系
        if current_price > 0 and self.avg_cost > 0:
            cost_diff = (current_price - self.avg_cost) / self.avg_cost * 100
            if cost_diff > 20:
                status_parts.append(f"现价高于平均成本{cost_diff:.1f}%")
            elif cost_diff > 5:
                status_parts.append(f"现价略高于成本{cost_diff:.1f}%")
            elif cost_diff > -5:
                status_parts.append("现价接近平均成本")
            else:
                status_parts.append(f"现价低于平均成本{abs(cost_diff):.1f}%")
        
        return "，".join(status_parts)


# ============================================
# P1.6 筹码原因码跨层契约
# ============================================
# 设计背景（docs/p1.6-arch-design.md §3.2）：
#   旧接口 get_chip_distribution() 只返回 Optional[ChipDistribution]，
#   "ETF 本就无筹码" 与 "接口挂了" 在 None 处无法区分，信息在数据层被丢弃。
#   本节引入 ChipFetchResult 原因码回传，采用「并列新增 _ex 入口 + 旧签名降为 shim」
#   的零破坏迁移方案，既有 6 处生产调用与 8 处测试引用无需改动。

class ChipUnavailableReason(str, Enum):
    """筹码不可用原因码。

    继承 ``str`` 使其可直接 JSON 序列化 / 写入 metadata，
    跨层传递时统一使用 ``.value``（禁止裸字符串构造）。
    """

    OK = "ok"                          # 成功取到有效筹码数据
    NOT_APPLICABLE = "not_applicable"  # 标的类型本就无筹码（ETF/指数/港股/美股）
    DISABLED = "disabled"              # config.enable_chip_distribution=False 或整源关闭
    NO_CREDENTIAL = "no_credential"    # 无凭证（如 tushare 缺 TUSHARE_TOKEN）
    CIRCUIT_OPEN = "circuit_open"      # 熔断中，本轮跳过
    EMPTY = "empty"                    # 接口通但返回空 / 字段不完整
    FETCH_FAILED = "fetch_failed"      # 网络异常 / HTTP 错误 / 解析异常

    @property
    def severity(self) -> int:
        """聚合排序用严重度。数值越大越"像故障"。

        多源全部失败时，取 severity 最大者作为最终原因码
        （见 docs/p1.6-arch-design.md §7.3 唯一真源表）。
        """
        return _CHIP_REASON_SEVERITY[self.value]

    def to_context_status(self) -> str:
        """映射到既有 ContextFieldStatus 取值（不造新枚举）。

        Returns:
            ``available`` / ``not_supported`` / ``fetch_failed`` / ``missing``
        """
        return _CHIP_REASON_TO_CONTEXT_STATUS.get(self.value, "missing")


# 严重度表与状态映射表抽为模块级常量，避免每次属性访问重建字典
_CHIP_REASON_SEVERITY: Dict[str, int] = {
    "ok": -1,
    "disabled": -1,
    "not_applicable": 0,
    "circuit_open": 1,
    "no_credential": 2,
    "empty": 3,
    "fetch_failed": 4,
}

_CHIP_REASON_TO_CONTEXT_STATUS: Dict[str, str] = {
    "ok": "available",
    "not_applicable": "not_supported",
    "fetch_failed": "fetch_failed",
    # disabled / empty / no_credential / circuit_open 一律落 missing
}


@dataclass
class ChipFetchResult:
    """筹码抓取结果对象（Result Object 模式）。

    取代裸 ``Optional[ChipDistribution]``，把"为什么没有数据"这一信息
    从数据层完整传递到渲染层。
    """

    chip: Optional[ChipDistribution] = None
    reason: ChipUnavailableReason = ChipUnavailableReason.FETCH_FAILED
    detail: str = ""       # 简短诊断串，如 "ConnectionError: RemoteDisconnected"
    source: str = ""       # 命中或最后尝试的 fetcher 名
    latency_ms: int = 0
    error_type: str = ""   # 原始异常类名（如 "RuntimeError"），用于兼容 run_diagnostics 的 error_type 字段

    @property
    def ok(self) -> bool:
        """是否成功取到筹码数据。"""
        return self.chip is not None and self.reason is ChipUnavailableReason.OK

    @property
    def is_not_applicable(self) -> bool:
        """是否属于「标的类型本就无筹码」。"""
        return self.reason is ChipUnavailableReason.NOT_APPLICABLE

    @property
    def should_record_failure(self) -> bool:
        """熔断分流关键：仅真故障计入熔断，避免 ETF 把 akshare 筹码熔掉。"""
        return self.reason is ChipUnavailableReason.FETCH_FAILED

    @property
    def should_record_inconclusive(self) -> bool:
        """空数据释放 HALF_OPEN 探测名额，但不累加失败次数。"""
        return self.reason is ChipUnavailableReason.EMPTY

    @property
    def should_record_provider_run(self) -> bool:
        """NOT_APPLICABLE / NO_CREDENTIAL 不计入失败率（PRD US-3）。

        另外 DISABLED / CIRCUIT_OPEN 本就未真正发起请求，同样不计。
        """
        return self.reason not in (
            ChipUnavailableReason.NOT_APPLICABLE,
            ChipUnavailableReason.NO_CREDENTIAL,
            ChipUnavailableReason.DISABLED,
            ChipUnavailableReason.CIRCUIT_OPEN,
        )

    def to_context_status(self) -> str:
        """映射到 ContextFieldStatus 取值字符串。"""
        return self.reason.to_context_status()

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的诊断字典（不含 chip 本体）。"""
        return {
            'reason': self.reason.value,
            'detail': self.detail,
            'source': self.source,
            'latency_ms': self.latency_ms,
            'error_type': self.error_type,
            'ok': self.ok,
        }

    # ── 工厂方法（优先使用，避免手搓 reason）──
    @classmethod
    def ok_of(
        cls,
        chip: ChipDistribution,
        source: str,
        latency_ms: int = 0,
    ) -> "ChipFetchResult":
        """构造成功结果。"""
        return cls(
            chip=chip,
            reason=ChipUnavailableReason.OK,
            detail="",
            source=source,
            latency_ms=latency_ms,
        )

    @classmethod
    def na(cls, detail: str, source: str, latency_ms: int = 0) -> "ChipFetchResult":
        """构造「标的类型不适用」结果。"""
        return cls(
            chip=None,
            reason=ChipUnavailableReason.NOT_APPLICABLE,
            detail=detail,
            source=source,
            latency_ms=latency_ms,
        )

    @classmethod
    def empty(cls, detail: str, source: str, latency_ms: int = 0) -> "ChipFetchResult":
        """构造「接口通但返回空」结果。"""
        return cls(
            chip=None,
            reason=ChipUnavailableReason.EMPTY,
            detail=detail,
            source=source,
            latency_ms=latency_ms,
        )

    @classmethod
    def failed(
        cls,
        detail: str,
        source: str,
        latency_ms: int = 0,
        error_type: str = "",
    ) -> "ChipFetchResult":
        """构造「抓取失败」结果。

        Args:
            detail: 诊断串（通常含 ``异常类名: 原因``）
            source: fetcher 名
            latency_ms: 耗时
            error_type: 原始异常类名（如 "RuntimeError"），用于兼容 run_diagnostics
                的 error_type 字段；为空时上层回退到 ``reason.value``
        """
        return cls(
            chip=None,
            reason=ChipUnavailableReason.FETCH_FAILED,
            detail=detail,
            source=source,
            latency_ms=latency_ms,
            error_type=error_type,
        )

    @classmethod
    def no_credential(cls, source: str, detail: str = "") -> "ChipFetchResult":
        """构造「无凭证」结果（静默跳过，不计失败率、不熔断）。"""
        return cls(
            chip=None,
            reason=ChipUnavailableReason.NO_CREDENTIAL,
            detail=detail or "缺少数据源凭证，静默跳过",
            source=source,
        )

    @classmethod
    def disabled(cls, source: str, detail: str = "") -> "ChipFetchResult":
        """构造「功能关闭」结果。"""
        return cls(
            chip=None,
            reason=ChipUnavailableReason.DISABLED,
            detail=detail or "筹码分布功能已关闭",
            source=source,
        )

    @classmethod
    def circuit_open(cls, source: str, detail: str = "") -> "ChipFetchResult":
        """构造「熔断中」结果。"""
        return cls(
            chip=None,
            reason=ChipUnavailableReason.CIRCUIT_OPEN,
            detail=detail or "数据源处于熔断状态",
            source=source,
        )


@dataclass(frozen=True)
class ChipFetchPolicy:
    """筹码抓取策略（Strategy-as-Data）。

    CI 实证结论（docs/p1.6-ci-probe-guide.md 三分叉判读）的唯一落地载体：
    三种实证结果 = 三组取值，代码路径唯一，不产生代码分叉。

    - 分叉 A（CI 可用）：全部默认值，零改动
    - 分叉 B（反爬/请求头）：force_user_agent=True + referer + max_retries=2
    - 分叉 C（接口变更/封禁）：disable_akshare_chip=True + allow_tushare_fallback=True
    """

    force_user_agent: bool = False          # 是否真正注入 UA 到 requests session
    referer: str = ""                       # 注入的 Referer 头
    max_retries: int = 0                    # 失败重试次数（0 表示不重试）
    retry_backoff_seconds: float = 1.5      # 指数退避基数
    request_timeout_seconds: float = 10.0   # 单次请求超时（预留）
    allow_tushare_fallback: bool = True     # 是否允许 tushare 兜底（有 token 才真正生效）
    disable_akshare_chip: bool = False      # 确认 akshare 筹码接口已死时整源关闭

    @classmethod
    def from_config(cls, config: Any = None) -> "ChipFetchPolicy":
        """从 Config 读取策略取值。

        本期（P1.6）筹码侧刻意不新增 config 字段（YAGNI，见架构设计 §3.8），
        因此这里对每个字段做 ``getattr`` 软读取：config 上没有对应属性时
        直接落回类默认值。待 CI 实证落到分叉 B/C 时再按需外化配置项，
        届时本方法无需改结构、只需字段生效。

        Args:
            config: Config 实例，可为 None

        Returns:
            ChipFetchPolicy 实例（永不抛异常）
        """
        if config is None:
            return cls()

        def _get(name: str, default: Any) -> Any:
            value = getattr(config, name, None)
            return default if value is None else value

        try:
            return cls(
                force_user_agent=bool(_get('chip_force_user_agent', cls.force_user_agent)),
                referer=str(_get('chip_referer', cls.referer)),
                max_retries=max(0, int(_get('chip_max_retries', cls.max_retries))),
                retry_backoff_seconds=float(
                    _get('chip_retry_backoff_seconds', cls.retry_backoff_seconds)
                ),
                request_timeout_seconds=float(
                    _get('chip_request_timeout_seconds', cls.request_timeout_seconds)
                ),
                allow_tushare_fallback=bool(
                    _get('chip_allow_tushare_fallback', cls.allow_tushare_fallback)
                ),
                disable_akshare_chip=bool(
                    _get('chip_disable_akshare', cls.disable_akshare_chip)
                ),
            )
        except (TypeError, ValueError) as exc:
            logger.debug("[筹码] ChipFetchPolicy 解析配置失败，回落默认值: %s", exc)
            return cls()


class ChipDiagnostics:
    """筹码链路诊断计数器（线程安全）。

    ``max_workers=5`` 并行下必须用锁保护计数；WebUI 长驻进程需在每轮
    run 开始时调用 ``reset()``，避免跨 run 累加。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self.attempted: int = 0
        self.succeeded: int = 0
        self.not_applicable: int = 0
        self.failed: int = 0
        self.empty: int = 0

    def record(self, result: ChipFetchResult) -> None:
        """记录一次筹码抓取的最终聚合结果。

        Args:
            result: FetcherManager 聚合后的 ChipFetchResult
        """
        if result is None:
            return
        with self._lock:
            self.attempted += 1
            if result.ok:
                self.succeeded += 1
            elif result.reason is ChipUnavailableReason.NOT_APPLICABLE:
                self.not_applicable += 1
            elif result.reason is ChipUnavailableReason.EMPTY:
                self.empty += 1
            elif result.reason is ChipUnavailableReason.FETCH_FAILED:
                self.failed += 1
            # DISABLED / NO_CREDENTIAL / CIRCUIT_OPEN 只累加 attempted，不归入任何故障桶

    def snapshot(self) -> Dict[str, int]:
        """返回当前计数快照（线程安全）。"""
        with self._lock:
            return {
                'attempted': self.attempted,
                'succeeded': self.succeeded,
                'not_applicable': self.not_applicable,
                'failed': self.failed,
                'empty': self.empty,
            }

    def summary_line(self) -> str:
        """生成可 grep 的汇总诊断行（格式见架构设计 §3.9）。"""
        snap = self.snapshot()
        return (
            f"[筹码汇总] 尝试 {snap['attempted']} 只 / 成功 {snap['succeeded']} "
            f"/ 不适用 {snap['not_applicable']} / 失败 {snap['failed']} "
            f"/ 空数据 {snap['empty']}"
        )

    def reset(self) -> None:
        """重置全部计数（每轮 run 开始时调用）。"""
        with self._lock:
            self.attempted = 0
            self.succeeded = 0
            self.not_applicable = 0
            self.failed = 0
            self.empty = 0


class CircuitBreaker:
    """
    熔断器 - 管理数据源的熔断/冷却状态
    
    策略：
    - 连续失败 N 次后进入熔断状态
    - 熔断期间跳过该数据源
    - 冷却时间后自动恢复半开状态
    - 半开状态下单次成功则完全恢复，失败则继续熔断
    
    状态机：
    CLOSED（正常） --失败N次--> OPEN（熔断）--冷却时间到--> HALF_OPEN（半开）
    HALF_OPEN --成功--> CLOSED
    HALF_OPEN --失败--> OPEN
    """
    
    # 状态常量
    CLOSED = "closed"      # 正常状态
    OPEN = "open"          # 熔断状态（不可用）
    HALF_OPEN = "half_open"  # 半开状态（试探性请求）
    
    def __init__(
        self,
        failure_threshold: int = 3,       # 连续失败次数阈值
        cooldown_seconds: float = 300.0,  # 冷却时间（秒），默认5分钟
        half_open_max_calls: int = 1      # 半开状态最大尝试次数
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls
        
        # 各数据源状态 {source_name: {state, failures, last_failure_time, half_open_calls}}
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()
    
    def _get_state_locked(self, source: str) -> Dict[str, Any]:
        """获取或初始化数据源状态（调用方需持有锁）。"""
        if source not in self._states:
            self._states[source] = {
                'state': self.CLOSED,
                'failures': 0,
                'last_failure_time': 0.0,
                'half_open_calls': 0
            }
        return self._states[source]
    
    def is_available(self, source: str) -> bool:
        """
        检查数据源是否可用
        
        返回 True 表示可以尝试请求
        返回 False 表示应跳过该数据源
        """
        with self._lock:
            state = self._get_state_locked(source)
            current_time = time.time()

            if state['state'] == self.CLOSED:
                return True

            if state['state'] == self.OPEN:
                # 检查冷却时间
                time_since_failure = current_time - state['last_failure_time']
                if time_since_failure >= self.cooldown_seconds:
                    # 冷却完成，进入半开状态（不预占名额，由 HALF_OPEN 分支统一管理）
                    state['state'] = self.HALF_OPEN
                    state['half_open_calls'] = 0
                    state['last_failure_time'] = current_time
                    logger.info(f"[熔断器] {source} 冷却完成，进入半开状态")
                    # Fall through to HALF_OPEN check below
                else:
                    remaining = self.cooldown_seconds - time_since_failure
                    logger.debug(f"[熔断器] {source} 处于熔断状态，剩余冷却时间: {remaining:.0f}s")
                    return False

            if state['state'] == self.HALF_OPEN:
                if state['half_open_calls'] < self.half_open_max_calls:
                    state['half_open_calls'] += 1
                    return True
                # 所有探测名额已用完；若冷却时间再次到期仍未收到
                # record_success/record_failure 回调，重置名额允许重新探测，
                # 避免永久卡在 HALF_OPEN。
                time_since_failure = current_time - state['last_failure_time']
                if time_since_failure >= self.cooldown_seconds:
                    state['half_open_calls'] = 1
                    state['last_failure_time'] = current_time
                    logger.info(f"[熔断器] {source} 半开状态探测超时，重新探测")
                    return True
                return False

            return True
    
    def record_inconclusive(self, source: str) -> None:
        """记录不确定的探测结果（如返回 None）。

        仅影响 HALF_OPEN 状态：将其转回 OPEN 以便冷却后重新探测。
        CLOSED 状态下为空操作，不影响失败计数。
        """
        with self._lock:
            state = self._get_state_locked(source)
            if state['state'] == self.HALF_OPEN:
                state['state'] = self.OPEN
                state['half_open_calls'] = 0
                state['last_failure_time'] = time.time()
                logger.info(f"[熔断器] {source} 半开探测结果不确定，重新进入冷却")

    def record_success(self, source: str) -> None:
        """记录成功请求"""
        with self._lock:
            state = self._get_state_locked(source)

            if state['state'] == self.HALF_OPEN:
                # 半开状态下成功，完全恢复
                logger.info(f"[熔断器] {source} 半开状态请求成功，恢复正常")

            # 重置状态
            state['state'] = self.CLOSED
            state['failures'] = 0
            state['half_open_calls'] = 0
    
    def record_failure(self, source: str, error: Optional[str] = None) -> None:
        """记录失败请求"""
        with self._lock:
            state = self._get_state_locked(source)
            current_time = time.time()

            state['failures'] += 1
            state['last_failure_time'] = current_time

            if state['state'] == self.HALF_OPEN:
                # 半开状态下失败，继续熔断
                state['state'] = self.OPEN
                state['half_open_calls'] = 0
                logger.warning(f"[熔断器] {source} 半开状态请求失败，继续熔断 {self.cooldown_seconds}s")
            elif state['failures'] >= self.failure_threshold:
                # 达到阈值，进入熔断
                state['state'] = self.OPEN
                logger.warning(f"[熔断器] {source} 连续失败 {state['failures']} 次，进入熔断状态 "
                              f"(冷却 {self.cooldown_seconds}s)")
                if error:
                    logger.warning(f"[熔断器] 最后错误: {error}")
    
    def get_status(self) -> Dict[str, str]:
        """获取所有数据源状态"""
        with self._lock:
            return {source: info['state'] for source, info in self._states.items()}
    
    def reset(self, source: Optional[str] = None) -> None:
        """重置熔断器状态"""
        with self._lock:
            if source:
                if source in self._states:
                    del self._states[source]
            else:
                self._states.clear()


# 全局熔断器实例（实时行情专用）
_realtime_circuit_breaker = CircuitBreaker(
    failure_threshold=3,      # 连续失败3次熔断
    cooldown_seconds=300.0,   # 冷却5分钟
    half_open_max_calls=1
)

# 筹码接口熔断器（更保守的策略，因为该接口更不稳定）
_chip_circuit_breaker = CircuitBreaker(
    failure_threshold=2,      # 连续失败2次熔断
    cooldown_seconds=600.0,   # 冷却10分钟
    half_open_max_calls=1
)


# 筹码链路诊断计数器（进程内全局单例，P1.6 新增）
_chip_diagnostics = ChipDiagnostics()


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器"""
    return _realtime_circuit_breaker


def get_chip_circuit_breaker() -> CircuitBreaker:
    """获取筹码接口熔断器"""
    return _chip_circuit_breaker


def get_chip_diagnostics() -> ChipDiagnostics:
    """获取筹码链路诊断计数器（进程内全局单例）。"""
    return _chip_diagnostics
