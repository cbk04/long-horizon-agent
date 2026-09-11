# Spec: 三阶段 Evaluator（规则评估 → 逻辑评估 → 加权打分）

> 状态：draft（2026-09-06 设计评审定稿；落地 staged spec 中显式排除的 "Phase 2 研究质量线"）
> 前置：`staged-plan-execute-state-machine.md`（本 spec 在其 evaluate 门上扩展，不改图形状）

## Problem Statement

现有 evaluate 门是单次 LLM 调用产出二值 verdict（COMPLETED / INCOMPLETE + gaps），只回答"做没做完"，不回答"做得对不对、好不好"：

1. **无硬校验前置**：格式残缺、内容空缺的产出也直接进 LLM 评估，白白消耗调用，且缺口反馈靠模型自由发挥、不稳定。
2. **结论无人挑战**：结论的证据支撑强度、证据本身的权威性与时效性，都由 actor 自己的产出自证——既当运动员又当裁判的问题在阶段层依然存在。
3. **验收只有二值**：acceptance 的"维度 + 标准"只被用来判完成/未完成，没有可量化的分维度质量分；任务最终交付质量无法被追踪和比较。

## Solution

evaluate 门升级为**三阶段串联流水线，逐 stage 执行，失败短路**。controller 仍是唯一决策层（评判与决策分离不变），图的形状不变。

```
phase 1  规则评估（零 LLM）
   │ 违规 ──短路──→ RETRY_STAGE（违规清单）
   ▼ pass
phase 2  逻辑评估（同档模型）
   ├─ 正向审计：证据↔结论相关性打分，低于阈值进「支撑不足清单」
   └─ 反向挑战：带搜索的 react agent 找事实反例（强制引用结构）
        └─ 强度裁决：时效性 / 冲突性 / 逻辑性 → low | medium | high
   │ forward 清单非空 或 high ────→ RETRY_STAGE（清单 / 反例）
   │ medium ──→ DEFEND_STAGE（一轮回应后重裁，仍 high → RETRY，否则放行+dispute）
   ▼ low 且 forward 清单为空
phase 3  加权打分（弱模型）
   │ < 阈值 或 主命题 0 分 ──→ RETRY_STAGE（低分 criteria + 评语）
   ▼ pass
   ADVANCE（如有未决争议，带 dispute 标记）
```

分工原则：**正向管内部逻辑**（证据→结论的推理是否成立），**反向管外部事实**（证据本身是否权威、及时、可被更新的事实推翻）。信息不对称保留在两处：反向 agent 不看 actor 的证据与推理（只拿结论，从外部世界找反例）；强度裁决者独立于反向 agent（生成者不给自己的反例打分）。

所有 LLM 输出都映射为**确定性路由规则**，路由无模糊地带。

## User Stories

1. 作为任务提交者，我希望格式残缺的产出在进入任何 LLM 评估前就被打回，以便不为一眼可见的垃圾消耗成本。
2. 作为任务提交者，我希望系统对"结论是否有证据支撑"有独立审计，以便结论不被执行 agent 的自说自话污染。
3. 作为任务提交者，我希望外部有新事实与结论冲突时系统能发现并要求回应，以便报告不基于过时证据。
4. 作为任务提交者，我希望最终报告阶段被明确要求评估可读性与易理解性，以便交付物不光内容对、还能被读懂。
5. 作为任务提交者，我希望每个阶段有一个按维度的加权质量分，以便任务质量可量化、可比较、可追踪。
6. 作为开发者，我希望三阶段的每一步输出都是结构化的、路由是确定性的，以便测试和排障不依赖对模型行为的猜测。
7. 作为运维者，我希望评估自身的 LLM 调用有总熔断，以便评估器不会反过来吃掉任务预算。

## Implementation Decisions

### Plan 侧

