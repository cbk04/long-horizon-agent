# Spec: 分阶段 Plan-Execute 状态机（Plan → Controller → React ⇄ Evaluate）

> 状态：draft（issue tracker 未配置，暂存于仓库；配置 tracker 后可由 /to-tickets 拆分并打 `ready-for-agent` 标签）
> 日期：2026-09-01

## Problem Statement

当前的 plan-execute 图把 plan 产出的步骤列表一次性整包交给单个 ReAct agent 执行：agent 在一条不断增长的消息历史里跑完全程。对于数十分钟量级的 DeepSearch 任务，这带来三个问题：

1. **没有阶段边界**：计划只是提示词里的一段文字，系统层面不知道"计划进行到哪一步了"，无法按阶段观察、评估和恢复。
2. **执行与评判混在一起**：ReAct agent 自己决定"做够了没有"，既当运动员又当裁判，长任务中容易过早收敛或无限发散。
3. **上下文全量传递**：后一段工作能看到之前全部原始消息历史，上下文随任务时长线性膨胀，与设计文档中 Context Engineering 的分层方向相悖。

## Solution

把执行图改造为四个节点的闭环：**plan → stage controller → react → evaluate**，外加保留在 plan 之后的人工审批门。

- **plan**：两阶段 planner——先用 ReAct agent 围绕目标做快速调研（复用 react 子图，独立 thread），再把"目标 + 调研笔记"交给结构化输出 LLM，产出带目标、边界（scope_excludes）与验收清单（维度+标准）的阶段列表；语义校验失败时带错误信息有界重试。
- **human_approval**（保留，位置在 plan 之后）：用户看到完整阶段划分后再放行。
- **stage controller**：**不调用模型**。它读取 plan 的 JSON，把阶段划分构造成一个确定性的状态机，是系统中**唯一的决策层**。evaluate 判定上一阶段完成后，controller 推进到下一阶段并把工作交给 react。
- **react**：每次只执行当前阶段，把执行结果交给 evaluate。
- **evaluate**：对当前阶段给出结构化评判（是否完成、缺口、关键结论），**只评判、不决策**——它的输出是 controller 的输入，路由永远由 controller 决定。

阶段之间的信息不采用全量传递：下一阶段的 react 只拿到必要信息和前一阶段的关键结论。具体的上下文分层（L0–L4 视图）本期不实现，先以"必要信息 + 关键结论"的交接消息落地。

## User Stories

1. 作为任务提交者，我希望计划被拆成明确的阶段逐段执行，以便最终结果的质量不因任务变长而下降。
2. 作为任务提交者，我希望在审批时看到带目标和完成标准的阶段划分，而不是一串扁平步骤，以便判断计划是否靠谱。
3. 作为任务提交者，我希望某个阶段没做好时系统会带着缺口反馈自动重做该阶段，以便最终交付不被单一阶段的失败拖垮。
4. 作为任务提交者，我希望阶段重试有次数上限、超限后任务明确失败而不是无限消耗预算，以便成本可控。
5. 作为通过 SSE 观察任务的用户，我希望收到阶段开始/阶段完成/阶段评判的事件，以便实时看到任务推进到了哪一步。
6. 作为通过 SSE 观察任务的用户，我希望阶段事件里包含 evaluate 的关键结论，以便不用等最终结果就能了解中间产出。
7. 作为开发者，我希望状态机的全部推进逻辑集中在一个不调用模型的纯函数里，以便零成本、确定性地验证所有路由分支。
8. 作为开发者，我希望"评判"与"决策"分离（evaluate 只产出 verdict，controller 独占路由），以便后续替换评判策略时不用碰执行逻辑。
9. 作为开发者，我希望 controller 不依赖 LLM，以便阶段推进永远确定、可复现，不因模型输出抖动而产生诡异路由。
10. 作为开发者，我希望 react 的每次阶段执行是独立构造的上下文而非全量历史，以便单阶段的重试和调试不受历史噪声干扰。
11. 作为开发者，我希望图的对外入口和中断/恢复行为保持不变（审批门仍是唯一的 interrupt），以便 worker 层不需要随本次改造变动。
12. 作为运维/排障者，我希望每个阶段的事件流能独立回放（开始、执行、评判、推进决策），以便定位"卡在哪个阶段、为什么"。
13. 作为后续贡献者，我希望阶段间交接的数据结构是显式定义的（必要信息 + 关键结论），以便未来在此基础上实现完整的 L0–L4 上下文分层而不推翻现有接口。
14. 作为后续贡献者，我希望 plan 的阶段结构是 pydantic 模型约束的 JSON，以便 controller 可以安全地确定性解析而无需容错自由文本。

## Implementation Decisions

