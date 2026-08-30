# Long-Horizon DeepSearch Agent Harness

> FastAPI + LangGraph + Middleware + MySQL + Redis

## 快速开始

```bash
# 安装依赖（使用 uv）
uv sync

# 数据库迁移
alembic upgrade head

# 启动 FastAPI 控制面
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 启动 Worker（后台执行 LangGraph）
python -m app.harness.worker
```

## 技术栈

| 模块 | 技术 |
| --- | --- |
| API | FastAPI |
| Agent Runtime | LangGraph |
| Database | MySQL 8.x |
| Cache/Stream | Redis |
| HTTP | httpx |
| Parsing | trafilatura / BeautifulSoup |
| Schema | Pydantic |
| ORM | SQLAlchemy |
| Migration | Alembic |

## 架构

三层职责分离：Agent 决定下一步做什么；Harness 决定任务如何可靠地活着。

1. **FastAPI Control Plane** — 创建/查询/控制 Task
2. **Harness / Long-Horizon Task Runtime** — 任务全生命周期管理
3. **LangGraph Agent Runtime** — ReAct 执行循环 + Middleware