- `AcceptanceItem` 新增 `weight: float`，约束 ∈ (0, 1]；`Plan.model_validator` 新增校验：每 stage acceptance 3–6 条、权重和 = 1（容差 0.01）。不合规 → 复用现有 plan 带错误重试机制。
- `Stage` 新增 `is_final: bool`；validator 校验：恰好一个 `is_final=true` 且必须是最后一个 stage。plan prompt 同步要求：最终 stage 产出最终报告，其 acceptance 必含可读性/易理解性维度（语义内容靠 prompt 约束，不做关键词硬校验）。
- `Stage` 新增可选 `output_contract: {min_length: int, required_sections: list[str]} | None`，由 plan 生成，供 phase 1 契约规则使用。
- `plan.generated` payload 随 `model_dump()` 自动携带新字段，无需改动事件代码。

### Phase 1 — 规则评估（零 LLM）

纯函数模块（落点 `app/evaluation/rules.py`），输入 stage_result + 当前 stage 的 plan 元数据，输出违规清单：

- **基础规则**（全局固定）：非空、最小长度下限、markdown 结构完整（存在标题层级、无非空标题）。
- **契约规则**（per-stage，来自 plan）：`output_contract.min_length`、`required_sections`（小节标题关键词必须出现）；`is_final` stage 额外要求报告类结构。
- **违规即短路**：跳过 phase 2/3，RETRY_STAGE，feedback = 精确违规清单。

### Phase 2 — 逻辑评估

**正向审计**（1 次 one-shot 调用，同档模型，structured output，`method="function_calling"`）：

- 输入：stage objective + scope_excludes + 证据列表 + stage_result。**不给** react 推理过程。
- 任务：对 stage_result **整体**评估"结论与证据的逻辑相关性"（三档 high/medium/low + 理由），不逐条死扣单句相关性——延伸性/拓展性结论属正常，只要整体仍立足证据即可。
- 确定性规则：`logical_relevance == low`（结论整体与证据脱节 / 主要断言无凭据）→ RETRY，feedback 给出整体理由并列出 `unsupported` 无凭据断言；medium/high 均放行进入 phase 3。

**反向挑战**（react agent + search 工具，独立 thread `{task_id}-stage-{n}-reverse`）：

- 输入：**仅 stage_result 的结论**。不给证据、不给推理——它的工作是从外部世界找事实反例，挑战证据的权威性与时效性。
- 每条反例强制结构：`{claim, counter_evidence, source_url, quote, date}`。"严禁编造"不靠提示词自觉：结构缺失、或裁决时发现 quote 支撑不了 counter_evidence 的条目**判废**。
- 工具轮数上限 `reverse_max_tool_calls`（默认 5），预算/取消 guard 沿用 react 机制。

**强度裁决**（1 次 one-shot 调用，同档模型，独立于反向 agent）：

- 输入：结论 + 全部反例 + 证据摘录 + 任务时间窗（从 plan / 任务元数据注入，供时效性判断）。
- 职责：① 校验引用（判废不合格条目）；② 对每条有效反例按三维度评级：
  - **时效性**：反例日期相对任务时间窗；
  - **冲突性**：只有**直接对立**（反例事实与结论断言不可同真）可评 high；**视角差异**（不同口径/侧面，可并存）封顶 medium；
  - **逻辑性**：even-if-true 测试——假设反例为真，结论是否被实质性推翻（而非仅伤及外围细节）。
- 聚合 = **取最高档**：任一 high → high；否则任一 medium → medium；否则 low。判废条目不参与。不做平均（一个强反例不应被一堆弱反例稀释）。
- 路由：low → 放行（反例记录在案）；medium → DEFEND；high → RETRY（feedback = 反例清单，"请正面回应"）。

**并行与优先级**：forward 与 reverse 并行执行。若 forward 清单非空 **且** reverse 为 high，合并为一份 feedback 一次 RETRY（并行成本相同，一次打回省一轮）。优先级：RETRY > DEFEND > 放行。

**DEFEND 轻路径**（争议回应）：

- controller `Action` 新增 `DEFEND_STAGE`；executor 新增 defend 提示模式：actor **不重做** stage，仅对反例逐条回应（解释、限定适用范围、承认局限），产出 append 到 stage_result。prompt 明确禁止引入新的事实性主张——需要新主张就是 RETRY 的职责。
- 回应后**只重跑强度裁决**（输入 = 原反例 + 回应；forward 不重跑——证据与结论未变，重跑无信息量）。仍 high → RETRY（消耗 stage 重试预算）；medium/low → 放行 + `dispute` 标记。
- defense 预算独立于 retry 预算：`max_defense_rounds`（默认 1）；耗尽仍 medium → 放行 + dispute。dispute 记入评估结果与最终报告。

