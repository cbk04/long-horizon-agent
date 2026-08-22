# Long-Horizon DeepSearch Agent Harness
> 技术架构设计与迭代路线 v3 — Context Engineering
> FastAPI + LangGraph + Middleware + MySQL + Redis

---

## 1. 背景与目标
Long-Horizon DeepSearch 与普通问答 Agent 的区别，不只是推理更复杂，而是任务可能持续数十分钟甚至数小时，需要多轮搜索、网页阅读、证据抽取、问题分解、验证、重新搜索和最终综合。执行过程中还会出现 LLM/工具失败、Worker 崩溃、用户断线、上下文膨胀和预算超限。
因此本项目不以"再实现一个 ReAct Agent"为目标，而是构建一个 Long-Horizon Task Runtime，并使用 LangGraph 承担 Agent 的图状态与 ReAct 执行循环，再用 Middleware 将 Context、Evaluation、Budget、Guardrail、Tracing 等横切能力解耦。
### 核心目标
- 任务与 HTTP 请求解耦，可异步持续运行。
- Worker 崩溃/进程重启后可从 checkpoint 恢复。
- DeepSearch 结果必须有 evidence/source 链路并经过验证。
- 上下文通过 Middleware 压缩、摘要、检索和结构化状态注入。
- 每个 model/tool/eval/checkpoint 都可追踪。
- Harness 不绑定具体 Agent Framework，LangGraph 是第一种 Runtime。
### 非目标
- MVP 不做 Multi-Agent。
- 不自研搜索引擎、浏览器或 LLM。
- 不一开始做复杂分布式调度。
- 不重复实现 LangGraph graph-state checkpoint；系统层只维护 task-level durable state。
## 2. 总体架构与边界
```
Client
  -> FastAPI Control Plane
  -> Harness / Long-Horizon Task Runtime
       | Task lifecycle / Lease / Recovery / Budget
       | Event / Artifact / Evidence / Cancellation
       v
    LangGraph Agent Runtime
       | ReAct main loop
       | Middleware: Context / Eval / Budget / Guardrail / Trace
       v
    LLM + Search/Read/Extract tools

MySQL: durable task metadata
Redis: event stream / lease / heartbeat / realtime state
Object Storage: raw pages / large artifacts
LangGraph Checkpointer: graph state
```
### Harness 的职责
- Task lifecycle：CREATED/QUEUED/RUNNING/PAUSED/FAILED/COMPLETED/CANCELLED。
- Worker lease、heartbeat、故障恢复、重试、取消、暂停。
- Task-level token/time/cost/tool budget。
- Event log、Artifact/Evidence 生命周期。
- 跨 Run 的任务状态和恢复策略。
### LangGraph 的职责
- ReAct 主循环与 graph state。
- Tool execution。
- 一次 Agent Run 的 checkpoint/resume。
- 承载 Middleware 生命周期。
### Middleware 的职责

| Middleware | 职责 | 输出 |
| --- | --- | --- |
| Context | 压缩、摘要、相关历史/evidence 注入 | model input |
| Evaluation | Step/Phase/Task 质量检查 | PASS/INCOMPLETE/RETRY/REPLAN |
| Budget | token/time/tool/cost 限制 | continue/stop/degrade |
| Guardrail | 工具权限、域名、输入输出校验 | allow/deny/sanitize |
| Trace | model/tool/eval/context 可观测性 | span/event |
| Tool Policy | 根据阶段限制/推荐工具 | allowed tools |


## 3. 为什么 Context / Eval 做成 Middleware
这样 ReAct 主循环只负责"根据当前状态决定下一步动作"，而上下文管理和质量控制成为可插拔能力。未来可以替换 Context 策略或 Evaluator，而不修改 Agent loop。
```
before_model:
    relevant_evidence = retrieve_relevant_evidence(goal, current_question)
    compact_history = context_manager.compact(messages)
    inject(compact_history, findings, relevant_evidence, gaps)

after_model:
    if should_summarize(messages):
        state.context_summary = summarize(messages)

Eval:
    result = evaluator.evaluate(phase, findings, evidence, goal)
    state.evaluation = result
    # Agent 根据 result 自己决定下一步，不让 middleware 变成第二个 Agent
```
### Evaluation 分层
- Step Eval：当前 tool call、网页解析、evidence extraction 是否成功。
- Phase Eval：当前研究阶段是否覆盖主要子问题。
- Task Eval：最终答案是否完整、正确、相关、可引用。
### 关键原则
Middleware 是 Observer/Controller，而不是第二个 Agent。Eval Middleware 产生结构化反馈，例如 missing claims、coverage、confidence，由 ReAct Agent 决定继续搜索、修正或综合。
## 4. DeepSearch 执行模型
```
Goal
 -> Plan
 -> Search
 -> Read
 -> Extract Evidence
 -> Evaluate Coverage / Evidence Quality
      | incomplete
      v
   Identify Gaps -> Re-search
      |
      v sufficient
   Synthesize -> Final Eval -> Completed
```
### 核心状态
```
{
 task_id, run_id, goal, research_plan, current_phase,
 current_question, findings, evidence_refs,
 unresolved_gaps, recent_messages, artifacts,
 budget, evaluation
}
```
不要把长期任务状态全部塞进 messages。研究计划、findings、gaps、evidence、budget 等关键事实应结构化保存。
### Evidence-first
- Search Result：候选结果，不等于证据。
- Source：网页/论文/官方文档等来源。
- Evidence：从 Source 定位抽取、可归属于 claim 的片段。
- Claim：最终回答中的事实性判断。
- Citation：Claim 与 Evidence/Source 的关系。
最终输出尽量形成 Claim -> Evidence -> Source 的链路。
## 5. Harness Runtime 与恢复

