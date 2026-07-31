/* ==========================================================================
 * 空 / 待生成 / 错误 状态组件（P0-4：友好化，绝不裸露 Traceback）。
 * 样式见 styles/states.css。
 * ========================================================================== */
import { el } from '../dom';

/** 待生成态（分析流水线尚未产出）。 */
export function renderPending(text = '待生成'): HTMLElement {
  return el('div', { class: 'state-box state-box--pending' }, [
    el('div', { class: 'state-box__icon', text: '⏳' }),
    el('div', { class: 'state-box__title', text }),
    el('div', {
      class: 'state-box__desc',
      text: '分析流水线尚未产出该内容，稍后自动刷新。',
    }),
  ]);
}

/** 空数据态。 */
export function renderEmpty(text = '暂无数据'): HTMLElement {
  return el('div', { class: 'state-box' }, [
    el('div', { class: 'state-box__icon', text: '📭' }),
    el('div', { class: 'state-box__title', text }),
    el('div', { class: 'state-box__desc', text: '当前没有可展示的数据。' }),
  ]);
}

/** 错误态（生成失败）：title 红字提示 + 可选 detail（绝不裸堆栈）。 */
export function renderError(title = '生成失败', detail?: string): HTMLElement {
  const box = el('div', { class: 'state-box state-box--error' }, [
    el('div', { class: 'state-box__icon', text: '⚠️' }),
    el('div', { class: 'state-box__title', text: title }),
  ]);
  if (detail) box.append(el('div', { class: 'state-box__detail', text: detail }));
  return box;
}
