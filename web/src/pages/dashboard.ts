/* ==========================================================================
 * 实时盯盘页：移植原 dashboard.html 的东方财富 push2 拉取 + 30s 刷新逻辑，
 * 套用共享外壳、设计令牌与 .up/.down 语义色（涨红跌绿）+ 主题切换。
 * ========================================================================== */
import { el, clear } from '../dom';
import { pageHeader } from '../components/cards';

const STOCKS: Record<string, string> = {
  '600036': '招商银行',
  '159915': '创业板ETF',
  '002049': '紫光国微',
  '603823': '百合花',
  '510050': '上证50ETF',
  '512400': '有色ETF',
};

function pad(n: number): string {
  return n < 10 ? '0' + n : '' + n;
}
function nowTs(): string {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes()
  )}:${pad(d.getSeconds())}`;
}
function fetchJSON(url: string): Promise<unknown> {
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open('GET', url, true);
    x.onreadystatechange = function () {
      if (x.readyState === 4) {
        try {
          resolve(x.status === 200 ? JSON.parse(x.responseText) : null);
        } catch {
          resolve(null);
        }
      }
    };
    x.send();
  });
}
function col(chg: number): string {
  return chg >= 0 ? 'up' : 'down';
}
function sign(chg: number): string {
  return chg >= 0 ? '+' : '';
}

async function fetchQuote(root: HTMLElement): Promise<void> {
  const idxGrid = root.querySelector('#idx-grid') as HTMLElement | null;
  const holdGrid = root.querySelector('#hold-grid') as HTMLElement | null;
  const sectorTop = root.querySelector('#sector-top') as HTMLElement | null;
  const inflowTop = root.querySelector('#inflow-top') as HTMLElement | null;
  const tsEl = root.querySelector('#dash-ts') as HTMLElement | null;
  if (!idxGrid || !holdGrid || !sectorTop || !inflowTop) return;

  const codes = Object.keys(STOCKS)
    .map((c) => (c.startsWith('6') ? '1' : '0') + '.' + c)
    .join(',');
  const quoteUrl = `https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids=${codes}`;
  const idxCodes = '1.000001,0.399001,0.399006';
  const idxUrl = `https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids=${idxCodes}`;
  const sectorUrl =
    'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fields=f2,f3,f12,f14&fid=f3&fs=m:90+t:2';
  const inflowUrl =
    'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fields=f12,f14,f62&fid=f62&fs=m:90+t:2';

  const [quote, idx, sector, inflow] = (await Promise.all([
    fetchJSON(quoteUrl),
    fetchJSON(idxUrl),
    fetchJSON(sectorUrl),
    fetchJSON(inflowUrl),
  ])) as any[];

  const renderCards = (container: HTMLElement, isIdx: boolean, items: any[]): void => {
    let html = '';
    for (const it of items || []) {
      const chg = Number(it.f3);
      const base = isIdx ? 'idx-card' : 'hold-card';
      const n = isIdx ? 'n' : 'hn';
      const p = isIdx ? 'p' : 'hp';
      const c = isIdx ? 'c' : 'hm';
      html += `<div class="${base}">
        <div class="${n}">${it.f14}</div>
        <div class="${p} ${col(chg)}">${Number(it.f2).toFixed(2)}</div>
        <div class="${c} ${col(chg)}">${sign(chg)}${chg.toFixed(2)}%</div>
      </div>`;
    }
    container.innerHTML = html || '<div class="dash-empty">数据加载失败</div>';
  };

  renderCards(holdGrid, false, quote?.data?.diff);
  renderCards(idxGrid, true, idx?.data?.diff);

  const sItems = (sector?.data?.diff as any[]) || [];
  let maxS = 0;
  sItems.forEach((it) => {
    const v = Math.abs(Number(it.f3));
    if (v > maxS) maxS = v;
  });
  sectorTop.innerHTML =
    sItems
      .map((it) => {
        const w = maxS > 0 ? (Math.abs(Number(it.f3)) / maxS) * 100 : 0;
        const c = Number(it.f3);
        return `<div class="bar"><span class="bn">${it.f14}</span><span class="bbar"><span class="bt" style="width:${w.toFixed(
          0
        )}%;background:${c >= 0 ? 'var(--up)' : 'var(--down)'}"></span></span><span class="bv ${col(
          c
        )}">${sign(c)}${c.toFixed(2)}%</span></div>`;
      })
      .join('') || '<div class="dash-empty">数据加载失败</div>';

  const iItems = (inflow?.data?.diff as any[]) || [];
  let maxI = 0;
  iItems.forEach((it) => {
    const v = Math.abs(Number(it.f62));
    if (v > maxI) maxI = v;
  });
  inflowTop.innerHTML =
    iItems
      .map((it) => {
        const v = Number(it.f62) / 1e8;
        const w = maxI > 0 ? (Math.abs(Number(it.f62)) / maxI) * 100 : 0;
        return `<div class="bar"><span class="bn">${it.f14}</span><span class="bbar"><span class="bt" style="width:${w.toFixed(
          0
        )}%;background:${v >= 0 ? 'var(--up)' : 'var(--down)'}"></span></span><span class="bv ${col(
          v
        )}">${sign(v)}${v.toFixed(2)}亿</span></div>`;
      })
      .join('') || '<div class="dash-empty">数据加载失败</div>';

  if (tsEl) tsEl.textContent = `更新时间: ${nowTs()} · 30 秒后自动刷新`;
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

  const run = (): void => {
    void fetchQuote(root).catch(() => {});
  };
  run();
  const timer = window.setInterval(run, 30000);
  window.addEventListener('beforeunload', () => window.clearInterval(timer));
}