| 对象 | 含义 | 生命周期 |
| --- | --- | --- |
| Task | 用户提交的长期目标 | 分钟～小时/天 |
| Run | Task 的一次 Agent Runtime 执行 | 秒～小时 |
| Step | 一次逻辑步骤 | 秒～分钟 |
| Action | 具体模型/工具动作 | 毫秒～分钟 |
| Event | 不可变执行事实 | 长期 |
| Artifact | 大对象/文件/网页快照 | 长期 |
| Evidence | 支撑 Claim 的证据 | 长期 |


### Worker Lease / Recovery
```
1. Worker 从 QUEUED 领取 task
2. MySQL 原子更新 status=RUNNING + lease_owner + lease_expire_at
3. Worker heartbeat
4. 执行 LangGraph
5. 持久化 event + checkpoint
6. Worker 崩溃 -> lease 过期
7. Recovery Worker 抢占 task
8. 从最新 LangGraph checkpoint resume
9. 继续执行，而不是从头开始
```
FastAPI 只负责创建/查询/控制 Task，真正执行由独立 Worker 完成。客户端断开不能杀死任务。
### 幂等性
- 每个 action 生成 action_id。
- Recovery 后执行前检查 action_id 是否已完成。
- MVP 以只读搜索为主，降低重复副作用。
- 未来有写操作时必须引入 idempotency key/transaction/compensation。
## 6. 数据架构：MySQL + Redis + Object Storage

| 组件 | 职责 | 权威性 |
| --- | --- | --- |
| MySQL | Task/Run/Step/Evidence/Artifact metadata、预算、状态 | Task metadata 的 source of truth |
| Redis | SSE event stream、lease、heartbeat、实时状态、rate limit | 实时协调层 |
| Object Storage | 网页快照、原始文档、大型 artifact | 二进制 artifact 的 source of truth |
| LangGraph Checkpointer | graph state / thread checkpoint | Agent graph state |


### MySQL 核心表
```
task(id, user_id, goal, status, priority, budget_tokens,
     budget_seconds, budget_cost, current_run_id,
     lease_owner, lease_expire_at, created_at, updated_at)

agent_run(id, task_id, thread_id, status, started_at,
          ended_at, checkpoint_ref, retry_count)

step(id, run_id, step_no, phase, status, action_type,
     started_at, ended_at, error_code)

evidence(id, task_id, source_url, source_title, locator,
         content_ref, claim_id, quality_score, created_at)

artifact(id, task_id, type, object_key, mime_type,
         size, checksum, created_at)

task_budget_usage(task_id, input_tokens, output_tokens,
                  tool_calls, elapsed_ms, estimated_cost)
```
### Redis Key
```
task:{task_id}:events       Redis Stream
task:{task_id}:lease        worker_id + expire_at
task:{task_id}:heartbeat    heartbeat
task:{task_id}:runtime      realtime runtime info
task:{task_id}:cancel       cancellation flag
task:{task_id}:budget       fast counters
```
## 7. FastAPI API 设计
FastAPI 是 Control Plane，不承担长任务执行。创建任务后立即返回 task_id；Worker 后台执行 LangGraph。

| Method | Path | 用途 |
| --- | --- | --- |
| POST | /api/v1/tasks | 创建 DeepSearch Task |
| GET | /api/v1/tasks/{task_id} | 查询状态 |
| POST | /api/v1/tasks/{task_id}/pause | 暂停 |
| POST | /api/v1/tasks/{task_id}/resume | 恢复 |
| POST | /api/v1/tasks/{task_id}/cancel | 取消 |
| GET | /api/v1/tasks/{task_id}/events | 历史事件 |
| GET | /api/v1/tasks/{task_id}/stream | SSE 实时流 |
| GET | /api/v1/tasks/{task_id}/evidence | 证据链 |
| GET | /api/v1/tasks/{task_id}/artifacts | Artifacts |
| POST | /api/v1/tasks/{task_id}/retry | 重试失败任务 |


### 创建任务
```
POST /api/v1/tasks
{
  "goal": "比较三种 Agent Runtime 的 long-horizon 能力",
  "max_duration_seconds": 7200,
  "max_cost": 10.0,
  "search_depth": "deep",
  "output_format": "report"
}
-> 202 Accepted
{"task_id":"task_01...","status":"QUEUED"}
```
### SSE
```
GET /api/v1/tasks/{task_id}/stream
event: step
data: {"step_id":"s17","phase":"research"}
event: tool
data: {"tool":"web_search","status":"completed"}
event: evidence
data: {"evidence_id":"ev88"}
event: eval
data: {"status":"INCOMPLETE","missing":["benchmark"]}
event: completed
data: {"task_id":"..."}
```
SSE 只做实时传输；客户端断线后任务继续运行。重连时通过 Last-Event-ID 或历史 events API 补齐。
## 8. MVP

