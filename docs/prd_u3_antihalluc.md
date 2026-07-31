# PRD（增量）— U3 升级：防幻觉闭环

> 范围：仅描述 U3 从 **reject-only → repair+grounding** 的**变更部分**。不重做已落地的 U3 校验 gate。
> 升级主线仓：`Elevensails/daily_stock_analysis_test`（master）；本地：`dsa-test/`。

## 0. 现状与变更边界

| 项 | 现状（U3 已落地） | 本轮增量 |
|---|---|---|
| 校验核心 | `src/core/validator.py`：`gate_report()` 返回 `ValidationResult(passed, score, reasons, checks)` | 主逻辑**不变**；仅 ③ 预留/可选增强 |
| 拦截点 | `scripts/emit_frontend_artifacts.py` line125–138：`not passed → continue` 直接丢弃 | **改为 repair loop**（fail 不丢，先修） |
| 行情侧车 | `_load_source_facts(f)` 读 `<stem>.facts.json`（含 `is_limit_up/is_limit_down/price`） | 作为 rewrite 的 grounding 上下文复用 |

**不在本轮**：U16 配置模块化、P1 其他项。
**本轮主线**：② 事后 repair loop；① grounding 改造点与 ③ LLM-judge 接口作为设计项随附。

## 1. 变更摘要

U3 事后校验从「拦截即丢弃（reject-only）」升级为「拦截后自动修复（repair）→ 再校验 → 通过则发布」的防幻觉闭环，并把真实行情 `source_facts` 同时用于事前 grounding 约束与事后重写，从源头与末端两端压制幻觉。本轮首要实现 ② repair loop，①/③ 以设计点或预留接口形式纳入。

## 2. 用户故事

| # | 角色 | 诉求 | 验收 |
|---|---|---|---|
| US1 | 系统运营者 | gate 判定 fail 的报告不要被直接丢弃，应尝试自动修掉幻觉后再发布 | 真实拦截过的 2 份样本（方向写反/红线）经 repair 后 `passed=True` 并正常 emit |
| US2 | 读者 | 发布到 GitHub Pages 的报告不得含买入建议/价位等红线、不得方向写反 | 修复后 `red_line` 与 `self_consistency` 检查必通过；红线文案必被移除 |
| US3 | 运营者 | 修不好的报告仍能安全兜住，且不因模型故障崩掉流水线 | 超轮数仍 fail → 回退 reject + 写日志；模型不可用 → 安全降级，不抛异常、不阻断其他报告 |

## 3. 需求池

### P0 — repair loop 核心（本轮必做）

- **P0-1 repair loop 主流程**：拦截点 `not passed` 时不 `continue`，进入修复循环：
  1. 提取违规段与原因（来自 `ValidationResult.reasons` / 各 `CheckResult.detail`）；
  2. 构造修正 prompt（原报告全文 + 违规反馈 + 可用 `source_facts` 行情上下文），调用模型重写违规部分；
  3. 重写后**重新跑完整 `gate_report`**（红线/一致性/不可能数值/未举证 + 可选数字核对/LLM judge，**不得绕过任何一项**）；
  4. 通过 → emit 发布；仍 fail → 进入下一轮，最多 `N` 轮；超过 `N` 轮仍 fail → 回退 reject（当前行为）+ 写 jsonl。
- **P0-2 轮数上限可配置**：新增环境变量 `REPAIR_MAX_ROUNDS`（默认建议 `2`，上限不超过 `3`），经配置注入，不写死。
- **P0-3 安全降级**：rewrite 模型调用必须 `try/except` 包裹；超时/无网/调用抛错 → 立即退出循环、回退 reject，**不得让整条 emit 流水线崩溃**。
- **P0-4 完整 gate 不可绕过**：每次重写后的校验都走与首轮完全相同的 `gate_report` 入口，禁止对任一 check 设豁免。

### P1 — grounding 与 judge 接口（设计纳入，可随 ② 落地）

- **P1-1 ① 事前 grounding 改造点**：在 `src/analyzer.py` 报告生成 prompt 强制约束「所有数字须来自注入的行情 context，禁止编造；涨跌停/价位的结论须与 `source_facts` 一致」。（可随 ② 一起落地，或单列后续，但本轮须给出改造点说明。）
- **P1-2 ③ LLM-judge 接口预留**：复用现有 `JUDGE_USE_LLM` 开关与 `llm_judge` callable 注入点；本轮**不强制激活**真实 judge，仅确认接口可接 DeepSeek 真 judge + 修正建议（供 ② 重写时作为可选修正信号源）。

