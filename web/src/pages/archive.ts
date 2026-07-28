/* ==========================================================================
 * 历史归档页：合并「今日 manifest」与运行时 fetch 的 reports-index.json，
 * 按日期列出各时段与可点击的报告片段链接。无网络/无索引时优雅降级为仅今日。
 * ========================================================================== */
import { getDsa, SLOT_ORDER } from '../dsa';
import { el, clear, colorize } from '../dom';
import { pageHeader } from '../components/cards';
import { renderEmpty } from '../components/states';

interface IndexEntry {
  slots: string[];
  fragments: string[];
}
interface ReportsIndex {
  dates: string[];
  entries: Record<string, IndexEntry>;
}

const TYPE_LABELS: Record<string, string> = {
  report: '个股分析',
  market_review: '大盘复盘',
  vibe: '量化分析',
};
const TYPE_HREF: Record<string, string> = {
  report: 'report.html',
  market_review: 'market_review.html',
  vibe: 'vibe.html',
};
const SLOT_LABEL: Record<string, string> = {
  '0900': '09:00 早盘',
  '0930': '09:30 开盘',
  '1200': '12:00 午间',
  '1430': '14:30 午盘',
  '1800': '18:00 收盘',
};

function dateLabel(d: string): string {
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
}

function slotGroup(date: string, label: string, frags: Record<string, string | null>): HTMLElement {
  const links = el('div', { class: 'slot-group__links' });
  let any = false;
  for (const t of ['report', 'market_review', 'vibe']) {
    const file = frags[t];
    if (file) {
      any = true;
      links.append(
        el('a', { class: 'chip chip--ok', href: `${TYPE_HREF[t]}#${file.replace(/\.html$/, '')}` }, [
          TYPE_LABELS[t],
        ])
      );
    }
  }
  if (!any) links.append(el('span', { class: 'chip chip--pending' }, ['全部待生成']));
  return el('div', { class: 'slot-group' }, [
    el('div', { class: 'slot-group__title', text: label }),
    links,
  ]);
}

export async function renderArchive(root: HTMLElement): Promise<void> {
  clear(root);
  root.append(pageHeader('🗂 历史归档', '按日期浏览全部分析报告'));
  const list = el('div', { class: 'archive-list' });
  root.append(list);

  // 聚合：日期 → { slots: [{code, label, frags}] }
  const entries: Record<string, { slots: { code: string; label: string; frags: Record<string, string | null> }[] }> = {};

  // 1) 今日：来自注入的 manifest
  const { manifest } = getDsa();
  if (manifest?.slots) {
    for (const code of SLOT_ORDER) {
      const s = manifest.slots[code];
      if (!s) continue;
      const files = (Object.values(s.fragments || {}) as (string | null)[]).filter(Boolean);
      if (!files.length) continue;
      let date: string | null = null;
      for (const f of files) {
        const m = /_(\d{8})\.html$/.exec(f as string);
        if (m) {
          date = m[1];
          break;
        }
      }
      if (!date) continue;
      const entry = (entries[date] ??= { slots: [] });
      entry.slots.push({ code, label: s.label || code, frags: s.fragments || {} });
    }
  }

  // 2) 历史：合并 reports-index.json（运行时 fetch；失败则忽略）
  try {
    const res = await fetch('reports-index.json');
    if (res.ok) {
      const idx = (await res.json()) as ReportsIndex;
      for (const date of idx.dates || []) {
        const e = idx.entries?.[date];
        if (!e) continue;
        const entry = (entries[date] ??= { slots: [] });
        for (const slotCode of e.slots || []) {
          if (entry.slots.some((s) => s.code === slotCode)) continue;
          const frags: Record<string, string | null> = {};
          for (const f of e.fragments || []) {
            const m = /^(report|market_review|vibe)_/.exec(f);
            if (m) frags[m[1]] = f;
          }
          entry.slots.push({
            code: slotCode,
            label: SLOT_LABEL[slotCode] ?? slotCode,
            frags,
          });
        }
      }
    }
  } catch {
    /* 离线/无索引：仅展示今日 */
  }

  const dates = Object.keys(entries).sort((a, b) => b.localeCompare(a));
  if (!dates.length) {
    list.append(renderEmpty('暂无历史报告。运行分析后将在此按日期归档。'));
    return;
  }

  for (const date of dates) {
    const entry = entries[date];
    const card = el('div', { class: 'archive-date' }, [
      el('div', { class: 'archive-date__head' }, [
        el('span', { class: 'archive-date__date', text: dateLabel(date) }),
        el('span', { class: 'archive-date__slots-count', text: `${entry.slots.length} 个时段` }),
      ]),
    ]);
    const slotsWrap = el('div', { class: 'archive-date__slots' });
    for (const s of entry.slots) {
      slotsWrap.append(slotGroup(date, s.label, s.frags));
    }
    card.append(slotsWrap);
    list.append(card);
  }
  colorize(list);
}
