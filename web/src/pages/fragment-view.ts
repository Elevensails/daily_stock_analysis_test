/* ==========================================================================
 * 片段视图通用渲染：按 location.hash（片段 key）或「最新时段」渲染一个报告片段。
 * 缺失片段 → 待生成态；片段存在但为错误态 → 包裹 .error-box。
 * report / market_review / vibe 三个页面共用此逻辑，仅 type/title 不同。
 * ========================================================================== */
import { getDsa, getFragment, latestFragmentKey } from '../dsa';
import { el, clear, colorize } from '../dom';
import { pageHeader } from '../components/cards';
import { renderPending, renderError } from '../components/states';

export function renderFragmentView(
  root: HTMLElement,
  opts: { type: string; title: string }
): void {
  clear(root);
  const { manifest } = getDsa();
  const key = location.hash.replace(/^#/, '') || latestFragmentKey(manifest, opts.type) || null;

  if (!key) {
    root.append(renderPending(`${opts.title}待生成`));
    return;
  }
  const html = getFragment(key);
  if (!html) {
    root.append(renderError('未找到该报告片段', `片段 ${key} 不存在或尚未生成`));
    return;
  }
  const article = el('article', { class: 'module' });
  article.innerHTML = html;
  // 错误态标记（vibe 等骨架报告含「未能完成分析」）→ 红色包裹，杜绝裸堆栈。
  if (html.includes('未能完成分析')) article.classList.add('error-box');
  colorize(article);
  root.append(pageHeader(opts.title, key), article);
}