- **图的形状**：`START → plan → human_approval → controller → react → evaluate → controller → … → END`。审批门是图中唯一的 interrupt；审批通过后整图一次性跑完所有阶段，中途不再暂停，worker 的两段式调用（首跑 → resume）与 PAUSED/COMPLETED 状态流转保持不变。
- **Plan 结构升级**：planner 的结构化输出从"有序步骤字符串列表"升级为阶段列表，每个阶段包含：阶段目标（objective）、边界声明（scope_excludes，本阶段明确不做的内容，保证阶段间不重叠）、验收清单（acceptance，≥1 条"维度 + 标准"，交给 evaluate 评判）。阶段划分本身仍由 plan 一次产出，执行期 controller 不改计划。
- **Planner 两阶段化**：`plan` 节点内部先跑一次 ReAct 调研（thread `{task_id}-research`，产出调研笔记：关键概念、数据来源、常见验收维度、风险），再以 `with_structured_output(Plan, method="function_calling")` 从"目标 + 笔记"生成计划。结构约束来自工具参数 schema；语义约束（阶段 ≥2、验收非空、scope 不重叠）由 `Plan` 的 pydantic `model_validator` 强制，违规时把校验错误拼回 prompt 重试，上限 2 次，仍失败抛 `ValueError` 映射任务失败。
- **Controller = 确定性状态机**：一个不调用任何模型的路由函数，输入为（阶段列表、当前阶段索引、各阶段累积状态、最近一次 evaluate verdict），输出为下一个动作。路由规则：
  - 当前阶段 verdict 为完成 → 推进到下一阶段；已是最后阶段 → 结束图。
  - verdict 为未完成 → 携带 evaluate 给出的缺口（gaps）反馈重新派发 react。
  - 当前阶段重试次数达到上限（每阶段 2 次）→ 整个任务判定失败（抛出可被 worker 映射为 FAILED 的异常）。
- **Evaluate 只产出结构化评判**：LLM 结构化输出至少包含：阶段是否完成（完成/未完成）、未完成时的缺口列表、本阶段关键结论（供后续阶段交接与事件流使用）。evaluate 的输出写入图状态供 controller 读取，但它对路由没有直接控制权。
- **阶段间信息交接**：不传递完整消息历史。每个阶段的 react 以"当前阶段目标 + 前序阶段的必要信息与关键结论"构造新的起始消息；react 子图内部的 checkpoint 机制照常工作，但阶段交接面是显式、有界的结构化数据。具体交接哪些字段本期取最小集（目标、关键结论、缺口），完整分层后续迭代实现。
- **预算与取消语义不变**：react 既有的 per-step 预算检查和取消检查在所有阶段上继续生效；预算耗尽仍然映射为任务 FAILED，用户取消仍然映射为 CANCELLED。阶段重试次数上限与预算上限是两条独立的护栏。
- **事件流**：`plan.generated` 的 payload 升级为包含阶段结构；新增阶段级事件（阶段开始、阶段评判结果、controller 推进决策），沿用现有 event_bus 发布方式，保证 SSE 消费者可以按阶段回放。
- **复用现有构件**：react 节点继续复用现有 ReAct 子图及其 checkpointer、预算、取消机制；审批门继续复用现有 interrupt 实现；worker 不感知阶段概念。

## Testing Decisions

- **只测外部行为**：测试断言的是"给定状态，状态机推进到哪、交接了什么信息、何时结束/失败"，不断言节点内部实现细节。
- **Seam 1 — controller 纯函数**：状态机的全部路由分支（推进、带缺口重试、重试超限失败、全部完成结束）用直接调用的方式覆盖，零 LLM、零数据库、确定性断言。这是本次改造的核心回归面。
- **Seam 2 — 整图 + fake LLM**：planner / react / evaluate 的模型替换为预设响应的 fake（LangChain 的 fake chat model），从编译后图的入口驱动，覆盖端到端场景：
  - 两阶段计划被依序执行，阶段一完成后才进入阶段二；
  - evaluate 判未完成时 react 收到缺口反馈并重跑；
  - 重试超限后任务走向失败路径；
  - 审批门 interrupt / resume 行为与改造前一致。
  整图测试沿用现有集成测试对本地 MySQL 的依赖方式（图节点内的直接事件发布保持原样，不为测试另加抽象）。
- **先例**：现有集成测试（worker 层 `_process_task` 驱动、真实 MySQL）作为集成层先例；fake LLM 在本仓库是新模式，但被限定在整图这一个 seam 上，不扩散到节点级单测。

## Out of Scope

- 完整的 L0–L4 上下文分层与动态 Working Context 组装（本期只做"必要信息 + 关键结论"的显式交接）。
- REPLAN：controller 不修改计划、不触发重新规划；计划在审批后固定。
- 阶段并行执行、阶段间依赖图（本期阶段严格线性）。
- Evidence/Claim/Citation 链路与质量评分（属 Phase 2 研究质量线）。
- Worker lease/heartbeat/崩溃恢复（属 Phase 1/3 可靠性线）。
- 多 Agent 协作。

## Further Notes

- 本 spec 是"决策层与执行层解耦"的第一步：controller 状态机先以最小规则集（线性推进 + 有界重试）落地，其输入输出结构为后续 REPLAN、动态计划、上下文分层预留了扩展点。
- 实现期发现：仓库安装的 LangGraph 1.x 中 interrupt 不再向外抛 `GraphInterrupt`，而是把 `__interrupt__` 放进 invoke 返回的 state——原 worker 的 `except GraphInterrupt` 分支实际永远接不到。实现改为首跑检测 `__interrupt__` 后抛领域异常 `AwaitingApproval`，worker 改接该异常；PAUSED/resume 语义与 spec 一致。
- 实现期发现（2026-09-02）：DeepSeek API 不支持 `response_format=json_schema`（strict，实测 400），且新版 `langchain-openai` 的 `with_structured_output` 默认 method 已是 `json_schema`，对 DeepSeek 会直接报错——所有结构化输出必须显式 `method="function_calling"`（工具参数 schema 路径，实测可用）。因此 schema 保证 = 工具参数 schema + pydantic 校验 + 有界重试，而非解码层硬约束。
- 项目 issue tracker 尚未配置（无 git remote、无 gh CLI）；本 spec 先以文件形式入库，配置 tracker 后建议拆分为 controller、evaluate、plan 结构升级、整图测试四个 ticket 并打 `ready-for-agent` 标签。
