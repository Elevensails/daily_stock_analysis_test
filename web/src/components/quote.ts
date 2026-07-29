/* ==========================================================================
 * 实时行情公共能力：fetchJSON（带超时与错误处理）+ 主要指数实时拉取与卡片渲染。
 * 被 dashboard（实时盯盘）与 home（首页今日概览）复用，避免重复实现与漂移。
 *
 * 关键数据约定（东方财富 push2 API）：
 *  - f2（最新价）= 真实价格 ×100 的整型，渲染时需 /100；
 *  - f3（涨跌幅）= 真实百分比 ×100 的整型，渲染时需 /100；
 *  - f62（主力净流入金额）单位为「元」，展示时 /1e8 转「亿」，无需 /100。
 * ========================================================================== */
import { el } from '../dom';

/** 单只指数实时行情。 */
export interface IndexQuote {
  /** 指数名称（如「上证指数」）。 */
  name: string;
  /** 最新价（已 /100）。 */
  price: number;
  /** 涨跌幅百分比（已 /100，带符号语义）。 */
  changePct: number;
}

/** 主要指数代码（上证指数 / 深证成指 / 创业板指）。 */
export const DEFAULT_INDEX_CODES = '1.000001,0.399001,0.399006';

/**
 * 带超时与错误处理的 JSON 拉取（基于 XMLHttpRequest，兼容 file:// 与跨域 CORS）。
 * 超时默认 8 秒；网络错误 / 解析失败 / HTTP 非 200 均以 rejected Promise 暴露，
 * 便于调用方做独立的错误态与重试处理。
 */
export function fetchJSON(url: string, timeoutMs = 8000): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    let settled = false;
    const timer = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      x.abort();
      reject(new Error('请求超时，请稍后重试'));
    }, timeoutMs);

    const settle = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      fn();
    };

    x.open('GET', url, true);
    x.onreadystatechange = () => {
      if (x.readyState !== 4 || settled) return;
      if (x.status === 200) {
        try {
          settle(() => resolve(JSON.parse(x.responseText)));
        } catch {
          settle(() => reject(new Error('数据解析失败，请稍后重试')));
        }
      } else {
        settle(() => reject(new Error(`数据拉取失败（HTTP ${x.status}）`)));
      }
    };
    x.onerror = () => settle(() => reject(new Error('网络异常，请检查网络后重试')));
    x.send();
  });
}

/**
 * 拉取主要指数实时行情（默认上证 / 深证成指 / 创业板指）。
 * 已按 push2 约定对 f2/f3 做 /100 换算。
 */
export async function fetchMarketIndices(codes: string = DEFAULT_INDEX_CODES): Promise<IndexQuote[]> {
  const url = `https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids=${codes}`;
  const data = (await fetchJSON(url)) as { data?: { diff?: unknown } } | null;
  const diff = data?.data?.diff;
  if (!Array.isArray(diff)) {
    throw new Error('指数数据暂不可用');
  }
  return (diff as Record<string, unknown>[]).map((it) => ({
    name: String(it.f14 ?? ''),
    price: Number(it.f2) / 100,
    changePct: Number(it.f3) / 100,
  }));
}

/** 渲染单张指数卡片（涨红跌绿）。 */
export function indexCardEl(q: IndexQuote): HTMLElement {
  const up = q.changePct >= 0;
  return el('div', { class: 'idx-card' }, [
    el('div', { class: 'n', text: q.name }),
    el('div', { class: `p ${up ? 'up' : 'down'}`, text: q.price.toFixed(2) }),
    el('div', { class: `c ${up ? 'up' : 'down'}`, text: `${up ? '+' : ''}${q.changePct.toFixed(2)}%` }),
  ]);
}

/** 当前时间戳字符串（HH:MM:SS）。 */
export function nowTs(): string {
  const pad = (n: number): string => (n < 10 ? '0' + n : '' + n);
  const d = new Date();
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
