# 增量 PRD：P0 第一批 — T01/U1 工作流连续性对齐与错误降级 + T04/U4 HTML 报告 XSS 安全渲染

> **文档类型**：增量 PRD（仅描述 T01 + T04 的变更，不含 U1–U18 其余项）
> **作者**：许清楚（software-product-manager）
> **日期**：2026-07-27
> **仓库边界**：`Elevensails/daily_stock_analysis_test`（dsa-test 升级演进线，本地副本 `projects/dsa-test`）
> **溯源键**：T01 ↔ U1 ↔ TECH-T02；T04 ↔ U4 ↔ TECH-T01
> **上游依据**：`roadmap/prd_u1_u18_20260726.md`、`benchmarks/benchmark_arch_20260726.md`，以及 dsa-test 现行代码核查（见 §0）
> **状态**：规划冻结待实现（本文档不修改任何代码）

---

## 0. 代码现状基线（已核查，纠正知识型描述偏差）

参考文档中的 `analyze.py` / `market_review.py` / `report_generator.py` 等为**知识型描述**；dsa-test 实际已重构为分层结构，关键落点如下（行号基于 2026-07-27 工作副本）：

| 能力 | 真实位置 |
|------|----------|
| Agent 分析入口 | `src/core/pipeline.py::_analyze_with_agent`（L1294） |
| 单股处理/编排 | `src/core/pipeline.py::process_single_stock`（L3003）、`analyze_stock`（L383） |
| 跨时段个股结论 `StockSlotConclusion` | `src/services/stock_continuity.py`（已实现、已对不可信文本做 `_escape_braces`） |
| 连续性加载（orchestration 层一次性） | `src/core/pipeline.py::run` 内 `load_previous_slot_stock_conclusions`（L3172） |
| 报告→HTML 部署（XSS 风险点） | `scripts/deploy_pages.py::md2html`（L120–149）、`make_report_page`（L241） |
| 另一条渲染路径（非 GH Pages） | `src/services/report_renderer.py`（Jinja2，`autoescape=select_autoescape(default=False)`，L256） |
| 密钥脱敏（与 XSS 无关） | `src/utils/sanitize.py` |

**T01 缺口（已确认）**：连续性字典在编排层（L3172）加载，经 `process_single_stock`(L3210) → `analyze_stock`(L389, L3071) 透传至 legacy `_analyze`（L690–691 注入 `enhanced_context`），**但在 agent 分支 `analyze_stock`→`_analyze_with_agent` 调用处（L563–578）未转发该字典，且 `_analyze_with_agent` 签名(L1294)也无此参数** → agent 路径完全不注入 `StockSlotConclusion`，与前一时段/前一日结论断链（fail-open）。

**T04 缺口（已确认）**：`md2html()` 将模型生成的 markdown 直接 f-string 注入 HTML，**所有模型文本节点均未 `html.escape`**（表格 `{c}` L127/L130、`{stock_title}` L137、`<h1>/<h3>/<blockquote>/<li>/<p>` L139–146）；`make_report_page` 的 `title` 裸注入 `<title>{title}</title>`（L245）。同一文件的 `extract_preview()` 已做 `html.escape`（L337），证明团队知晓该 sink、`md2html` 被遗漏。

---

## 1. 产品目标

消除 agent 分析路径相对于 legacy 路径的「连续性断链」与报告流水线的「未转义 XSS 注入」两处 P0 级正确性/安全隐患，使每日（GitHub Actions `daily-analysis-v2.yml` / `vibe-quant.yml`）产出的报告前后日结论自洽、且公开页对任意模型输出安全渲染。

---

## 2. 用户故事

**T01 / U1（工作流连续性对齐与错误降级）**
- 作为持仓用户，我希望无论 legacy 还是 agent 模式产出的报告，我的个股历史结论都能被延续引用，以便前后日分析不自相矛盾。
- 作为运维/SRE，我希望连续性注入在子系统异常时显式告警并降级而非静默丢空，以便及时排查、不会在无人察觉下产出「断链」报告。

**T04 / U4（HTML 报告 XSS 安全渲染）**
- 作为站点访问者，我希望公开报告页不会执行模型输出的任意 `<script>`/`<img onerror>` 等脚本，以便浏览安全、不被注入攻击。
- 作为合规负责人，我希望部署前的自动化测试能拦截未转义注入，以便安全合规底线可验证、可回归。

---

## 3. T01 / U1 — 工作流连续性对齐与错误降级

### 3.1 需求详述

