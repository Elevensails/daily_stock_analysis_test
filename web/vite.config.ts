/* ==========================================================================
 * Vite 多页（MPA）静态构建配置 —— U2 方案C。
 *  - base: './'：相对路径，使 web/ 在 dsa-test 与生产仓字节一致（双仓复用）。
 *  - injectContentPlugin：构建期读取 manifest + fragments，注入 window.__DSA__
 *    （优先 web/src/content，缺失时回退 web/mock，使 `npm run dev` 脱离 Python）。
 *  - closeBundle：将内容契约拷贝到 dist/content，供归档页运行时 fetch 历史片段。
 * ========================================================================== */
import { defineConfig, type Plugin } from 'vite';
import { resolve, dirname, join } from 'node:path';
import {
  existsSync,
  readFileSync,
  readdirSync,
  mkdirSync,
  writeFileSync,
} from 'node:fs';
import { fileURLToPath } from 'node:url';

const webDir = dirname(fileURLToPath(import.meta.url));

interface DsaData {
  manifest: unknown | null;
  fragments: Record<string, string>;
}

/** 读取内容契约：优先生成物 src/content，回退到提交进仓库的 mock。 */
function loadContent(): DsaData {
  const candidates = [join(webDir, 'src', 'content'), join(webDir, 'mock')];
  for (const dir of candidates) {
    const manifestPath = join(dir, 'manifest.json');
    if (!existsSync(manifestPath)) continue;
    try {
      const manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));
      const fragments: Record<string, string> = {};
      const fragDir = join(dir, 'fragments');
      if (existsSync(fragDir)) {
        for (const fn of readdirSync(fragDir)) {
          if (fn.endsWith('.html')) {
            fragments[fn.replace(/\.html$/, '')] = readFileSync(join(fragDir, fn), 'utf-8');
          }
        }
      }
      return { manifest, fragments };
    } catch (err) {
      console.warn(`[injectContentPlugin] 读取 ${dir} 失败:`, err);
    }
  }
  return { manifest: null, fragments: {} };
}

function injectContentPlugin(): Plugin {
  let cached: DsaData | null = null;
  const get = (): DsaData => (cached ??= loadContent());
  return {
    name: 'inject-dsa-content',
    transformIndexHtml(html: string): string {
      const data = get();
      // 将 `<` 转义为 \u003c，避免片段内容意外闭合 <script>。
      const json = JSON.stringify(data).replace(/</g, '\\u003c');
      const snippet = `<script>window.__DSA__=${json};</script>`;
      return html.replace('</head>', `${snippet}\n</head>`);
    },
    closeBundle() {
      // 拷贝内容契约到 dist/content：归档页运行时 fetch 历史片段 + deploy 读取。
      const data = get();
      if (!data.manifest) return;
      const outDir = join(webDir, 'dist', 'content');
      try {
        mkdirSync(join(outDir, 'fragments'), { recursive: true });
        writeFileSync(
          join(outDir, 'manifest.json'),
          JSON.stringify(data.manifest, null, 2),
          'utf-8'
        );
        const fragSrc = join(webDir, 'src', 'content', 'fragments');
        const src = existsSync(fragSrc) ? fragSrc : join(webDir, 'mock', 'fragments');
        if (existsSync(src)) {
          for (const fn of readdirSync(src)) {
            if (fn.endsWith('.html')) {
              writeFileSync(join(outDir, 'fragments', fn), readFileSync(join(src, fn), 'utf-8'), 'utf-8');
            }
          }
        }
      } catch (err) {
        console.warn('[injectContentPlugin] 拷贝 /content 失败（不影响主构建）:', err);
      }
    },
  };
}

export default defineConfig({
  base: './',
  plugins: [injectContentPlugin()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: resolve(webDir, 'index.html'),
        report: resolve(webDir, 'report.html'),
        market_review: resolve(webDir, 'market_review.html'),
        vibe: resolve(webDir, 'vibe.html'),
        archive: resolve(webDir, 'archive.html'),
        dashboard: resolve(webDir, 'dashboard.html'),
      },
    },
  },
});