### Phase 3 — 加权打分

- 1 次 one-shot 调用，**弱模型**（`evaluator_score_model`；rubric 明确的机械对照，弱档足够）。
- 输入：stage objective + acceptance（维度+标准+权重）+ stage_result（含 defense 回应）+ 证据。
- 每条 criterion 离散三档 **0 / 0.5 / 1**（未达成 / 部分达成 / 达成），可判定命题就该可判定，连续分是伪精度。强制逐条引用证据位置；**单次调用评全部**（命题间需统一松紧，分开调用会各评各的）。
- stage 分 = Σ(score × weight)。
- 通过条件（确定性）：stage 分 ≥ `score_pass_threshold`（默认 0.7）**且**无任何 weight > `heavy_criteria_weight`（默认 0.3）的 criterion 得 0 分。不达标 → RETRY，feedback = 低分 criteria + 评语。

### Controller 路由扩展

`Action` 枚举新增 `DEFEND_STAGE`；`route_decision` 输入扩展（`evaluation` 结构 + `stage_defense_rounds`）。映射表：

| 评估结果 | Action | feedback |
|---|---|---|
| phase 1 违规 | RETRY_STAGE | 违规清单 |
| forward 支撑不足清单非空 | RETRY_STAGE | 清单逐条 |
| 强度裁决 high（含 defend 后仍 high） | RETRY_STAGE | 反例清单 |
| phase 3 不达标 | RETRY_STAGE | 低分 criteria + 评语 |
| 强度 medium（未 defend） | DEFEND_STAGE | 反例清单（要求逐条回应） |
| 全部通过（或 defend 后 medium/low） | ADVANCE / COMPLETE | — |
| RETRY 耗尽（`max_stage_retries`） | FAIL（现状不变，任务 FAILED） | — |

`stage_feedback` 继续作为 `list[str]` 传递，内容由对应阶段的结构化结果渲染。

### 熔断与预算接线

- 新增任务级计数 `eval_call_count`（AgentState 声明字段）；超过 `max_eval_llm_calls_per_task`（默认 100）→ **降级**：跳过 phase 2，只跑 phase 1 + 3，verdict 带 `eval_degraded=true`。每 stage 全量评估 ≈ 正向 1 + 反向 ~6（含工具循环）+ 裁决 1 + 打分 1 ≈ 9 次，5 stage × 最多 3 轮评估的最坏情形约 135 次，100 是合理默认。
- 评估器所有 one-shot 调用改走 `call_llm`（现有未接线的封装本次激活），计入任务预算；反向 agent 走 react 机制自带 per-step guard。react 全链 token 计数仍属可靠性线，不在本 spec。

### 数据结构

- `StageVerdict` 扩展（保持向后兼容字段）：`status`、`gaps`、`key_findings` 保留；新增 `phase1: {violations}`、`phase2: {forward: {relevance[], insufficient[]}, reverse: {counterexamples[], tier}, dispute}`、`phase3: {criteria_scores[], weighted_score}`、`feedback`、`eval_degraded`。
- `AgentState` 新增声明字段：`stage_defense: str | None`、`stage_defense_rounds: int`、`eval_call_count: int`、`eval_degraded: bool`。**必须显式声明**——LangGraph 对未声明 key 静默丢弃。
- 事件：`stage.evaluated` payload 扩展为上述结构；新增 `stage.defended`。沿用 `common.publish` 双写，SSE 与历史查询自动生效。

### 持久化（新表）

`app/storage/mysql/models.py` 新增 `stage_evaluation` + alembic 迁移：

```
stage_evaluation
├─ id             PK
├─ task_id        indexed
├─ run_id
├─ stage_index
├─ attempt                      # 第几次 stage 重试
├─ defense_round                # 第几轮 defend（无则 0）
├─ status         str           # PASSED / RETRY / DEFEND / DEGRADED_PASS
├─ rule_result    JSON          # 违规清单
├─ forward_result JSON          # relevance[] + insufficient[]
├─ reverse_result JSON          # counterexamples[]（含强度与判废标记）+ tier
├─ criteria_scores JSON         # 逐条 dimension/criteria/weight/score/comment
├─ weighted_score FLOAT | NULL
├─ feedback       JSON
├─ created_at
```