| 能力 | MVP | 说明 |
| --- | --- | --- |
| FastAPI | 是 | Task/control/SSE |
| MySQL | 是 | Task/Run/Step/Evidence/Artifact |
| Redis | 是 | Event/Lease/Heartbeat |
| LangGraph | 是 | ReAct + checkpoint |
| Context Middleware | 是 | summary + recent window + structured state |
| Eval Middleware | 是 | step + final 基础评估 |
| Budget Middleware | 是 | token/time/tool |
| Trace Middleware | 是 | 基础 trace/event |
| DeepSearch | 是 | search + open + extract + evidence |
| Worker Recovery | 是 | lease + checkpoint resume |
| Multi-Agent | 否 | 后续 |
| 复杂 Browser | 否 | 后续 |


### MVP 验收标准
- 客户端断开不影响任务。
- Worker kill 后可以恢复而不是从头重跑。
- Context 有压缩事件且不会无限增长。
- 主要事实有 evidence/source 关联。
- Eval 能拒绝明显不完整答案并反馈缺口。
- SSE 可实时展示进度，重连可补事件。
- 超过 budget 能安全停止/降级。
- 所有关键动作可由 task_id/run_id/step_id 追踪。
## 9. 迭代路线
### Phase 0：Foundation
- FastAPI + MySQL + Redis + LangGraph 骨架
- Task/Run/Step
- Worker lease + heartbeat
- LangGraph checkpoint
- Redis Stream + SSE
- 先完成可运行 10～30 分钟的 DeepSearch
### Phase 1：MVP Durable Long-Horizon
- Context/Eval/Budget/Trace Middleware
- Evidence/Artifact
- Worker crash recovery
- 目标：稳定运行 30～120 分钟并可恢复
### Phase 2：Research Quality
- Evidence quality scoring
- Claim-Evidence-Source 图
- Query rewrite / search diversification
- Source ranking / dedup
- Gap/contradiction detection
- Benchmark：coverage/citation/correctness/cost
### Phase 3：Long-Horizon Reliability
- Step retry + backoff
- Tool failure classification
- 细粒度 checkpoint
- Task resume
- Artifact versioning
- Action ledger
- Human pause/approval
- 多 Worker
### Phase 4：Adaptive Research
- 动态搜索深度
- Evidence quality 驱动继续/停止
- 动态 context budget
- 模型路由
- cost-quality frontier
### Phase 5：Multi-Agent（可选）
- 只有单 Agent 到达瓶颈后引入
- Planner/Researcher/Critic/Synthesizer
- 统一 task/evidence/state 协议
- Harness 对 Agent 数量无感
## 10. Evaluation / Benchmark

| 维度 | 指标 | 含义 |
| --- | --- | --- |
| Task Success | Success Rate | 任务是否完成 |
| Coverage | Coverage | 关键子问题覆盖 |
| Evidence | Evidence Recall / Citation Correctness | 结论是否有可靠证据 |
| Answer | Correctness / Relevance | 最终回答质量 |
| Efficiency | Steps / Search Calls / Cost | 完成代价 |
| Reliability | Recovery Success Rate | 故障后恢复 |
| Context | Growth / Compression Ratio | 上下文控制 |
| Latency | P50/P95 | 任务耗时 |


不能只评最终答案。Harness 的核心价值还包括持续运行、恢复、减少无效搜索以及预算内完成。
## 11. 推荐技术栈

| 模块 | 技术 | 原因 |
| --- | --- | --- |
| API | FastAPI | 异步 API、SSE、Pydantic、Python Agent 生态 |
| Agent Runtime | LangGraph | ReAct、State、Checkpoint、Middleware |
| Database | MySQL 8.x | Task/Run/Evidence/Artifact |
| Cache/Stream | Redis | Stream、lease、heartbeat |
| Object Storage | S3-compatible | 网页快照/大型 artifact |
| HTTP | httpx | 异步抓取 |
| Parsing | trafilatura / BeautifulSoup | 正文抽取 |
| Schema | Pydantic | 接口和状态校验 |
| ORM | SQLAlchemy | MySQL 访问 |
| Migration | Alembic | 数据库版本管理 |
| Observability | OpenTelemetry | trace/span 标准化 |


