/* ==========================================================================
 * 卡片与区块组件：StatCard / NavCard / SlotCard / pageHeader / sectionTitle。
 * 仅消费数据，渲染语义化 DOM；着色由 CSS 令牌与 colorize 负责。
 * ========================================================================== */
import { el } from '../dom';
import type { SlotEntry } from '../dsa';

export function statCard(opts: { icon: string; label: string; value: string }): HTMLElement {
  return el('div', { class: 'card stat-card col-3' }, [
    el('div', { class: 'stat-card__icon', text: opts.icon }),
    el('div', { class: 'stat-card__value', text: opts.value }),
    el('div', { class: 'stat-card__label', text: opts.label }),
  ]);
}

export function navCard(opts: {
  title: string;
  desc: string;
  href: string;
  variant?: 'live' | 'archive';
}): HTMLElement {
  const variant = opts.variant ?? 'live';
  return el(
    'a',
    { class: `card nav-card nav-card--${variant} col-6`, href: opts.href },
    [
      el('div', { class: 'nav-card__title', text: opts.title }),
      el('div', { class: 'nav-card__desc', text: opts.desc }),
    ]
  );
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

/** 首页时段卡：列出该时段各类型片段链接，缺失则显示「待生成」。 */
export function slotCard(code: string, slot: SlotEntry): HTMLElement {
  const frags = slot.fragments || {};
  const links = el('div', { class: 'slot-card__links' });
  for (const t of ['report', 'market_review', 'vibe']) {
    const file = frags[t];
    if (file) {
      links.append(
        el('a', { class: 'chip chip--ok', href: `${TYPE_HREF[t]}#${file.replace(/\.html$/, '')}` }, [
          TYPE_LABELS[t],
        ])
      );
    } else {
      links.append(el('span', { class: 'chip chip--pending' }, [`${TYPE_LABELS[t]} · 待生成`]));
    }
  }
  return el('div', { class: 'card slot-card col-4' }, [
    el('div', { class: 'slot-card__head' }, [
      el('span', { class: 'slot-card__time', text: slot.label || code }),
      el('span', { class: 'slot-card__name', text: slot.name || '' }),
    ]),
    links,
  ]);
}

export function pageHeader(title: string, subtitle?: string): HTMLElement {
  const wrap = el('div', { class: 'page-header' }, [el('h1', { text: title })]);
  if (subtitle) wrap.append(el('div', { class: 'page-header__sub', text: subtitle }));
  return wrap;
}

export function sectionTitle(text: string): HTMLElement {
  return el('h2', { class: 'section-title', text });
}