- 写入点：staged evaluate 节点内经 storage repository 落库（与 `publish` 同位置）。
- **任务最终分** = `is_final` stage 最新一条 PASSED 评估行的 `weighted_score`，不另设任务级聚合。
- 行内同时快照当次评估所用的 acceptance（含 weight），避免 plan 只存在于事件流中导致分数不可追溯。

### 配置项（config.py + .env.example 同步）

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_stage_retries` | 2 | 从 controller 常量迁出 |
| `reverse_max_tool_calls` | 5 | 反向 agent 搜索轮数上限 |
| `max_defense_rounds` | 1 | 争议回应轮数 |
| `score_pass_threshold` | 0.7 | stage 加权分通过线 |
| `heavy_criteria_weight` | 0.3 | 超过此权重的 criterion 得 0 即不通过 |
| `max_eval_llm_calls_per_task` | 100 | 评估调用熔断，超限降级 |
| `evaluator_score_model` | （留空，待填） | phase 3 弱模型名 |
| `evaluator_score_api_key` | （留空，待填；空则回落 `openai_api_key`） | |
| `evaluator_score_base_url` | （留空待填；空则回落 `openai_base_url`） | |

### LLM 封装

`get_llm` 增加 `model` / `api_key` / `base_url` 覆盖参数，空值回落主配置；phase 3 用弱模型档，正向审计与强度裁决保持与 actor 同档（逻辑审计与裁断用弱模型会被强词夺理骗过，信息不对称实验失效）。

## Testing Decisions

- **Seam 1 — controller 纯函数**：新增分支全覆盖——DEFEND 派发、defend 耗尽放行+dispute、defend 后 high 转重试并计数、phase 3 不达标重试、熔断降级路径。零 LLM、零 DB。
- **Seam 2 — 纯函数单测**：规则引擎（基础 + 契约 + is_final 特例）；强度聚合（最高档、判废剔除、视角差异封顶）。
- **Seam 3 — 整图 + fake LLM**（沿用 `test_staged_graph.py` 的 monkeypatch 注入点，evaluator 走 `get_llm` 自动被替身覆盖）：全通过 happy path；规则短路；forward 清单打回；反例 high 打回；medium → defend → 放行带 dispute；defend 后 high → 重试；打分不达标打回；重试耗尽 FAIL；熔断触发后跳过 phase 2。
- 所有结构化输出 fake 与真实路径都必须 `method="function_calling"`。

## Out of Scope

- react 全链 token 计数接入预算（可靠性线）。
- 任务级总分聚合、跨任务分数对比看板。
- 反向 agent 多路并行 / 多数投票。
- dispute 的人工仲裁流程。
- criteria 打分的逐条并行调用（已定单次调用）。
- REPLAN / 动态计划（沿用前置 spec 排除项）。

## Further Notes

- **建议实现顺序**：plan schema（weight / is_final / output_contract）→ 规则引擎纯函数 → evaluator 三阶段重构 → controller 新分支 → DEFEND 路径与 executor 提示模式 → 持久化表与迁移 → 三个 seam 测试补齐。每步可独立提交，controller/evaluator 的测试面现成。
- **已知坑 checklist**（沿袭前置 spec）：① 所有结构化输出必须 `method="function_calling"`（DeepSeek 不支持 json_schema）；② `AgentState` 未声明 key 被 LangGraph 静默丢弃；③ 评估调用必须实际接入 `call_llm` 否则熔断计数不生效；④ 独立 thread 命名遵循 `{task_id}-stage-{n}-reverse` 约定，避免与 research / stage 线程串线。
- 本 spec 落地后，`app/evaluation/` 占位目录承载纯逻辑（rules / 强度聚合 / 判废），带 I/O 的编排留在 `app/agent/staged/evaluator.py`，与"评判策略可替换、决策层确定"的既有架构承诺一致。