## 12. 推荐项目结构
```
app/
├── api/                    # FastAPI control plane
│   ├── tasks.py
│   ├── events.py
│   └── schemas.py
├── harness/                # Long-Horizon Task Runtime
│   ├── task_manager.py
│   ├── worker.py
│   ├── lease.py
│   ├── recovery.py
│   ├── budget.py
│   └── event_bus.py
├── agent/                  # LangGraph Runtime
│   ├── graph.py
│   ├── state.py
│   ├── tools/
│   └── middleware/
│       ├── context.py
│       ├── evaluation.py
│       ├── budget.py
│       ├── guardrail.py
│       └── tracing.py
├── storage/
│   ├── mysql/
│   ├── redis/
│   └── object_store/
├── evaluation/
│   ├── step_eval.py
│   ├── phase_eval.py
│   └── task_eval.py
└── main.py
```
## 13. 完整任务示例
```
用户：深入研究 2026 年 Long-Horizon Agent 可靠性，
比较不同 Agent Runtime 的持久化、Context、Evaluation、Recovery，
给出技术选型。

FastAPI -> MySQL create task
Worker -> acquire lease
LangGraph -> Planning
 -> Search / Open / Extract
 -> Context Middleware
 -> Evidence persist
 -> Eval Middleware 发现 research gap
 -> Agent 决定继续搜索
Worker 崩溃
 -> lease expired
 -> Recovery Worker
 -> LangGraph checkpoint resume
 -> 继续 research
 -> Synthesis
 -> Task Eval PASS
 -> MySQL COMPLETED
 -> SSE completed
```
## 14. 关键设计取舍

| 问题 | 方案 | 原因 |
| --- | --- | --- |
| Agent loop | LangGraph ReAct | 成熟 Runtime |
| Context | LangGraph Middleware | 与 ReAct 解耦 |
| Evaluation | Middleware + evaluator | 每一步可观察、反馈 Agent |
| Graph state | LangGraph Checkpointer | 复用 Runtime |
| Task state | MySQL | 跨 Run/Worker durable source of truth |
| Realtime event | Redis Stream | SSE + 实时消费 |
| Recovery | Harness lease + checkpoint resume | 处理 Worker 级故障 |
| Artifact | Object Storage + MySQL metadata | 避免大文本污染关系库 |
| Evidence | MySQL metadata + artifact | 可查询、可追踪 |
| Multi-Agent | 后置 | 先验证单 Agent 基础设施 |
| API | FastAPI | 控制面与执行面解耦 |


## 15. 最终原则
- Agent 决定下一步做什么；Harness 决定任务如何可靠地活着。
- LangGraph 管一次 Agent Run 的图状态和执行循环；Middleware 管 Agent 内横切能力。
- MySQL 管 durable task metadata；LangGraph checkpoint 管 graph state；Redis 管实时事件与租约；Object Storage 管大对象。
- Evaluation 不直接替代 Agent 决策，而是结构化回答"做得够不够、还缺什么"，由 ReAct 自己决定下一步。
- MVP 的成功标准不是"回答得像 Agent"，而是"任务可以长时间运行、失败后恢复、证据可追踪、质量可验证"。

Long-Horizon Context Engineering
上下文分层、压缩、注入与长期任务注意力保持设计
## 16. Context Engineering：Long-Horizon 的核心基础设施
对于 Long-Horizon Agent，Context Management 不是简单的"历史消息截断"或"做一个 Summary"。任务持续几十分钟甚至数小时后，Agent 面临的核心问题是：当前窗口有限，但任务状态、研究发现、证据、失败尝试、未解决问题和历史决策不断增长。如果把所有历史直接塞给模型，会产生上下文膨胀、信息稀释、旧信息干扰和注意力漂移；如果过度压缩，又会丢失当前任务所必需的事实。
因此本系统将 Context 设计为一个独立 Middleware，并采用"分层存储 + 按需检索 + 动态压缩 + 每步重建 Context"的策略。Agent 每次调用模型时都不是简单读取上一轮 messages，而是由 Context Middleware 根据当前 Phase、目标、未解决问题、最近动作和相关 Evidence 动态构造 Working Context。
### 16.1 Context 的五层模型
```
                    LLM Working Context
                           ▲
                           │ dynamic assembly
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   Hot Context        Task Context       Evidence Context
        │                  │                  │
 recent messages      goal/plan/gaps      relevant claims
 current action       progress/status     source/evidence
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
                    Cold / Archive
                           │
                  full history / artifacts
                           │
                    MySQL / Object Store
```

| 层级 | 内容 | 生命周期 | 是否每次发送给 LLM |
| --- | --- | --- | --- |
| L0 Hot Context | 最近若干轮消息、当前 tool result、当前 reasoning context | 分钟 | 是 |
| L1 Task State | Goal、Plan、Phase、Progress、Gaps、Constraints、Budget | 整个 Task | 核心字段是 |
| L2 Working Memory | 阶段摘要、关键 Findings、决策、失败尝试 | 整个 Task/Phase | 按相关性注入 |
| L3 Evidence Memory | Claim、Evidence、Source、Citation、质量评分 | 整个 Task | 按当前问题检索 |
| L4 Archive | 完整历史消息、原始网页、Artifact、旧版本 Summary | 长期 | 否，按需召回 |


