import { renderFragmentView } from './fragment-view';

/** 大盘复盘页：渲染 market_review 类型片段。 */
export function renderMarketReview(root: HTMLElement): void {
  renderFragmentView(root, { type: 'market_review', title: '🌎 大盘复盘' });
}
