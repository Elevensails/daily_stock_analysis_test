# web/mock —— 前端独立开发假数据

`npm run dev` 可不依赖 Python 流水线直接预览全部页面。本目录提交进仓库，
由 `vite.config.ts` 的 `injectContentPlugin` 在 `web/src/content` 缺失时自动回退读取。

## 文件约定

- `manifest.json`：内容契约（schema 见 `U2-architecture.md` §2.1）。
- `fragments/<type>_<HHMM>_<YYYYMMDD>.html`：单个报告**正文片段**（不含外壳/导航），
  即 `md2html()` 的渲染产物。key = 去掉 `.html` 的文件名。

## 片段样例

| 文件 | 说明 |
|------|------|
| `report_0900_20250728.html` | 早盘个股分析（部分时段） |
| `report_1800_20250728.html` | 收盘个股分析（4 只持仓） |
| `market_review_1800_20250728.html` | 大盘复盘 |
| `vibe_1800_20250728.html` | 量化分析（正常态） |
| `vibe_1800_20250728_error.html` | 量化分析（错误态样例，含「未能完成分析」标记） |

## 更新方式

当真实 `reports/*.md` 的结构（md2html 输出 class）发生变化时，同步更新此处样例，
避免前端开发态与生产态漂移。新增内容模块：在 `manifest.fragmentTypes` 追加类型，
并补一个 `<newtype>_*.html` 片段即可。
