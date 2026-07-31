/* ==========================================================================
 * 首页：Hero + 「主要指数实时」概览 + 快捷入口 + 今日 5 时段分析（来自 manifest.slots）。
 *
 * 「今日概览」改造（BugFix）：原 4 张静态统计卡（时段/持仓/报告/月成本）对用户无意义，
 * 现改为「主要指数实时」卡片行（上证 / 深证成指 / 创业板指），数据直接从东方财富实时拉取，
 * 支持手动刷新按钮 + 30s 自动刷新，接口失败时回退「指数数据暂不可用」。
 * ========================================================================== */
import { getDsa, SLOT_ORDER, type SlotEntry } from '../dsa';
import { el, clear } from '../dom';
import { navCard, slotCard, pageHeader, sectionTitle } from '../components/cards';
import { renderEmpty } from '../components/states';
import { fetchMarketIndices, indexCardEl, nowTs } from '../components/quote';
import { buildArchiveMap, beijingDateStr, beijingNow, currentSlotThreshold, SLOT_LABEL } from '../components/archive-data';

export function renderHome(root: HTMLElement): void {
  clear(root);
  const { manifest } = getDsa();

  if (!manifest) {
    root.append(renderEmpty('内容尚未生成，请运行分析流水线后查看。'));
    return;
  }

  root.append(
    pageHeader(
      manifest.site?.title || 'A股智能分析 · 决策仪表盘',
      `DeepSeek AI · 生成于 ${manifest.generatedAt || ''}`
    )
  );

  // 今日概览：主要指数实时
  const idxWrap = el('div', { class: 'idx-grid', id: 'home-indices' });
  for (let i = 0; i < 3; i++) idxWrap.append(el('div', { class: 'skeleton-card' }));
  const idxTs = el('span', { class: 'overview-ts', id: 'home-idx-ts', text: '加载中...' });
  const refreshBtn = el('button', {
    class: 'refresh-btn',
    id: 'home-idx-refresh',
    text: '🔄 刷新',
    onClick: () => void loadIndices(),
  });
  const overviewHead = el('div', { class: 'overview-head' }, [idxTs, refreshBtn]);
  root.append(sectionTitle('今日概览'), overviewHead, idxWrap);

  // 快捷入口
  const navRow = document.createElement('div');
  navRow.className = 'grid';
  navRow.append(
    navCard({ title: '⚡ 实时盯盘', desc: '30s 刷新 · 4 持仓 + 指数', href: 'dashboard.html', variant: 'live' }),
    navCard({ title: '🗂 历史归档', desc: '按日期浏览全部报告', href: 'archive.html', variant: 'archive' })
  );
  root.append(sectionTitle('快捷入口'), navRow);

  // 今日 5 时段分析（异步填充：合并运行时 reports-index.json，按北京时间过滤未来时段）
  const slotsWrap = document.createElement('div');
  slotsWrap.className = 'grid';
  // 先放 5 个占位骨架卡，避免布局跳动
  for (let i = 0; i < SLOT_ORDER.length; i++) {
    slotsWrap.append(el('div', { class: 'skeleton-card' }));
  }
  root.append(sectionTitle('今日 5 时段分析'), slotsWrap);

  void (async () => {
    const threshold = currentSlotThreshold(beijingNow());
    // 今日桶：合并 manifest + reports-index.json；缺失 / fetch 失败则回退 manifest.slots
    let source: Record<string, SlotEntry> | null = null;
    try {
      const map = await buildArchiveMap();
      const bucket = map[beijingDateStr()];
      if (bucket) {
        source = {};
        for (const s of bucket.slots) source[s.code] = { label: s.label, fragments: s.fragments };
      }
    } catch {
      source = null;
    }
    if (!source) source = manifest?.slots ?? {};

    const buildSlot = (code: string): HTMLElement => {
      const fromSource = source[code] ?? null;
      const isFuture = code > threshold;
      let fragments: Record<string, string | null> =
        fromSource?.fragments ?? { report: null, market_review: null, vibe: null };
      // 未来时段：强制隐藏真实链接，显示为「待生成」
      if (isFuture) fragments = { report: null, market_review: null, vibe: null };
      const slot: SlotEntry = { label: SLOT_LABEL[code] ?? code, fragments };
      return slotCard(code, slot);
    };

    clear(slotsWrap);
    for (const code of SLOT_ORDER) slotsWrap.append(buildSlot(code));
  })();

  // 主要指数实时加载：手动刷新 + 30s 自动刷新
  let timer = 0;
  const loadIndices = async (): Promise<void> => {
    const wrap = root.querySelector('#home-indices') as HTMLElement | null;
    const ts = root.querySelector('#home-idx-ts') as HTMLElement | null;
    if (!wrap) return;
    wrap.classList.add('is-updating');
    if (ts) ts.textContent = '更新中...';
    try {
      const quotes = await fetchMarketIndices();
      clear(wrap);
      for (const q of quotes) wrap.append(indexCardEl(q));
      wrap.classList.remove('is-updating');
      if (ts) ts.textContent = `更新于 ${nowTs()}`;
    } catch {
      clear(wrap);
      wrap.classList.remove('is-updating');
      wrap.append(el('div', { class: 'dash-empty', text: '指数数据暂不可用' }));
      if (ts) ts.textContent = '数据获取失败';
    }
  };

  void loadIndices();
  timer = window.setInterval(() => void loadIndices(), 30000);
  window.addEventListener('beforeunload', () => window.clearInterval(timer));
}
