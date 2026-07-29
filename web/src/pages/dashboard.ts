/* ==========================================================================
 * 实时盯盘页：移植原 dashboard.html 的东方财富 push2 拉取 + 30s 刷新逻辑，
 * 套用共享外壳、设计令牌与 .up/.down 语义色（涨红跌绿）+ 主题切换。
 *
 * 设计要点（BugFix）：
 *  - f2/f3 已按 push2 约定 ÷100（价格/涨跌幅真实值）；
 *  - fetchJSON 带 8s 超时与错误处理；
 *  - 首次加载显示骨架屏；失败时显示具体错误 + 重试按钮；
 *  - 各板块（指数 / 持仓 / 板块涨幅 / 主力净流入）独立加载态，单一接口失败不影响其余；
 *  - 30s 自动刷新期间显示「更新中...」，避免用户误以为卡住。
 * ========================================================================== */
import { el, clear } from '../dom';
import { pageHeader } from '../components/cards';
import { fetchJSON, nowTs } from '../components/quote';

/** 持仓标的（代码 -> 名称）。 */
const STOCKS: Record<string, string> = {
  '600036': '招商银行',
  '159915': '创业板ETF',
  '002049': '紫光国微',
  '603823': '百合花',
  '510050': '上证50ETF',
  '512400': '有色ETF',
};

/** push2 接口地址（f2/f3 为 ×100 整型，渲染时已 ÷100）。 */
const IDX_URL =
  'https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids=1.000001,0.399001,0.399006';
const QUOTE_CODES = Object.keys(STOCKS)
  .map((c) => (c.startsWith('6') ? '1' : '0') + '.' + c)
  .join(',');
const QUOTE_URL = `https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids=${QUOTE_CODES}`;
const SECTOR_URL =
  'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fields=f2,f3,f12,f14&fid=f3&fs=m:90+t:2';
const INFLOW_URL =
  'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fields=f12,f14,f62&fid=f62&fs=m:90+t:2';

interface ApiItem {
  f2?: string | number;
  f3?: string | number;
  f12?: string;
  f14?: string;
  f62?: string | number;
}

/** 渲染指数/持仓卡片（DOM 构建，textContent 注入，天然 XSS 安全）。 */
function renderQuoteCards(container: HTMLElement, items: ApiItem[] | null | undefined, isIdx: boolean): void {
  clear(container);
  if (!items || items.length === 0) {
    container.append(el('div', { class: 'dash-empty', text: '暂无数据' }));
    return;
  }
  const base = isIdx ? 'idx-card' : 'hold-card';
  const nCls = isIdx ? 'n' : 'hn';
  const pCls = isIdx ? 'p' : 'hp';
  const cCls = isIdx ? 'c' : 'hm';
  for (const it of items) {
    const price = Number(it.f2) / 100;
    const chg = Number(it.f3) / 100;
    const up = chg >= 0;
    container.append(
      el('div', { class: base }, [
        el('div', { class: nCls, text: String(it.f14 ?? '') }),
        el('div', { class: `${pCls} ${up ? 'up' : 'down'}`, text: price.toFixed(2) }),
        el('div', { class: `${cCls} ${up ? 'up' : 'down'}`, text: `${up ? '+' : ''}${chg.toFixed(2)}%` }),
      ])
    );
  }
}