核心原则：数据库/存储层保存完整事实，LLM Context 只保存当前决策所需的信息。因此不能把"Context"理解成一个不断增长的字符串，而应该理解成一个由多种 Memory 组成的动态视图。
### 16.2 每次 Agent 调用到底传什么
一次 Model Call 的 Context Builder 按照**变化频率从低到高**的顺序构造，使 prompt 前缀尽可能稳定，最大化 LLM Provider 的 prompt cache 命中率：
```
┌─────────────────────────────────────────────────────────┐
│  Stable Prefix（跨步不变，cache-friendly）              │
│                                                         │
│  1. System / Agent Policy              ← 永不变         │
│  2. Task Brief (Stable Part)           ← 任务期间不变   │
│     - original goal                                     │
│     - success criteria                                  │
│     - constraints                                       │
│  3. Research Plan (Snapshot)           ← 仅 plan 更新时变│
│     - completed items                                   │
│     - current item                                      │
│     - next candidate items                              │
│  4. Working Memory (Snapshot)          ← 仅压缩时变     │
│     - phase summary                                     │
│     - important findings                                │
│     - key decisions                                     │
│     - failed approaches                                 │
│  5. Relevant Evidence (Cached)         ← 仅 question 变时变│
│     - top-K claims/evidence/source                     │
│     - contradiction signals                             │
│                                                         │
│  ──────────── cache breakpoint ────────────             │
│                                                         │
│  Dynamic Suffix（每步可能变化）                         │
│                                                         │
│  6. Current Phase                      ← 低频，phase 转换时变│
│  7. Current Sub-question               ← 每步可能变     │
│  8. Progress + Unresolved Gaps         ← 每步变         │
│  9. Recent Interaction                 ← 每步必变       │
│     - recent messages                                   │
│     - latest tool calls/results                         │
│ 10. Current Action Context             ← 每步变         │
│     - why previous action happened                      │
│     - what information it produced                      │
│     - what needs to be decided now                     │
│ 11. Budget / Runtime State             ← 每步变         │
│     - remaining time                                    │
│     - remaining tool calls                              │
│     - estimated cost                                   │
│ 12. Evaluation Feedback                ← 每步变         │
│     - missing information                               │
│     - coverage gaps                                     │
│     - evidence quality warnings                         │
└─────────────────────────────────────────────────────────┘
```
这里最重要的是"当前决策上下文"而不是"历史聊天记录"。模型每次都应该明确知道：我是谁、我要完成什么、已经做到哪里、当前正在解决什么、缺什么、已经知道什么、下一步需要决定什么。

同时，Context 的组装顺序直接影响 prompt cache 命中率。高频变化的内容放在后面，可以让前面的稳定前缀被 LLM Provider 缓存复用。
### 16.3 Task Brief：防止长期任务"忘记目标"
Long-Horizon 中最危险的问题之一不是模型不知道某条历史信息，而是随着大量搜索和工具调用，Agent 逐渐偏离原始目标。因此每次 Model Call 都应该有一个稳定、短小的 Task Brief。

Task Brief 拆分为**稳定部分**和**动态部分**，分别注入 Stable Prefix 和 Dynamic Suffix：
```
── Task Brief (Stable Part) → 注入 Stable Prefix ──
Goal:
  比较 A/B/C 三种方案在 Long-Horizon Agent 中的可靠性

Success Criteria:
  1. 覆盖持久化
  2. 覆盖 Context Management
  3. 覆盖 Recovery
  4. 至少有官方/一手证据
  5. 给出明确技术选型

Constraints:
  - 只考虑开源方案
  - 预算 50k token

── Task Brief (Dynamic Part) → 注入 Dynamic Suffix ──
Current Phase:
  Research -> Context Management

Current Question:
  长任务中如何避免 Context Drift？

Unresolved Gaps:
  - 缺少实际 benchmark
  - 缺少官方实现细节

Next Decision:
  判断是否需要继续搜索，还是进入 synthesis
```
稳定部分在任务创建后基本不变，可以被 LLM Provider 长期缓存。动态部分每步可能变化，放在 Dynamic Suffix 中不影响前缀缓存。

