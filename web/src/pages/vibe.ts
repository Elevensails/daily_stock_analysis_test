import { renderFragmentView } from './fragment-view';

/** 量化分析页：渲染 vibe 类型片段（错误态自动套 .error-box）。 */
export function renderVibe(root: HTMLElement): void {
  renderFragmentView(root, { type: 'vibe', title: '📈 量化分析' });
}
