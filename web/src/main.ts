/* ==========================================================================
 * 入口装配：按 <body data-page> 挂载对应渲染器，并挂载共享外壳与主题。
 * 所有页面共用本入口（MPA 每个 HTML 都引用它）。
 * ========================================================================== */
import './styles/tokens.css';
import './styles/base.css';
import './styles/states.css';

import { ThemeController } from './theme-toggle';
import { mountShell } from './shell';
import { renderHome } from './pages/home';
import { renderReport } from './pages/report';
import { renderMarketReview } from './pages/market_review';
import { renderVibe } from './pages/vibe';
import { renderArchive } from './pages/archive';
import { renderDashboard } from './pages/dashboard';

function bootstrap(): void {
  ThemeController.init();
  const app = document.getElementById('app');
  if (!app) return;
  const content = mountShell(app);
  const page = document.body.dataset.page || 'home';
  const renderers: Record<string, (root: HTMLElement) => void> = {
    home: renderHome,
    report: renderReport,
    market_review: renderMarketReview,
    vibe: renderVibe,
    archive: renderArchive,
    dashboard: renderDashboard,
  };
  (renderers[page] ?? renderHome)(content);
}

bootstrap();