Task Brief 相当于 Agent 的"导航仪"。它不随历史消息无限增长，而是随着任务状态更新保持稳定、明确和高度结构化。
### 16.4 动态压缩，而不是一次性 Summary
Context Compression 不应该设计成"超过 N token 后，把所有历史总结成一段文字"。这种方式容易把错误、无关内容和已经失效的信息一起压缩进去。推荐采用分层、事件驱动的增量压缩。
```
Recent Messages
      │
      │ threshold / phase boundary / semantic change
      ▼
Message Compactor
      │
      ├── important facts
      ├── decisions
      ├── failed attempts
      ├── discovered evidence
      └── unresolved questions
      ▼
Working Memory Update
      │
      ├── replace stale summary
      ├── preserve stable facts
      └── append new findings
      ▼
Archive old messages
```
#### 触发条件
- Token threshold：当前消息窗口达到预算阈值。
- Phase boundary：Research → Verification → Synthesis 等阶段切换。
- Semantic change：研究方向、核心结论或任务约束发生明显变化。
- Tool burst：连续大量搜索/阅读后需要归纳。
- Context pressure：Context Builder 发现可用 token 不足。
- Explicit checkpoint：完成一个重要研究阶段时主动生成稳定摘要。
#### 压缩后的结构化 Schema
```
{
  "phase_summary": "...",
  "confirmed_facts": [
    {"fact": "...", "evidence_ids": ["ev1", "ev2"]}
  ],
  "important_decisions": [
    {"decision": "...", "reason": "..."}
  ],
  "failed_attempts": [
    {"approach": "...", "reason_failed": "..."}
  ],
  "open_questions": [
    {"question": "...", "priority": "high"}
  ],
  "research_gaps": [
    {"gap": "...", "status": "open"}
  ]
}
```
关键事实不应该只存在自然语言 Summary 中，而应尽可能落成结构化 State/Evidence。这样压缩时不会因为语言模型重新总结而把事实关系丢掉。
### 16.5 Context 的"新鲜度"与版本化
Long-Horizon Task 运行时间长，Context 中存在 stale information 的风险。例如早期判断某个来源可信，后来发现其数据已经过期。因此 Working Memory 中的内容应带有 source、timestamp、phase 和 confidence 等元数据。
```
Finding
├── id
├── content
├── source/evidence_refs
├── created_at
├── last_validated_at
├── phase
├── confidence
└── status: ACTIVE / SUPERSEDED / REJECTED
```
Context Builder 默认优先注入 ACTIVE 且与当前问题相关的内容；SUPERSEDED/REJECTED 信息只在解释历史决策时按需召回。
### 16.6 相关性检索：不是"全部 Memory 都放进去"
当任务运行到数百个 Step 时，Working Memory 和 Evidence Memory 也会很大。Context Middleware 应根据当前 Sub-question 做检索，而不是把整个任务的 Findings 全部注入。
```
current_question
      │
      ├── semantic retrieval -> relevant findings
      ├── keyword / metadata -> phase / source / claim
      ├── dependency lookup -> related evidence
      └── recency boost -> recent decisions
                    │
                    ▼
              Context Ranker
                    │
                    ▼
              Top-K Context
```
可以使用"语义相关性 + 当前 Phase + Recency + Evidence Quality + Dependency"综合排序。其中 Recency 不能成为唯一标准，因为长期任务中的稳定核心事实可能很久没有出现，但仍然比最近的一条无关搜索结果重要。
### 16.7 注意力预算：把 Context 当成有限资源
Context Window 很大并不意味着应该把更多信息塞给模型。Long-Horizon Agent 更需要控制每次 Model Call 的 attention budget。

| Context 区域 | 建议策略 | 目的 |
| --- | --- | --- |
| Task Brief | 固定保留 | 防止目标漂移 |
| Current State | 固定保留 | 保持当前进度感 |
| Current Question | 固定保留 | 聚焦当前决策 |
| Unresolved Gaps | 固定保留 | 避免遗漏任务缺口 |
| Recent Actions | 保留最近若干步 | 理解短期因果链 |
| Working Memory | 按相关性 Top-K | 提供长期经验 |
| Evidence | 按 Claim/Question Top-K | 提供事实依据 |
| Old Messages | 默认不注入 | 避免噪声 |
| Raw Artifacts | 按需读取 | 避免大文本污染 |


因此 Context Builder 的目标不是 maximize context utilization，而是 maximize decision-relevant information density。
### 16.8 Context Middleware 生命周期
```
before_model(state):
    1. Read Task State
    2. Determine current phase/question
    3. Check context pressure
    4. Compact recent messages if needed
    5. Check cache validity:
       a. current_question_hash == last_injected_question_hash?
          -> yes: reuse Working Memory + Evidence block (skip 6-7)
       b. plan_version == last_injected_plan_version?
          -> yes: reuse Research Plan block (skip 6)
       c. working_memory_version == last_injected_wm_version?
          -> yes: reuse Working Memory block
    6. Retrieve relevant findings (if question changed or first call)
    7. Retrieve relevant evidence (if question changed or first call)
    8. Build Task Brief (stable + dynamic parts)
    9. Inject eval feedback
    10. Apply token budget
    11. Emit CONTEXT_BUILT event (with cache hit/miss stats)
    12. Call LLM

after_model(result):
    1. Extract important facts/decisions if needed
    2. Update structured state
    3. Update working memory (bump version if changed)
    4. Update plan (bump version if changed)
    5. Record context statistics (including cache hit rate)
    6. Emit CONTEXT_UPDATED event
```
### 16.9 Context Cache Strategy：最大化 prompt cache 命中率
Long-Horizon Agent 在单个任务中可能产生数百次 LLM 调用。如果每次调用都全量重建 Context，LLM Provider 侧的 prompt cache 命中率接近 0%，造成大量重复 token 计费和延迟。Context Middleware 应通过分层快照和按变化频率排列来最大化缓存命中率。

#### 设计原则
- **按变化频率分层排列**：稳定内容在前，高频变化内容在后，让前缀尽可能可缓存。
- **快照 + 版本号**：低频变化的 block（Research Plan、Working Memory、Relevant Evidence）用 version 标记，未变则不重建。
- **Question-level Evidence 缓存**：Relevant Evidence 按当前 sub-question 的 hash 缓存，同一 sub-question 连续搜索时复用。
- **Cache breakpoint**：在 Stable Prefix 和 Dynamic Suffix 之间明确标注断点。

#### Context 分层与缓存策略

