/* ==========================================================================
 * 历史归档页：合并「今日 manifest」与运行时 fetch 的 reports-index.json，
 * 按日期列出各时段与可点击的报告片段链接。无网络/无索引时优雅降级为仅今日。
 * ========================================================================== */
import { SLOT_ORDER } from '../dsa';
import { el, clear, colorize } from '../dom';
import { pageHeader } from '../components/cards';
import { renderEmpty } from '../components/states';
import { TYPE_LABELS, TYPE_HREF, buildArchiveMap, applyTodayTimeFilter } from '../components/archive-data';

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

  // 聚合：合并注入 manifest 与运行时 reports-index.json，并按北京时间过滤未来时段
  const entries = applyTodayTimeFilter(await buildArchiveMap());

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
      slotsWrap.append(slotGroup(date, s.label, s.fragments));
    }
    card.append(slotsWrap);
    list.append(card);
  }
  colorize(list);
}
