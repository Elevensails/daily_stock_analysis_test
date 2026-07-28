import { renderFragmentView } from './fragment-view';

/** 个股分析页：渲染 report 类型片段（按 hash key，缺省取最新时段）。 */
export function renderReport(root: HTMLElement): void {
  renderFragmentView(root, { type: 'report', title: '📊 个股分析' });
}