| Context 区域 | 变化频率 | 缓存策略 | 命中条件 |
| --- | --- | --- | --- |
| System / Agent Policy | 永不变 | 静态缓存 | 始终命中 |
| Task Brief (Stable Part) | 任务期间不变 | 静态缓存 | 同一任务始终命中 |
| Research Plan (Snapshot) | 仅 plan 更新时变 | version-based snapshot | plan_version 未变则命中 |
| Working Memory (Snapshot) | 仅压缩/新发现时变 | version-based snapshot | wm_version 未变则命中 |
| Relevant Evidence (Cached) | 仅 question 变时变 | question-hash cache | current_question_hash 未变则命中 |
| Current Phase | 低频，phase 转换时变 | 无缓存，但在前缀后 | — |
| Current Sub-question | 每步可能变 | 无缓存 | — |
| Progress + Gaps | 每步变 | 无缓存 | — |
| Recent Messages | 每步必变 | 无缓存 | — |
| Budget / Runtime | 每步变 | 无缓存 | — |
| Eval Feedback | 每步变 | 无缓存 | — |

#### 缓存命中场景与预期收益

| 场景 | Stable Prefix | Plan + Working Memory | Evidence | 总命中率 |
| --- | --- | --- | --- | --- |
| 同一 sub-question 连续搜索（换关键词） | 命中 | 命中 | 命中 | ~80%+ |
| 同一 phase 内不同 sub-question | 命中 | 命中 | 未命中 | ~60% |
| Phase 转换后第一步 | 命中 | 可能命中 | 未命中 | ~40% |
| 任务恢复后第一步 | 命中 | 可能命中 | 可能命中 | ~30-50% |

对于 Long-Horizon 任务（几百步），同一 sub-question 连续搜索和同一 phase 内多步是主要场景，缓存命中率可以从接近 0% 提升到 60-80%。

#### 实现要点
```
ContextBlock:
  - content: str           # block 内容
  - version: int           # 快照版本号
  - content_hash: str      # 内容 hash，用于缓存 key
  - last_injected_step: int # 上次注入的 step 序号

ContextCacheManager:
  - question_hash -> evidence_block   # sub-question 到 evidence 的缓存
  - plan_version -> plan_block        # plan 版本到 plan block 的缓存
  - wm_version -> wm_block            # working memory 版本到 block 的缓存

before_model:
  1. 计算 current_question_hash
  2. 如果 hash 未变 -> 复用 evidence_block
  3. 如果 plan_version 未变 -> 复用 plan_block
  4. 如果 wm_version 未变 -> 复用 wm_block
  5. 只重建发生变化的 block
  6. 按 Stable Prefix → cache breakpoint → Dynamic Suffix 顺序拼接
```

#### 注意事项
- 缓存的是**组装后的 block 文本**，不是 LLM 的 response。LLM Provider 的 prompt cache 是透明的，我们只需保证 prompt 前缀稳定即可。
- 压缩触发时 Working Memory version 会 bump，此时该 block 缓存失效，但 System + Task Brief + Plan 仍命中。
- 如果使用 Anthropic prompt caching API，可以在 cache breakpoint 处显式设置 cache_control，让 Provider 自动管理缓存。
- 缓存命中统计应加入 Context Quality Eval 的指标中（见 16.11）。

### 16.10 防止 Context Drift 的机制
- Goal anchoring：每次 Model Call 都注入稳定 Task Brief。
- State anchoring：始终提供 current phase、current question、progress。
- Gap anchoring：始终暴露 unresolved gaps，避免"搜了很多但漏掉关键问题"。
- Evidence anchoring：事实尽量引用结构化 evidence，而不是依赖记忆中的自然语言。
- Plan anchoring：保留已完成/当前/待完成研究项。
- Evaluation anchoring：把评估产生的缺口显式注入。
- Compression：定期移除低价值历史，降低噪声。
- Relevance retrieval：只召回与当前决策相关的长期记忆。
- Phase transition：阶段变化时重新整理 Working Memory，防止旧阶段信息污染新阶段。
- Contradiction check：发现新证据与旧 Finding 冲突时，标记旧 Finding 为 SUPERSEDED，而不是简单追加。
### 16.11 Context Quality Evaluation
Context 本身也应该进入 Eval，而不是只评估最终答案。

| 指标 | 含义 |
| --- | --- |
| Goal Retention | 模型是否仍围绕原始任务目标行动 |
| State Awareness | 是否正确理解当前 Phase/Progress |
| Gap Awareness | 是否知道当前尚未解决的问题 |
| Evidence Utilization | 是否正确使用相关 Evidence |
| Context Relevance | 注入内容中与当前决策真正相关的比例 |
| Compression Loss | 压缩前后关键事实是否丢失 |
| Context Drift | 随着 Step 增长，Agent 是否逐渐偏离任务 |
| Decision Quality | 当前 Context 是否足以支持正确下一步行动 |
| Cache Hit Rate | prompt cache 命中率，反映 Context 组装是否按变化频率分层 |


