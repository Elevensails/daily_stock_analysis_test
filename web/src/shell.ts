/* ==========================================================================
 * 共享外壳：顶部导航（含主题切换按钮）+ 主内容容器 + 页脚。
 * 所有页面共用，保证设计系统一致性与链接完整性。
 * ========================================================================== */
import { el } from './dom';
import { ThemeController } from './theme-toggle';

const NAV: { href: string; label: string }[] = [
  { href: 'index.html', label: '首页' },
  { href: 'report.html', label: '个股分析' },
  { href: 'market_review.html', label: '大盘复盘' },
  { href: 'vibe.html', label: '量化分析' },
  { href: 'dashboard.html', label: '实时盯盘' },
  { href: 'archive.html', label: '历史归档' },
];

/**
 * 在 root 内挂载共享外壳，返回主内容容器（页面渲染器往里填充）。
 */
export function mountShell(root: HTMLElement): HTMLElement {
  root.replaceChildren();

  const navLinks = NAV.map((i) => el('a', { class: 'nav__link', href: i.href, text: i.label }));
  const toggle = el('button', {
    class: 'nav__toggle',
    id: 'theme-toggle',
    title: '切换主题',
    text: '🌓',
  });

  const header = el('header', { class: 'site-header' }, [
    el('div', { class: 'site-header__inner' }, [
      el('a', { class: 'brand', href: 'index.html', text: '📊 A股智能分析' }),
      el('nav', { class: 'nav' }, [...navLinks, toggle]),
    ]),
  ]);

  const main = el('main', { class: 'site-main', id: 'page-content' });

  const footer = el('footer', { class: 'site-footer' }, [
    el('div', { html: '以上分析基于公开数据，<strong>不构成投资建议</strong>' }),
    el('div', {
      class: 'site-footer__sub',
      text: 'Fork 自 daily_stock_analysis · U2 方案C 内容排版彻底分离',
    }),
  ]);

  root.append(header, main, footer);
  toggle.addEventListener('click', () => ThemeController.toggle());
  return main;
}
