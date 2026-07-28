/* ==========================================================================
 * 首页：Hero + 统计卡片行 + 快捷入口 + 今日 5 时段分析（来自 manifest.slots）。
 * ========================================================================== */
import { getDsa, SLOT_ORDER } from '../dsa';
import { clear, colorize } from '../dom';
import { statCard, navCard, slotCard, pageHeader, sectionTitle } from '../components/cards';
import { renderEmpty } from '../components/states';

export function renderHome(root: HTMLElement): void {
  clear(root);
  const { manifest } = getDsa();

  if (!manifest) {
    root.append(renderEmpty('内容尚未生成，请运行分析流水线后查看。'));
    return;
  }

  root.append(
    pageHeader(
      manifest.site?.title || 'A股智能分析 · 决策仪表盘',
      `DeepSeek AI · 生成于 ${manifest.generatedAt || ''}`
    )
  );

  // 统计卡片行
  const stats = manifest.stats || {};
  const statRow = document.createElement('div');
  statRow.className = 'grid';
  statRow.append(
    statCard({ icon: '⚡', label: '时段 / 天', value: String(stats.slotsPerDay ?? 5) }),
    statCard({ icon: '💻', label: '持仓数', value: String(stats.holdings ?? 4) }),
    statCard({ icon: '📄', label: '报告类型', value: String(stats.reportTypes ?? 3) }),
    statCard({ icon: '💰', label: '月成本', value: String(stats.monthlyCost ?? '~0') })
  );
  root.append(sectionTitle('今日概览'), statRow);

  // 快捷入口
  const navRow = document.createElement('div');
  navRow.className = 'grid';
  navRow.append(
    navCard({ title: '⚡ 实时盯盘', desc: '30s 刷新 · 4 持仓 + 指数', href: 'dashboard.html', variant: 'live' }),
    navCard({ title: '🗂 历史归档', desc: '按日期浏览全部报告', href: 'archive.html', variant: 'archive' })
  );
  root.append(sectionTitle('快捷入口'), navRow);

  // 今日 5 时段分析
  const slotsWrap = document.createElement('div');
  slotsWrap.className = 'grid';
  const slots = manifest.slots || {};
  let any = false;
  for (const code of SLOT_ORDER) {
    const s = slots[code];
    if (!s) continue;
    any = true;
    slotsWrap.append(slotCard(code, s));
  }
  if (any) root.append(sectionTitle('今日 5 时段分析'), slotsWrap);

  colorize(root);
}