尤其建议建立 Long-Horizon Context Benchmark：让同一个任务运行 20、50、100、200 个 Step，比较不同 Context Strategy 下的 Goal Retention、Gap Awareness、Evidence Utilization、Cache Hit Rate 和最终 Task Success。
### 16.12 Context Middleware 与 Eval Middleware 的协作
```
                ┌────────────────────────────┐
                │      Current Task State     │
                └─────────────┬──────────────┘
                              ▼
                     Context Middleware
                              │
                 build decision context
                              │
                              ▼
                           LLM
                              │
                        tool / action
                              │
                              ▼
                     Evaluation Middleware
                              │
               ┌──────────────┼──────────────┐
               │              │              │
            PASS          INCOMPLETE      DRIFT
               │              │              │
               │              ▼              ▼
               │         missing gaps    re-anchor
               │              │
               └──────────────┴──────► Context Update
                                           │
                                           ▼
                                      next Model Call
```
Eval 发现"当前研究缺少某个证据"时，不应该直接代替 Agent 搜索；它应该把 missing gap 写入结构化状态。下一轮 Context Middleware 将该 gap 提升到高优先级 Context，Agent 再决定如何解决。
### 16.13 MVP 到后续版本的 Context 迭代

| 阶段 | Context 能力 |
| --- | --- |
| MVP | Recent messages + Task State + Summary + 基础 token budget |
| V1 | Working Memory + Evidence Retrieval + Phase-aware Context |
| V2 | 结构化 Finding/Decision/Gap + 增量压缩 + stale information |
| V3 | Context Ranker + contradiction handling + dynamic token allocation |
| V3.5 | Context Cache Strategy（snapshot + version-based + question-hash） |
| V4 | Context Quality Eval + Long-Horizon Context Benchmark |
| V5 | 根据任务阶段/模型能力动态选择 Context Strategy |


### 16.14 这一设计最终解决什么问题
Long-Horizon Agent 的 Context Management 最终要解决的不是"怎么让模型记住更多"，而是"怎么让模型在很长的任务中始终知道当前最重要的事情"。
```
Long Task
   ↓
大量历史不断增长
   ↓
分层存储
   ↓
结构化 Task State
   ↓
Working Memory
   ↓
Evidence Memory
   ↓
Relevant Retrieval
   ↓
Dynamic Compression
   ↓
Task Brief + Current State + Current Question
   ↓
Focused Working Context
   ↓
Agent 保持对任务目标、进度、缺口和证据的持续感知
```
因此本系统的 Context Middleware 可以理解为 Long-Horizon Agent 的"认知状态管理层"：它不负责替 Agent 做决定，而是确保 Agent 在每一次决策时都拥有足够、相关、最新且结构化的信息。
## 17. 更新后的核心架构定位
经过 Context Engineering 加强后，本项目的核心不是简单的"DeepSearch + LangGraph"，而是一个以 Harness 为外层、LangGraph 为执行 Runtime、Middleware 为 Agent Cognitive Infrastructure 的 Long-Horizon Agent 系统。
```
                    Long-Horizon Harness
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   Task Runtime          Durable State        Recovery
        │                    │                    │
        └────────────────────┼────────────────────┘
                             ▼
                       LangGraph Runtime
                             │
                         ReAct Loop
                             │
        ┌────────────────────┼─────────────────────┐
        │                    │                     │
 Context Middleware    Evaluation Middleware   Budget/Guardrail
        │                    │                     │
        ▼                    ▼                     ▼
 Context Engineering    Quality Feedback      Runtime Control
        │                    │                     │
        └────────────────────┼─────────────────────┘
                             ▼
                     Search / Read / Tools
                             │
                             ▼
                  Evidence / Artifact Storage
```
## 18. 更新后的项目核心卖点
- Durable Agent：把一次性 Agent Call 变成可恢复的 Long-Horizon Task。
- Context Engineering：通过分层 Memory、动态压缩、相关性检索和 Task Brief，让 Agent 在数百个 Step 后仍然保持目标、状态和研究缺口感知。
- Evidence-driven DeepSearch：围绕 Claim-Evidence-Source 构建研究闭环，而不是简单堆叠 Search Call。
- Middleware Architecture：Context、Evaluation、Budget、Guardrail、Tracing 与 ReAct 主循环解耦。
- Observable Runtime：Task/Run/Step/Event 全链路可追踪，客户端断线不影响后台任务。
- Reliable Execution：Lease、Heartbeat、Checkpoint、Recovery 解决 Worker Crash 和长任务中断问题。
## 19. 面试时推荐的 Context Engineering 表述
如果被问"Long-Horizon Agent 最难的问题是什么"，推荐不要回答单纯的上下文窗口，而是回答：
```
Long-Horizon 最大的问题之一是 Context Drift。
任务运行几十分钟甚至几小时后，历史消息、搜索结果和中间结论会不断增长。
如果全部保留，噪声会越来越大；如果简单 Summary，又容易丢失关键事实。

所以我把 Context Management 独立成 LangGraph Middleware，
把 Context 分成 Hot Context、Task State、Working Memory、Evidence Memory 和 Archive。
每次调用 LLM 时不直接读取全部历史，而是根据当前 Phase 和 Sub-question，
动态组装 Task Brief、当前状态、未解决 Gap、相关 Findings、相关 Evidence 和最近动作。

同时通过增量压缩、阶段切换压缩和相关性检索控制 Context 大小。
Eval Middleware 发现研究缺口后会更新 Gap，
下一轮 Context Builder 再把这个 Gap 提升到高优先级。

这样 Agent 每一步看到的都不是"最多的信息"，
而是"当前决策最需要的信息"，从而降低长期运行过程中的 Context Drift 和注意力涣散。
```