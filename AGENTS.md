# AGENTS.md

> 本仓库目前处于技术设计阶段，尚无代码实现。以下内容基于 `Long_Horizon_DeepSearch_Harness_Technical_Design_v3_Context_Engineering.md` 技术设计文档提炼，供后续开发参考。

## 项目概述

Long-Horizon DeepSearch Agent Harness — 一个支持长时间运行（数十分钟至数小时）的 DeepSearch Agent 系统。核心不是"再实现一个 ReAct Agent"，而是构建一个 Long-Horizon Task Runtime，使用 LangGraph 承担 Agent 图状态与 ReAct 执行循环，通过 Middleware 解耦 Context、Evaluation、Budget、Guardrail、Tracing 等横切能力。

技术栈：FastAPI + LangGraph + MySQL + Redis + Object Storage。

## 开发命令（规划）

项目尚无代码，以下为设计文档中规划的技术栈对应命令，实现后需根据实际项目配置调整。

### 安装依赖
```bash
pip install -r requirements.txt
```

### 数据库迁移（Alembic）
```bash
# 生成迁移脚本
alembic revision --autogenerate -m "description"
# 执行迁移
alembic upgrade head
```

### 启动 FastAPI 控制面
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 启动 Worker（后台执行 LangGraph）
```bash
python -m app.harness.worker
```

### 运行测试
```bash
# 全部测试
pytest
# 单个测试文件
pytest tests/test_task_manager.py
# 单个测试用例
pytest tests/test_task_manager.py::test_create_task
# 带覆盖率
pytest --cov=app --cov-report=term-missing
```

## 架构总览

### 三层职责分离

系统采用严格的分层架构，核心原则是"Agent 决定下一步做什么；Harness 决定任务如何可靠地活着"。

1. **FastAPI Control Plane** — 只负责创建/查询/控制 Task（创建后立即返回 task_id），不承担长任务执行。API 层与 Worker 完全解耦，客户端断开不影响后台任务。
2. **Harness / Long-Horizon Task Runtime** — 负责任务全生命周期：Task/Run/Step 状态管理、Worker lease/heartbeat、崩溃恢复、Budget 控制、Event/Artifact/Evidence 生命周期管理。Task-level durable state 存 MySQL，实时协调用 Redis。
3. **LangGraph Agent Runtime** — ReAct 主循环与 graph state，一次 Agent Run 的 checkpoint/resume，承载 Middleware 生命周期。LangGraph Checkpointer 负责 graph state，系统层只维护 task-level durable state。

### Middleware 层（Agent Cognitive Infrastructure）

Middleware 是 Observer/Controller，不是第二个 Agent。它产生结构化反馈，由 ReAct Agent 自己决定下一步。

| Middleware | 职责 |
| --- | --- |
| Context | 压缩、摘要、相关历史/evidence 注入 — 每次 Model Call 动态构造 Working Context |
| Evaluation | Step/Phase/Task 三层质量检查，输出 PASS/INCOMPLETE/RETRY/REPLAN |
| Budget | token/time/tool/cost 限制，输出 continue/stop/degrade |
| Guardrail | 工具权限、域名、输入输出校验 |
| Trace | model/tool/eval/context 可观测性 |
| Tool Policy | 根据阶段限制/推荐工具 |

### Context Engineering（核心设计）

Context 不是"不断增长的字符串"，而是由多种 Memory 组成的动态视图。采用五层模型：

- **L0 Hot Context** — 最近消息、当前 tool result（每次发送给 LLM）
- **L1 Task State** — Goal/Plan/Phase/Progress/Gaps/Constraints/Budget（核心字段每次发送）
- **L2 Working Memory** — 阶段摘要、Findings、决策、失败尝试（按相关性注入）
- **L3 Evidence Memory** — Claim/Evidence/Source/Citation/质量评分（按当前问题检索）
- **L4 Archive** — 完整历史、原始网页、Artifact（按需召回）

