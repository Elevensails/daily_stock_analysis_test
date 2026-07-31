/* ==========================================================================
 * 主题控制器：localStorage 持久化 + [data-theme] 切换。
 * 防 FOUC：各页面 <head> 内联脚本已在首屏前设置 data-theme，
 * 此处仅在用户点击切换时更新 DOM 属性与存储。
 * ========================================================================== */

const STORAGE_KEY = 'dsa-theme';

type Theme = 'light' | 'dark';

function read(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

function apply(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme);
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* 隐私模式等场景下忽略写入失败 */
  }
}

export const ThemeController = {
  /** 首屏由内联脚本完成，这里无需重复初始化。 */
  init(): void {
    /* no-op */
  },
  current(): Theme {
    return read();
  },
  set(theme: Theme): void {
    apply(theme);
  },
  toggle(): void {
    apply(read() === 'light' ? 'dark' : 'light');
  },
};