1. **对齐注入**：在 `analyze_stock` 的 agent 分支（L563–578）将 `previous_slot_stock_conclusions` 转发给 `_analyze_with_agent`；`_analyze_with_agent` 新增该参数，按 `code` 取出对应 `StockSlotConclusion`，经既有 `format_previous_slot_stock_conclusions_prompt_section(...)`（已对不可信文本做 brace 转义）渲染后，注入 agent 的 `initial_context`（键名与 legacy 的 `previous_slot_stock_conclusions` 对齐，供 agent executor/orchestrator 在 prompt 中消费）。注入逻辑与 legacy 路径（L690–691）保持一致。
2. **错误降级（graceful degradation）**：连续性加载/渲染须 fail-soft——仅「模型调用级」异常允许硬失败（维持现有 L1433 `raise`）；连续性注入本身（字典缺失、单标的无历史、渲染异常）须 `try/except` 包裹，失败时 `logger.warning("[WARN] continuity fallback: ...")` 并继续以空结论段分析，**不得静默丢空、不得中断整只股票分析**。与既有降级范式（L460 实时行情降级、L531 快照写失败）风格一致。
3. **文档补全**：`_analyze_with_agent` 及其连续性封装 100% 补全 docstring，显式说明与 legacy 路径在连续性注入上的对齐点（fail-soft 语义、注入键名、空值处理）。
4. **依赖复用**：直接复用已落地的 `src/services/stock_continuity.py`（`StockSlotConclusion` / `load_previous_slot_stock_conclusions` / `format_..._prompt_section`），**不重复造轮子、不改该模块既有契约**（仅消费方变更）。

### 3.2 验收标准（可量化）

1. **注入对齐**：`_analyze_with_agent` 在 agent 路径下注入个股跨时段结论；构造一只 `previous_slot_stock_conclusions[code]` 非空的标的走 agent 分析路径（executor 以 mock 替代，避免真实 DeepSeek 调用），断言最终 prompt/上下文含该标的跨时段结论文本（如 `上一时段个股结论`、`持有/观望` 等既有结论字段）。
2. **错误降级可观测**：模拟连续性渲染/取值异常的分支，断言日志出现 `[WARN] continuity fallback` 字样，且分析流程未中断、报告（若产出）结论文本源自缓存（非空），**非静默丢空**。
3. **文档**：`_analyze_with_agent` 与其连续性封装函数 docstring 经人工/静态检查 100% 补全，含与 legacy 的对齐说明。
4. **无回归**：既有 `tests/test_continuity.py`（legacy/stock_continuity 渲染与防幻觉）全量通过；新增 `tests/test_agent_continuity.py`（见 §5）通过；`ci.yml` 的 `offline-tests` 门禁（`./scripts/ci_gate.sh offline-tests`）通过。
5. **范围控制**：改动仅限 `src/core/pipeline.py` 单文件 + 新增测试；`src/services/stock_continuity.py` 保持只读复用。

---

## 4. T04 / U4 — HTML 报告 XSS 安全渲染

### 4.1 需求详述

1. **转义所有模型注入文本**：`scripts/deploy_pages.py` 中凡将模型/外部文本注入 HTML 处，先经 `html.escape(...)` 再 f-string 拼接。具体最小改动集：
   - `md2html()`（L120–149）：表格单元格 `{c}`（L127/L130）、`{stock_title}`（L137）、`<h1>/<h3>/<blockquote>/<li>` 与 `<p>` 中的行文本（L139–146）均包裹 `html.escape`。
   - `make_report_page()`（L241–250）：报告 `title`（L243/L245）包裹 `html.escape`。
   - 说明：`html.escape` 仅转义 `< > & " '`，不影响 markdown 强调（`**bold**` 的 `*` 不被转义），且我方插入的 `<strong>/<td>` 等标签由代码生成、非模型文本，故合法排版与表格不受影响。
2. **可测性重构（建议）**：将 `make_report_page` 的「纯 HTML 拼装」与 `gh_put` 网络上传拆开（或导出纯函数 `build_report_html(...)`），使 XSS 测试可离线运行，无需 `GITHUB_TOKEN`/网络。
3. **CI 门禁**：新增 XSS 测试纳入既有 offline 套件（`ci.yml` → `backend-gate` → `offline-tests`），并在两个 deploy workflow 的 `Deploy to GH Pages` 步骤**之前**加显式 step 跑该测试，失败即阻断 deploy。
4. **范围与关联**：本期仅覆盖 GH Pages 部署路径 `scripts/deploy_pages.py`。另一条 `src/services/report_renderer.py`（Jinja2 `autoescape=select_autoescape(default=False)`，L256）及 `templates/report_*.j2` 用于 webapp/API 报告，属独立渲染路径，**本期不改动**（见 §6 待确认 Q2）。

### 4.2 验收标准（可量化）