### P2 — 可观测性（建议随 ② 一起落地）

- **P2-1 jsonl 新增字段**：`repair_rounds`（int）、`rewritten`（bool）、`final_action`（`"emit"`|`"reject"`）、`repair_reasons`（list，喂给模型的首轮违规原因）。
- **P2-2 repair 后 md 落盘备查**：修复成功发布的报告可另存 `<stem>.repaired.md`（或写入 `logs/`），便于回查重写效果与回归。

## 4. 验收标准（可测试）

| # | 验收项 | 判定方法 |
|---|---|---|
| AC1 | 幻觉样本经 repair 后发布 | 取 `logs/judge_rejects.jsonl` 中 2 份真实样本（`market_review` 方向写反、`report` 红线+跌停写反）+ 对应 `.facts.json`，跑 repair loop 后 `gate_report().passed == True` 且被 emit |
| AC2 | 红线必被移除 | 修复后 `red_line` check `passed == True`；报告不含「买入/卖出 + 价位/目标价」等违规文案 |
| AC3 | 正常样本不被改坏 | 一份本就 `passed` 的报告：首轮即通过，**不进 repair loop**（`repair_rounds == 0`、不调模型、内容不变）；若强制触发重写，重写后语义等价、无新增幻觉 |
| AC4 | 超轮数回退 reject | 构造「模型无法修好」的样本（或 mock rewrite 返回原样），轮数耗尽后 `final_action == "reject"`、**不 emit**、jsonl 记录完整 |
| AC5 | LLM 不可用时安全降级 | mock `rewrite_report` 抛异常：流水线不崩溃、其余报告正常 emit、该报告 `final_action == "reject"`、日志落盘 |
| AC6 | 完整 gate 不可绕过 | 重写后报告即便仅红线修好但方向仍写反，仍判 fail 进入下一轮（不得因「部分修复」误 emit） |

> 测试归属：`tests/test_judge_gate.py` 现有 10/10 须保持通过；新增 repair 回归用例覆盖 AC1/AC3/AC4/AC5。

## 5. UI / 日志影响（仅字段增量）

`logs/judge_rejects.jsonl` 现有记录字段：`ts, kind, passed, score, reasons, checks, context`。
本轮**新增/调整**字段（保持旧字段兼容）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `repair_rounds` | int | repair loop 实际轮数（首轮即通过为 `0`） |
| `rewritten` | bool | 是否真正发起过模型重写 |
| `final_action` | str | 最终动作：`"emit"`（修好发布）/ `"reject"`（超限或降级丢弃） |
| `repair_reasons` | list[str] | 喂给模型重写的首轮违规原因（便于回查修了什么） |

> 触发时机：① 修好并 emit → 写一条 `final_action="emit"` 记录；② 超轮/降级 reject → 写 `final_action="reject"` 记录。首轮即通过仍不写（维持现状，零噪音）。

## 6. 待确认问题（需架构师/工程确认）

1. **重写粒度**：整篇重生成 vs 段落 patch？整篇（复用 analyzer 模型客户端、实现简单）可能改动非违规段；段落 patch 更精准但需 validator 返回**违规 span/行号**（当前 `CheckResult` 仅存人类可读 `detail`，不含命中文本）。建议架构师在方案阶段定。
2. **`REPAIR_MAX_ROUNDS` 默认值**：取 `2` 还是 `3`？直接影响 API 成本与 emit 延迟（每轮一次模型调用 + 一次完整 gate）。
3. **rewrite 模型来源**：是否复用主分析模型（`LITELLM_MODEL=deepseek/deepseek-v4-flash`）？是否需独立轻量模型/更低温度以保证「照反馈改、不自由发挥」？rewrite 封装建议落在 `src/analyzer.py`（复用其模型客户端）还是新建 `src/core/repair.py`？
4. **source_facts 是否必填**：当前多数报告**无** `.facts.json` 侧车。若侧车缺失，repair 退化为「仅靠 `reasons` 让模型自纠」，准确率下降。是否要求分析阶段**强制产出** `.facts.json`（与 ① grounding 联动）？若强制，是否在本轮一并改 pipeline 注入。

---
*附：关键代码锚点 — `src/core/validator.py:ValidationResult/CheckResult/gate_report`；`scripts/emit_frontend_artifacts.py:65 _load_source_facts`、`:59 _JUDGE_CONFIG`、`:125-138` 拦截点（repair loop 插入处）、`:92 LITELLM_MODEL`。*