/** 渲染板块涨幅 / 主力净流入的条形列表（DOM 构建，textContent 注入）。 */
function renderBarList(
  container: HTMLElement,
  items: ApiItem[] | null | undefined,
  mode: 'sector' | 'inflow'
): void {
  clear(container);
  if (!items || items.length === 0) {
    container.append(el('div', { class: 'dash-empty', text: '暂无数据' }));
    return;
  }
  let max = 0;
  for (const it of items) {
    const abs = Math.abs(mode === 'sector' ? Number(it.f3) : Number(it.f62));
    if (abs > max) max = abs;
  }
  if (max === 0) {
    container.append(el('div', { class: 'dash-empty', text: '暂无数据' }));
    return;
  }
  for (const it of items) {
    let value: number;
    let display: string;
    if (mode === 'sector') {
      value = Number(it.f3) / 100; // 涨跌幅（真实百分比）
      display = `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
    } else {
      value = Number(it.f62) / 1e8; // 主力净流入（亿元）
      display = `${value >= 0 ? '+' : ''}${value.toFixed(2)}亿`;
    }
    const w = Math.min(100, (Math.abs(mode === 'sector' ? Number(it.f3) : Number(it.f62)) / max) * 100);
    const up = value >= 0;
    container.append(
      el('div', { class: 'bar' }, [
        el('span', { class: 'bn', text: String(it.f14 ?? '') }),
        el('span', { class: 'bbar' }, [
          el('span', {
            class: 'bt',
            attrs: { style: `width:${w.toFixed(0)}%;background:${up ? 'var(--up)' : 'var(--down)'}` },
          }),
        ]),
        el('span', { class: `bv ${up ? 'up' : 'down'}`, text: display }),
      ])
    );
  }
}

/** 骨架屏占位（首次加载）。 */
function showSkeleton(container: HTMLElement, kind: 'card' | 'bar', count = 4): void {
  clear(container);
  for (let i = 0; i < count; i++) {
    container.append(el('div', { class: kind === 'card' ? 'skeleton-card' : 'skeleton-bar' }));
  }
}

/** 错误态 + 重试按钮（调用方提供重试回调）。 */
function showError(container: HTMLElement, message: string, onRetry: () => void): void {
  clear(container);
  container.append(
    el('div', { class: 'state-box state-box--error' }, [
      el('div', { class: 'state-box__icon', text: '⚠️' }),
      el('div', { class: 'state-box__title', text: message }),
      el('button', { class: 'retry-btn', text: '重试', onClick: onRetry }),
    ])
  );
}

/**
 * 加载单个板块：首屏显示骨架屏；刷新阶段进入「更新中」态（保留既有内容），
 * 成功后渲染，失败则渲染错误态 + 重试。返回 Promise 便于聚合 allSettled。
 */
function loadSection(
  container: HTMLElement,
  fetchFn: () => Promise<unknown>,
  renderFn: (data: any) => void,
  opts: { initial: boolean; onRetry: () => void }
): Promise<void> {
  const isInitial = opts.initial && !container.dataset.loaded;
  if (isInitial) {
    showSkeleton(container, container.id === 'sector-top' || container.id === 'inflow-top' ? 'bar' : 'card');
    container.dataset.loaded = '1';
  } else {
    container.classList.add('is-updating');
  }
  return fetchFn()
    .then((data) => {
      renderFn(data);
      container.dataset.loaded = '1';
      container.classList.remove('is-updating');
    })
    .catch((err: Error) => {
      container.classList.remove('is-updating');
      showError(container, err?.message || '数据拉取失败，请检查网络', opts.onRetry);
    });
}

export function renderDashboard(root: HTMLElement): void {
  clear(root);
  root.append(pageHeader('⚡ A股实时盯盘', '数据源：东方财富 · 30 秒自动刷新'));

  const grid = el('div', { class: 'dash-grid' }, [
    el('div', { class: 'dash-module dash-module--half' }, [
      el('h2', { text: '📈 指数实时' }),
      el('div', { class: 'idx-grid', id: 'idx-grid', text: '加载中...' }),
    ]),
    el('div', { class: 'dash-module dash-module--half' }, [
      el('h2', { text: '💼 持仓快照' }),
      el('div', { class: 'hold-grid', id: 'hold-grid', text: '加载中...' }),
    ]),
    el('div', { class: 'dash-module' }, [
      el('h2', { text: '🔥 板块涨幅 Top' }),
      el('div', { id: 'sector-top', text: '加载中...' }),
    ]),
    el('div', { class: 'dash-module' }, [
      el('h2', { text: '💰 主力净流入 Top' }),
      el('div', { id: 'inflow-top', text: '加载中...' }),
    ]),
  ]);
  root.append(grid);
  root.append(el('div', { class: 'dash-empty', id: 'dash-ts', text: '加载中...' }));

  const idxGrid = root.querySelector('#idx-grid') as HTMLElement | null;
  const holdGrid = root.querySelector('#hold-grid') as HTMLElement | null;
  const sectorTop = root.querySelector('#sector-top') as HTMLElement | null;
  const inflowTop = root.querySelector('#inflow-top') as HTMLElement | null;
  const tsEl = root.querySelector('#dash-ts') as HTMLElement | null;
  if (!idxGrid || !holdGrid || !sectorTop || !inflowTop) return;

  const loadIdx = (initial: boolean): Promise<void> =>
    loadSection(
      idxGrid,
      () => fetchJSON(IDX_URL),
      (d) => renderQuoteCards(idxGrid, (d as any)?.data?.diff, true),
      { initial, onRetry: () => void loadIdx(false) }
    );
  const loadHold = (initial: boolean): Promise<void> =>
    loadSection(
      holdGrid,
      () => fetchJSON(QUOTE_URL),
      (d) => renderQuoteCards(holdGrid, (d as any)?.data?.diff, false),
      { initial, onRetry: () => void loadHold(false) }
    );
  const loadSector = (initial: boolean): Promise<void> =>
    loadSection(
      sectorTop,
      () => fetchJSON(SECTOR_URL),
      (d) => renderBarList(sectorTop, (d as any)?.data?.diff, 'sector'),
      { initial, onRetry: () => void loadSector(false) }
    );
  const loadInflow = (initial: boolean): Promise<void> =>
    loadSection(
      inflowTop,
      () => fetchJSON(INFLOW_URL),
      (d) => renderBarList(inflowTop, (d as any)?.data?.diff, 'inflow'),
      { initial, onRetry: () => void loadInflow(false) }
    );

  const refreshAll = async (initial: boolean): Promise<void> => {
    if (!initial && tsEl) tsEl.textContent = '更新中...';
    await Promise.allSettled([loadIdx(initial), loadHold(initial), loadSector(initial), loadInflow(initial)]);
    if (tsEl) tsEl.textContent = `更新时间: ${nowTs()} · 30 秒后自动刷新`;
  };

  void refreshAll(true);
  const timer = window.setInterval(() => void refreshAll(false), 30000);
  window.addEventListener('beforeunload', () => window.clearInterval(timer));
}