1. **零未转义注入**：`scripts/deploy_pages.py` 中对模型注入文本全部先 `html.escape`；静态核查（grep / 人工 review）确认脚本中再无以 `{模型文本}` 形式裸注入 HTML 的 f-string（除我方常量/文件名等可信值外）。
2. **XSS 测试**：新增 `tests/test_xss_escape.py`，构造含 `<script>alert(1)</script>` 与 `<img src=x onerror=alert(1)>` 的 payload 作为「模型输出」，跑完整生成流程（优先直接测 `md2html` 及拆出的纯拼装函数，离线），断言产物 HTML 中该内容被转义为 `&lt;script&gt;...` / `&lt;img ...&gt;`，且不含可执行的原始标签；浏览器语义下不执行。
3. **CI 阻断**：`daily-analysis-v2.yml` 与 `vibe-quant.yml` 在 `Deploy to GH Pages` 前新增 XSS step（`python3 -m pytest tests/test_xss_escape.py -q`），该 step 失败则 workflow 失败、deploy 不执行；`ci.yml` 的 `offline-tests` 亦覆盖该测试。
4. **回归不破坏**：4 只持仓（600036 / 159915 / 603823 / 512400）+ 指数报告生成非空、结构与历史产物一致（合法 markdown 排版——加粗、表格、标题——渲染正常，未被转义破坏）；`tests/test_report_renderer.py` 等既有测试无回归。

---

## 5. 受影响的文件清单（相对 dsa-test 仓库根目录）

### T01 / U1
| 文件 | 变更 | 说明 |
|------|------|------|
| `src/core/pipeline.py` | **MODIFY** | `analyze_stock`(L563–578) 转发 `previous_slot_stock_conclusions` 至 `_analyze_with_agent`；`_analyze_with_agent`(L1294) 新增参数并注入 `initial_context`；连续性加载/渲染加 try/except + `[WARN] continuity fallback`；补全 docstring。 |
| `tests/test_agent_continuity.py` | **NEW** | agent 路径连续性注入断言 + 降级告警断言（mock executor，离线）。 |
| `src/services/stock_continuity.py` | 不改（只读复用） | `StockSlotConclusion` / `format_..._prompt_section` 已具备，仅消费。 |

### T04 / U4
| 文件 | 变更 | 说明 |
|------|------|------|
| `scripts/deploy_pages.py` | **MODIFY** | `md2html`(L120–149) 与 `make_report_page`(L241–250) 对模型文本 `html.escape`；建议拆分纯拼装函数以便离线测试。 |
| `tests/test_xss_escape.py` | **NEW** | XSS payload 转义断言（离线）。 |
| `.github/workflows/daily-analysis-v2.yml` | **MODIFY** | `Deploy to GH Pages` 步骤前新增 XSS 测试 step。 |
| `.github/workflows/vibe-quant.yml` | **MODIFY** | 同上。 |

### 关联但本期不改动（透明披露）
- `src/services/report_renderer.py`（Jinja2 `autoescape=False`，L256）+ `templates/report_*.j2`：独立 webapp/API 渲染路径，是否纳入 T04 待 §6 Q2 确认。
- `src/utils/sanitize.py`：密钥脱敏用途，与 XSS 无关，不改动。

---

## 6. 待确认问题（Open Questions）

1. **T01 表述对齐**：本轮将「GitHub Actions 工作流连续性对齐与错误降级」对齐到路线图的 T01/U1（agent 路径连续性 fail-open 修复 + fail-soft 降级）。若主理人原意还包含「deploy workflow 自身的跨运行状态持久化/失败重试」等独立诉求，请补充，以免遗漏。
2. **T04 渲染范围**：PRD T04（及 benchmark TECH-T01）范围明确为 `scripts/deploy_pages.py`（GH Pages 路径）。但 `src/services/report_renderer.py` 的 Jinja2 环境 `autoescape=select_autoescape(default=False)`（L256，`*.j2` 默认不转义）对 webapp/API 报告同样存在潜在注入面。是否将 Jinja2 改为 `autoescape=True`（或 `select_autoescape(['html','xml','j2'])`）纳入本期 T04，还是单列 follow-up？建议本期保持 scope 不变、另开 follow-up 项。
3. **XSS 静态门禁强度**：验收 #1 的「grep 确认无裸注入」建议由实现者在 PR 中提供 grep 证据；是否需要在 `scripts/ci_gate.sh` 增加一条针对 `deploy_pages.py` 的禁止裸注入的 lint 规则（强约束）？
4. **CSP 头（可选）**：benchmark TECH-T01 建议加 `Content-Security-Policy`。PRD T04 验收未强制 CSP；是否作为本期「建议项/可选」追加，还是留给后续安全加固专项？

---

## 7. 与路线图/架构对齐 & 风险提示

- **优先级/排期**：T01 + T04 均为 P0 首批，可并行（见 `prd_u1_u18_20260726.md` §3.1），工作量均为 S（<3 人日），低风险高收益，符合「先动手」建议。
- **溯源一致**：T01=U1=TECH-T02；T04=U4=TECH-T01；本文档与 `benchmark_arch_20260726.md` 经溯源键对齐。
- **风险**：T01 连续性上下文增长或推高 token（benchmark 标注 TECH-T02 风险「中」，但本期仅「复用既有已加载字典 + 注入」，不新增读取，token 增量可忽略）；T04 转义须确保不破坏合法 markdown 排版（验收 #4 已覆盖）。
- **不做的事**：不改动 `stock_continuity.py` 契约、不引入新依赖（XSS 仅用标准库 `html`）、不触碰 U2–U3/U5–U18。