每次 Model Call 的 Context 按**变化频率从低到高**组装，最大化 LLM Provider prompt cache 命中率：稳定前缀（System → Task Brief → Research Plan → Working Memory → Relevant Evidence）在 cache breakpoint 前，动态后缀（Current Phase/Question/Progress/Recent Messages/Budget/Eval Feedback）在后面。

### DeepSearch 执行模型与 Evidence-first

执行流程：Goal → Plan → Search → Read → Extract Evidence → Evaluate Coverage → (incomplete → Identify Gaps → Re-search) → Synthesize → Final Eval → Completed。

Evidence 链路：Search Result（候选）→ Source（来源）→ Evidence（可归属于 claim 的片段）→ Claim（事实性判断）→ Citation（Claim 与 Evidence/Source 关系）。最终输出形成 Claim → Evidence → Source 链路。

### Worker Lease / Recovery 机制

1. Worker 从 QUEUED 领取 task
2. MySQL 原子更新 status=RUNNING + lease_owner + lease_expire_at
3. Worker heartbeat → 执行 LangGraph → 持久化 event + checkpoint
4. Worker 崩溃 → lease 过期 → Recovery Worker 抢占 → 从最新 LangGraph checkpoint resume（不从头重跑）

每个 action 生成 action_id，Recovery 后检查是否已完成，保证幂等性。

### 数据架构

| 组件 | 职责 | 权威性 |
| --- | --- | --- |
| MySQL | Task/Run/Step/Evidence/Artifact metadata、预算、状态 | Task metadata source of truth |
| Redis | SSE event stream、lease、heartbeat、实时状态、rate limit | 实时协调层 |
| Object Storage | 网页快照、原始文档、大型 artifact | 二进制 artifact source of truth |
| LangGraph Checkpointer | graph state / thread checkpoint | Agent graph state |

MySQL 核心表：`task`、`agent_run`、`step`、`evidence`、`artifact`、`task_budget_usage`。
Redis Key 模式：`task:{task_id}:events`（Stream）、`:lease`、`:heartbeat`、`:runtime`、`:cancel`、`:budget`。

### 规划项目结构

```
app/
├── api/              # FastAPI control plane (tasks, events, schemas)
├── harness/          # Long-Horizon Task Runtime (task_manager, worker, lease, recovery, budget, event_bus)
├── agent/            # LangGraph Runtime (graph, state, tools/, middleware/)
├── storage/          # mysql/, redis/, object_store/
├── evaluation/       # step_eval, phase_eval, task_eval
└── main.py
```

### 关键设计取舍

- Task 状态用 MySQL（跨 Run/Worker durable），不依赖 LangGraph checkpoint 做 task-level 持久化。
- 实时事件用 Redis Stream（SSE + 重连补齐），不用 MySQL 轮询。
- Artifact 用 Object Storage + MySQL metadata，避免大文本污染关系库。
- Evaluation 不替代 Agent 决策，而是结构化回答"做得够不够、还缺什么"。
- MVP 不做 Multi-Agent，先验证单 Agent 基础设施。
- Context 缓存策略：block 级快照 + version 号 + question-hash，同一 sub-question 连续搜索时缓存命中率可达 60-80%。

## 迭代路线

- **Phase 0**：FastAPI + MySQL + Redis + LangGraph 骨架，可运行 10-30 分钟 DeepSearch
- **Phase 1**：MVP Durable Long-Horizon — Context/Eval/Budget/Trace Middleware + Worker crash recovery，稳定运行 30-120 分钟并可恢复
- **Phase 2**：Research Quality — Evidence scoring、Claim-Evidence-Source 图、Gap/contradiction detection
- **Phase 3**：Long-Horizon Reliability — Step retry、细粒度 checkpoint、Action ledger、多 Worker
- **Phase 4**：Adaptive Research — 动态搜索深度、模型路由、cost-quality frontier
- **Phase 5**：Multi-Agent（可选）— 仅在单 Agent 到达瓶颈后引入
