/* ==========================================================================
 * 归档 / 首页共享数据层：合并「构建期注入的 manifest.slots」与运行时 fetch 的
 * reports-index.json，并提供「按北京时间过滤未来时段」的工具。
 *
 * 修复两个实测 Bug：
 *  - 归档页在白天列出未来时段（如 1800_20260729 残留）：applyTodayTimeFilter
 *    对今日桶仅保留 code <= 当前时段阈值的 slot。
 *  - 首页「今日 5 时段分析」仅 vibe 可点：buildArchiveMap 同时合并运行时
 *    reports-index.json，与归档页共用同一份数据来源。
 *
 * 安全：所有动态文本均经 el().text / textContent 设置，绝不 innerHTML 注入。
 * 着色：仅用 CSS 令牌（var(--up)/var(--down) 等），不写死颜色。
 * ========================================================================== */
import { getDsa, SLOT_ORDER } from '../dsa';

/** 时段码升序（用于阈值比较）。 */
export const SLOT_CODES = ['0900', '0930', '1200', '1430', '1800'];

/** 时段显示标签（09:00 早盘 / 09:30 开盘 / 12:00 午间 / 14:30 午盘 / 18:00 收盘）。 */
export const SLOT_LABEL: Record<string, string> = {
  '0900': '09:00 早盘',
  '0930': '09:30 开盘',
  '1200': '12:00 午间',
  '1430': '14:30 午盘',
  '1800': '18:00 收盘',
};

/** 片段类型 → 中文标签（个股分析 / 大盘复盘 / 量化分析）。 */
export const TYPE_LABELS: Record<string, string> = {
  report: '个股分析',
  market_review: '大盘复盘',
  vibe: '量化分析',
};

/** 片段类型 → 落地页 href。 */
export const TYPE_HREF: Record<string, string> = {
  report: 'report.html',
  market_review: 'market_review.html',
  vibe: 'vibe.html',
};

/** 单个时段在归档 / 首页桶中的归一化形状（兼容 cards.ts 的 slotCard）。 */
export interface ArchiveSlot {
  code: string;
  label: string;
  fragments: Record<string, string | null>;
}

/** 某日归档桶：时段列表。 */
export interface ArchiveDay {
  slots: ArchiveSlot[];
}

/** 日期(YYYYMMDD) → 归档桶。 */
export type ArchiveMap = Record<string, ArchiveDay>;

/** reports-index.json 中单个日期的条目。 */
export interface ReportsIndexEntry {
  slots: string[];
  fragments: string[];
}

/** reports-index.json 根结构。 */
export interface ReportsIndex {
  dates: string[];
  entries: Record<string, ReportsIndexEntry>;
}

/**
 * 取北京时间 Date 对象：用 en-US 区域在 Asia/Shanghai 时区下格式化，再解析回 Date。
 * 返回的 Date 在本地时区下表示北京「墙上时钟」，getHours() 等读取即为北京时间。
 */
export function beijingNow(): Date {
  const text = new Date().toLocaleString('en-US', { timeZone: 'Asia/Shanghai' });
  return new Date(text);
}

/** 北京时间日期串 YYYYMMDD（缺省取当前北京时刻）。 */
export function beijingDateStr(d?: Date): string {
  const date = d ?? beijingNow();
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${y}${m}${day}`;
}

/**
 * 当前时段阈值：取 ≤ 当前时分的最大时段码。
 * 例：10:00 → '0930'，14:35 → '1430'，盘前(<09:00) → ''（空，过滤掉全部时段）。
 */
export function currentSlotThreshold(now: Date): string {
  const cur = now.getHours() * 100 + now.getMinutes();
  let threshold = '';
  for (const code of SLOT_CODES) {
    if (parseInt(code, 10) <= cur) threshold = code;
  }
  return threshold;
}

/**
 * 运行时拉取 reports-index.json。失败或非 200 返回 null（调用方据此优雅降级）。
 */
export async function fetchReportsIndex(): Promise<ReportsIndex | null> {
  try {
    const res = await fetch('reports-index.json');
    if (!res.ok) return null;
    return (await res.json()) as ReportsIndex;
  } catch {
    return null;
  }
}

/**
 * 合并「注入的 manifest.slots」与「运行时 reports-index.json」，按日期归桶。
 *  - manifest 优先提供今日数据；每个 slot 用其 fragments 字段，从任一文件名
 *    正则 /_(\d{8})\.html$/ 提取日期。
 *  - reports-index.json 补充历史与运行时新产生的时段（已有同日同时段则不覆盖）。
 *  - 片段按 ^(report|market_review|vibe)_ 前缀分到对应类型。
 */
export async function buildArchiveMap(): Promise<ArchiveMap> {
  const map: ArchiveMap = {};

  // 1) 注入的 manifest.slots（构建期数据，今日为主）
  const { manifest } = getDsa();
  if (manifest?.slots) {
    for (const code of SLOT_ORDER) {
      const s = manifest.slots[code];
      if (!s) continue;
      const files = (Object.values(s.fragments || {}) as (string | null)[]).filter(
        (f): f is string => typeof f === 'string' && f.length > 0
      );
      if (!files.length) continue;
      let date: string | null = null;
      for (const f of files) {
        const m = /_(\d{8})\.html$/.exec(f);
        if (m) {
          date = m[1];
          break;
        }
      }
      if (!date) continue;
      const day = (map[date] ??= { slots: [] });
      if (!day.slots.some((x) => x.code === code)) {
        day.slots.push({
          code,
          label: s.label || SLOT_LABEL[code] || code,
          fragments: s.fragments || {},
        });
      }
    }
  }

  // 2) 运行时 reports-index.json（补充历史 / 实时新产出）
  const idx = await fetchReportsIndex();
  if (idx) {
    for (const date of idx.dates || []) {
      const e = idx.entries?.[date];
      if (!e) continue;
      const day = (map[date] ??= { slots: [] });
      for (const slotCode of e.slots || []) {
        if (day.slots.some((x) => x.code === slotCode)) continue;
        const frags: Record<string, string | null> = {};
        for (const f of e.fragments || []) {
          const m = /^(report|market_review|vibe)_/.exec(f);
          if (m) frags[m[1]] = f;
        }
        day.slots.push({
          code: slotCode,
          label: SLOT_LABEL[slotCode] ?? slotCode,
          fragments: frags,
        });
      }
    }
  }

  return map;
}

/**
 * 对今日(北京时间)桶应用时段过滤：仅保留 code <= 当前时段阈值的 slot，
 * 过滤掉未来时段（含 1800 残留）。历史日期桶保持不变。原地修改并返回同一引用。
 */
export function applyTodayTimeFilter(entries: ArchiveMap): ArchiveMap {
  const today = beijingDateStr();
  const threshold = currentSlotThreshold(beijingNow());
  const day = entries[today];
  if (day) {
    day.slots = day.slots.filter((s) => s.code <= threshold);
  }
  return entries;
}
