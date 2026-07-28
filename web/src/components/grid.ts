/* ==========================================================================
 * 栅格工具：12 列响应式栅格容器（列跨度由卡片自身 col-* class 控制）。
 * 响应式回落在 base.css 中统一处理（≤640px 自动单列，保证 ≤480px 无溢出）。
 * ========================================================================== */
import { el } from '../dom';

/** 创建 12 列栅格容器并追加子元素。 */
export function grid(children: HTMLElement[], extraClass = ''): HTMLElement {
  return el('div', { class: `grid ${extraClass}`.trim() }, children);
}
