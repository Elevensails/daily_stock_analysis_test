#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
U14 长期记忆 — 离线回填脚本
=============================

把 ``analysis_history`` 中已有的分析结论批量抽取、向量化并幂等写入
``analysis_memory_vector`` 表，用于：
- 存量历史一次性回填
- 向量模型升级/重建
- embedding 失败的离线补偿（断网环境下只落文本，向量由此脚本补）

用法::

    python scripts/backfill_ltm.py --probe              # 探针：检查连接与配置
    python scripts/backfill_ltm.py --limit 50 --dry-run  # 预览 50 条（不写库）
    python scripts/backfill_ltm.py --code 600519          # 回填指定股票
    python scripts/backfill_ltm.py --since 2025-01-01     # 回填指定日期之后
    python scripts/backfill_ltm.py --rebuild              # 全量重建
    python scripts/backfill_ltm.py --histogram            # 合成 50 条语料自检

默认使用本地词法向量（``LTM_EMBEDDING_PROVIDER=local``，裁决 F），零网络、零成本。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# ── 注入 repo 根（确保 src.* / data_provider 可导入） ──────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_ltm")


# ═══════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════

def _parse_date(raw: str) -> Optional[date]:
    """解析 YYYY-MM-DD 日期字符串。"""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _hash_text(text: str) -> str:
    """sha256(conclusion_text) —— 与 writer 内 text_hash 口径一致。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════════════

def build_provider(config: Any):
    """构造 embedding provider（裁决 F：默认 local）。"""
    provider_name = os.environ.get(
        "LTM_EMBEDDING_PROVIDER",
        getattr(config, "ltm_embedding_provider", "local"),
    )
    if str(provider_name).strip().lower() == "local":
        from src.memory.embedding_provider import build_local_provider
        return build_local_provider(config)
    else:
        from src.memory.embedding_provider import build_embedding_provider
        provider, degraded, reason = build_embedding_provider(config)
        if degraded:
            logger.warning("embedding 降级: %s", reason)
        return provider


def load_history(
    db: Any,
    *,
    limit: Optional[int] = None,
    code: Optional[str] = None,
    since: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """从 ``analysis_history`` 读取待回填记录。

    只读具备有效结论字段的记录（sentiment_score / trend_prediction /
    analysis_summary / operation_advice 至少一个不为空）。
    """
    from sqlalchemy import text as sa_text

    conditions = [
        "id > 0",
        (
            "(sentiment_score IS NOT NULL AND sentiment_score >= 0)"
            " OR (trend_prediction IS NOT NULL AND trend_prediction != '')"
            " OR (analysis_summary IS NOT NULL AND analysis_summary != '')"
            " OR (operation_advice IS NOT NULL AND operation_advice != '')"
        ),
    ]
    params: Dict[str, Any] = {}

    if code:
        conditions.append("code = :code")
        params["code"] = str(code).strip()
    if since:
        conditions.append("created_at >= :since")
        params["since"] = str(since)

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM analysis_history WHERE {where} ORDER BY id ASC"
    if limit is not None and limit > 0:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    rows: List[Dict[str, Any]] = []
    with db.session_scope() as session:
        result = session.execute(sa_text(sql), params)
        for row in result.mappings():
            rows.append(dict(row))
    return rows


def backfill(
    *,
    config: Any,
    db: Any,
    provider: Any,
    writer: Any,
    store: Any,
    limit: Optional[int] = None,
    code: Optional[str] = None,
    since: Optional[date] = None,
    dry_run: bool = False,
    rebuild: bool = False,
) -> Dict[str, Any]:
    """主回填流程。"""
    from src.memory.models import MemoryRecord, normalize_trade_date, normalize_time_slot, VECTOR_VERSION

    rows = load_history(db, limit=limit, code=code, since=since)
    stats: Dict[str, Any] = {
        "scanned": len(rows),
        "built": 0,
        "embedded": 0,
        "written": 0,
        "skipped": 0,
        "empty_text": 0,
        "dry_run": dry_run,
        "elapsed_ms": 0.0,
    }

    if not rows:
        logger.info("无待回填记录")
        return stats

    started = time.perf_counter()

    # 阶段 1：抽取结论文本
    records: List[MemoryRecord] = []
    for row in rows:
        history_id = int(row["id"])
        row_code = str(row.get("code") or "").strip()
        row_name = str(row.get("name") or "").strip()
        row_report_type = str(row.get("report_type") or "daily").strip() or "daily"
        trend = str(row.get("trend_prediction") or "")
        advice = str(row.get("operation_advice") or "")
        summary = str(row.get("analysis_summary") or "")
        sentiment = row.get("sentiment_score")
        op_advice = str(row.get("operation_advice") or "")
        created_at = row.get("created_at")

        # 推断 trade_date 与 time_slot
        trade_date = ""
        time_slot_val = None
        if created_at is not None:
            if isinstance(created_at, (date, datetime)):
                trade_date = normalize_trade_date(created_at)
                time_slot_val = normalize_time_slot(
                    f"{created_at.hour:02d}{created_at.minute:02d}"
                )

        conclusion_text = writer.build_conclusion_text(
            trend=trend,
            advice=advice,
            summary=summary,
            code=row_code,
            name=row_name,
        )
        if not conclusion_text:
            stats["empty_text"] += 1
            continue

        record = MemoryRecord(
            history_id=history_id,
            code=row_code,
            name=row_name,
            report_type=row_report_type,
            trade_date=trade_date,
            time_slot=time_slot_val,
            conclusion_text=conclusion_text,
            sentiment_score=writer._coerce_sentiment(sentiment),
            operation_advice=str(op_advice or "")[:20],
        )
        if record.is_valid():
            records.append(record)
            stats["built"] += 1

    if not records:
        stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info("无有效结论文本，跳过 embedding 与写入")
        return stats

    # 阶段 2：批量 embed
    texts = [r.conclusion_text for r in records]
    try:
        matrix = provider.embed(texts)
    except Exception as exc:
        logger.error("批量 embedding 失败: %s", exc)
        stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        return stats

    import numpy as np
    array = np.asarray(matrix, dtype=np.float32)
    if array.ndim != 2 or array.shape[0] != len(records):
        logger.error("embedding shape mismatch: %s vs %d records", getattr(array, "shape", None), len(records))
        stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        return stats

    for i, record in enumerate(records):
        vec = array[i]
        if not np.any(vec):
            continue
        record.embedding = vec
        stats["embedded"] += 1

    # 阶段 3：幂等写入
    if dry_run:
        logger.info(
            "[DRY-RUN] 将写入 %d 条（已 embed %d 条），model_id=%s vector_version=%d",
            len(records), stats["embedded"], provider.model_id, VECTOR_VERSION,
        )
        for r in records[:5]:
            logger.info("  [DRY-RUN] history_id=%d code=%s text=%s",
                        r.history_id, r.code, r.conclusion_text[:80])
        if len(records) > 5:
            logger.info("  ... 共 %d 条", len(records))
    else:
        result = store.upsert_many(records)
        stats["written"] = int(result.get("written", 0))
        stats["skipped"] = int(result.get("skipped", 0))
        logger.info(
            "写入完成: written=%d skipped=%d (model_id=%s vector_version=%d)",
            stats["written"], stats["skipped"], provider.model_id, VECTOR_VERSION,
        )

    stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    return stats


# ═══════════════════════════════════════════════════════════════════════
# 探针
# ═══════════════════════════════════════════════════════════════════════

def probe(config: Any, db: Any) -> int:
    """探测 embedding provider 连通性与 DB 表状态。返回 exit code。"""
    ok = True

    # 1. config
    print("=== Config ===")
    for key in sorted(dir(config)):
        if key.startswith("ltm_"):
            print(f"  {key} = {getattr(config, key, None)}")
    if not getattr(config, "ltm_enabled", False):
        print("  ⚠  ltm_enabled=False —— 长期记忆未启用，回填后召回仍为空")

    # 2. embedding provider
    print("\n=== Embedding Provider ===")
    try:
        provider = build_provider(config)
        print(f"  provider_name = {provider.provider_name}")
        print(f"  model_id      = {provider.model_id}")
        texts = ["[趋势] 测试探针文本 [建议] 观望 [要点] 这是一条用于检测 embedding 连通性的合成样本"]
        vecs = provider.embed(texts)
        import numpy as np
        arr = np.asarray(vecs, dtype=np.float32)
        print(f"  dim           = {arr.shape[1]}")
        print(f"  norm          = {float(np.linalg.norm(arr[0])):.6f}")
        print(f"  ✅ embedding provider 正常")
    except Exception as exc:
        print(f"  ❌ embedding provider 失败: {exc}")
        ok = False

    # 3. DB
    print("\n=== Database ===")
    try:
        from sqlalchemy import text as sa_text
        with db.session_scope() as session:
            count = session.execute(
                sa_text("SELECT COUNT(*) AS cnt FROM analysis_history")
            ).scalar()
            print(f"  analysis_history 记录数: {count}")
            try:
                vec_count = session.execute(
                    sa_text("SELECT COUNT(*) AS cnt FROM analysis_memory_vector")
                ).scalar()
                print(f"  analysis_memory_vector 记录数: {vec_count}")
            except Exception:
                print("  analysis_memory_vector 表不存在或无法访问")
    except Exception as exc:
        print(f"  ❌ DB 连接失败: {exc}")
        ok = False

    return 0 if ok else 1


# ═══════════════════════════════════════════════════════════════════════
# 柱状图自检（裁决 E：阈值 0.75，≥50 条合成语料）
# ═══════════════════════════════════════════════════════════════════════

_HISTOGRAM_CORPUS = [
    "[趋势] 放量突破年线，MACD 金叉 [建议] 买入 [要点] 机构大幅加仓，北向连续流入",
    "[趋势] 缩量回踩 20 日线，量能萎缩 [建议] 观望 [要点] 主线切换至有色，资金分歧加大",
    "[趋势] 高开低走收长上影，抛压明显 [建议] 减仓 [要点] 短期获利盘涌出，上方套牢盘密集",
    "[趋势] 低位十字星，成交量极度萎缩 [建议] 持有 [要点] 底部特征初现，但需放量确认",
    "[趋势] 连续三日跳空上行，短期超买 [建议] 卖出 [要点] RSI>80，技术面回调需求强烈",
    "[趋势] 沿 5 日线稳步上行，量价配合良好 [建议] 加仓 [要点] 趋势健康，筹码锁定度高",
    "[趋势] 破位下行，跌破前期低点支撑 [建议] 卖出 [要点] 止损线触发，规避系统性风险",
    "[趋势] 箱体震荡整理，方向待选择 [建议] 持有 [要点] 基本面良好，等待催化剂",
    "[趋势] 尾盘放量拉升，疑似主力护盘 [建议] 观望 [要点] 资金面偏紧，持续性存疑",
    "[趋势] 早盘急跌后V型反转，承接有力 [建议] 买入 [要点] 恐慌盘被消化，下方支撑强劲",
    "[趋势] 窄幅横盘，布林带收窄至极致 [建议] 持有 [要点] 变盘窗口临近，向上概率偏大",
    "[趋势] 跳空低开跌破所有短期均线 [建议] 减仓 [要点] 负面消息发酵，空头排列确立",
    "[趋势] 温和放量站上 60 日线 [建议] 买入 [要点] 中期趋势转多，估值处于历史低位",
    "[趋势] 天量长阴，换手率异常高 [建议] 卖出 [要点] 主力出货迹象明显，短期回避",
    "[趋势] 回踩确认支撑后反弹 [建议] 加仓 [要点] 二次探底成功，W底形态初成",
    "[趋势] 大盘普跌中逆势收红 [建议] 持有 [要点] 防御属性凸显，资金避险偏好",
    "[趋势] 财报超预期后高开低走 [建议] 观望 [要点] 利好兑现，市场已有充分预期",
    "[趋势] 二次冲顶失败，双顶形态 [建议] 减持 [要点] 量价背离，顶部风险积聚",
    "[趋势] 底部逐级抬高，均线开始粘合 [建议] 买入 [要点] 蓄势待发，关注突破信号",
    "[趋势] 冲高回落，收出长上影周线 [建议] 观望 [要点] 高位压力明显，调整尚未结束",
    "[趋势] 暴力拉升后高位换手 [建议] 持有 [要点] 短线资金博弈激烈，谨慎追高",
    "[趋势] 缩量小阳缓慢爬升 [建议] 持有 [要点] 慢牛格局延续，回调即机会",
    "[趋势] 放量下行跌破重要关口 [建议] 卖出 [要点] 关键支撑失守，止损纪律优先",
    "[趋势] 利空出尽后止跌企稳 [建议] 买入 [要点] 悲观预期充分定价，左侧布局窗口",
    "[趋势] 连续十字星，多空均衡 [建议] 观望 [要点] 方向不明朗，等待信号确认",
    "[趋势] 突破前期高点后回踩确认 [建议] 加仓 [要点] 突破有效，上升空间打开",
    "[趋势] 业绩预告不及预期，跳空下行 [建议] 减仓 [要点] 基本面恶化，估值需重估",
    "[趋势] 板块轮动中补涨启动 [建议] 买入 [要点] 资金高低切换，相对估值优势",
    "[趋势] 高位震荡出货迹象 [建议] 减持 [要点] 换手率持续偏高，警惕诱多",
    "[趋势] 低估值高股息防御配置 [建议] 持有 [要点] 分红率可观，下行风险有限",
    "[趋势] 突发利空急速跳水后快速修复 [建议] 买入 [要点] 错杀机会，基本面未变",
    "[趋势] 政策利好驱动放量涨停 [建议] 持有 [要点] 短期情绪过热，关注开板后走势",
    "[趋势] 弱势反弹无量能配合 [建议] 卖出 [要点] 技术性反抽，下跌中继概率大",
    "[趋势] 筹码持续集中，股东人数下降 [建议] 买入 [要点] 主力吸筹阶段，静待拉升",
    "[趋势] 年报分红方案超预期 [建议] 加仓 [要点] 高分红吸引长期资金配置",
    "[趋势] 板块龙头回调拖累跟跌 [建议] 持有 [要点] 系统性调整非个股问题",
    "[趋势] 回购注销方案公布 [建议] 买入 [要点] 管理层信心信号，EPS增厚",
    "[趋势] 新产能投产在即 [建议] 持有 [要点] 盈利增长确定性高，等待兑现",
    "[趋势] 行业景气度下行 [建议] 减持 [要点] 周期见顶信号，逆风而行风险大",
    "[趋势] 技术面与基本面共振向好 [建议] 买入 [要点] 戴维斯双击机会，确定性高",
    "[趋势] 高位平台整理偏弱 [建议] 观望 [要点] 上方筹码密集，突破需放量配合",
    "[趋势] 底部放量三连阳 [建议] 买入 [要点] 红三兵形态，反转信号较强",
    "[趋势] 估值修复接近尾声 [建议] 减仓 [要点] PE已回归历史均值，上行空间收窄",
    "[趋势] 中报预告大幅增长 [建议] 加仓 [要点] 业绩拐点确认，成长逻辑强化",
    "[趋势] 股东减持计划公告 [建议] 观望 [要点] 短期承压，但不改中长期逻辑",
    "[趋势] 融资余额持续攀升 [建议] 持有 [要点] 杠杆资金看多，但需警惕去杠杆风险",
    "[趋势] 大宗交易溢价成交 [建议] 买入 [要点] 机构溢价接盘，看好后市",
    "[趋势] 尾盘集合竞价异动拉升 [建议] 观望 [要点] 疑似收盘价操纵，次日关注开盘",
    "[趋势] 板块ETF大幅净申购 [建议] 持有 [要点] 被动资金流入支撑，系统性机会",
    "[趋势] 跌破布林带下轨后反弹 [建议] 买入 [要点] 超跌反弹，下轨支撑有效",
    "[趋势] MA5金叉MA20 [建议] 买入 [要点] 短期均线多头排列确立",
    "[趋势] 高位天量十字星 [建议] 减持 [要点] 变盘信号，短期头部概率大",
]


def run_histogram(config: Any, db: Any) -> int:
    """用 ≥50 条合成语料自检 embedding + 召回通路。"""
    import numpy as np

    print("=== Histogram 自检（裁决 E：阈值 0.75，语料 %d 条）===" % len(_HISTOGRAM_CORPUS))

    # 1. provider
    try:
        provider = build_provider(config)
        print(f"provider: {provider.provider_name} / {provider.model_id}")
    except Exception as exc:
        print(f"❌ provider 构造失败: {exc}")
        return 1

    # 2. embed 全部语料
    print("正在 embed %d 条合成语料..." % len(_HISTOGRAM_CORPUS))
    try:
        matrix = provider.embed(_HISTOGRAM_CORPUS)
        vectors = np.asarray(matrix, dtype=np.float32)
        print(f"  shape = {vectors.shape}")
    except Exception as exc:
        print(f"❌ embed 失败: {exc}")
        return 1

    # L2 归一化
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    vectors = vectors / norms

    # 3. 自相似矩阵
    sim_matrix = vectors @ vectors.T

    # 4. 统计
    all_sims: List[float] = []
    same_pair_sims: List[float] = []
    for i in range(len(_HISTOGRAM_CORPUS)):
        for j in range(i + 1, len(_HISTOGRAM_CORPUS)):
            s = float(sim_matrix[i, j])
            all_sims.append(s)

    # 按趋势分类统计组内相似度
    buy_indices = [i for i, t in enumerate(_HISTOGRAM_CORPUS) if "[建议] 买入" in t]
    sell_indices = [i for i, t in enumerate(_HISTOGRAM_CORPUS) if "[建议] 卖出" in t or "[建议] 减持" in t]
    hold_indices = [i for i, t in enumerate(_HISTOGRAM_CORPUS) if "[建议] 持有" in t or "[建议] 观望" in t]

    def _group_sim(indices):
        sims = []
        for a in range(len(indices)):
            for b in range(a + 1, len(indices)):
                sims.append(float(sim_matrix[indices[a], indices[b]]))
        return sims

    buy_sims = _group_sim(buy_indices)
    sell_sims = _group_sim(sell_indices)
    hold_sims = _group_sim(hold_indices)

    arr = np.array(all_sims)
    threshold = 0.75

    print(f"\n全量统计（{len(all_sims)} 对）:")
    print(f"  mean   = {float(arr.mean()):.4f}")
    print(f"  std    = {float(arr.std()):.4f}")
    print(f"  min    = {float(arr.min()):.4f}")
    print(f"  max    = {float(arr.max()):.4f}")
    above = int((arr >= threshold).sum())
    print(f"  ≥{threshold}: {above} / {len(all_sims)} ({100.0 * above / len(all_sims):.1f}%)")

    if buy_sims:
        b = np.array(buy_sims)
        print(f"\n买入组内（{len(buy_sims)} 对）: mean={float(b.mean()):.4f} std={float(b.std()):.4f}")
    if sell_sims:
        s = np.array(sell_sims)
        print(f"\n卖出组内（{len(sell_sims)} 对）: mean={float(s.mean()):.4f} std={float(s.std()):.4f}")
    if hold_sims:
        h = np.array(hold_sims)
        print(f"\n持有/观望组内（{len(hold_sims)} 对）: mean={float(h.mean()):.4f} std={float(h.std()):.4f}")

    # 交叉组相似度（买入 vs 卖出）
    cross_sims = []
    for bi in buy_indices:
        for si in sell_indices:
            cross_sims.append(float(sim_matrix[bi, si]))
    if cross_sims:
        c = np.array(cross_sims)
        print(f"\n买入×卖出交叉（{len(cross_sims)} 对）: mean={float(c.mean()):.4f} std={float(c.std()):.4f}")

    # 5. 结论
    print(f"\n{'✅' if float(arr.mean()) > 0.3 else '⚠'} 平均相似度 {float(arr.mean()):.4f} {'>' if float(arr.mean()) > 0.3 else '<='} 0.3")
    print(f"  买入组内均值 {float(np.array(buy_sims).mean()):.4f} — 应高于交叉组均值 {float(np.array(cross_sims).mean()):.4f}" if buy_sims and cross_sims else "")
    print(f"  阈值 0.75 以上占比 {100.0 * above / len(all_sims):.1f}%")

    return 0


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="U14 长期记忆 — 离线回填（analysis_history → analysis_memory_vector）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --probe                        探测 embedding 与 DB 连通性
  %(prog)s --limit 50 --dry-run           预览 50 条，不写库
  %(prog)s --code 600519 --limit 100      回填贵州茅台最近 100 条
  %(prog)s --since 2025-01-01             回填 2025-01-01 之后的所有记录
  %(prog)s --rebuild                      全量重建（慎用，耗时）
  %(prog)s --histogram                    合成语料自检
""",
    )
    parser.add_argument("--probe", action="store_true", help="探测 embedding 与 DB 连通性")
    parser.add_argument("--limit", type=int, default=None, help="最大回填条数")
    parser.add_argument("--code", type=str, default=None, help="按股票代码过滤")
    parser.add_argument("--since", type=str, default=None, help="按日期过滤（YYYY-MM-DD）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写库")
    parser.add_argument("--rebuild", action="store_true", help="全量重建（忽略 limit/code/since）")
    parser.add_argument("--histogram", action="store_true", help="≥50 条合成语料自检")

    args = parser.parse_args()

    # 提前导入（已通过 sys.path.insert 注入 repo 根）
    from src.config import get_config
    from src.storage import get_db

    config = get_config()
    db = get_db()

    if args.histogram:
        return run_histogram(config, db)

    if args.probe:
        return probe(config, db)

    # 构造 writer / store / provider
    from src.memory.writer import build_memory_writer
    from src.memory.vector_store import build_vector_store

    provider = build_provider(config)
    store = build_vector_store(config, provider.model_id, db_manager=db)
    writer = build_memory_writer(config, provider=provider, store=store, db_manager=db)

    model_id = provider.model_id
    from src.memory.models import VECTOR_VERSION as vv
    logger.info("backfill_ltm: provider=%s model_id=%s vector_version=%d",
                provider.provider_name, model_id, vv)

    since_date = _parse_date(args.since) if args.since else None
    limit = args.limit
    code = args.code

    if args.rebuild:
        limit = None
        code = None
        since_date = None
        logger.info("--rebuild: 全量重建模式")

    stats = backfill(
        config=config,
        db=db,
        provider=provider,
        writer=writer,
        store=store,
        limit=limit,
        code=code,
        since=since_date,
        dry_run=args.dry_run,
        rebuild=args.rebuild,
    )

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}回填统计:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
