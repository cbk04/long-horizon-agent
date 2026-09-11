"""Application configuration via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic-settings' env_file only populates the Settings object; libraries that
# read os.environ directly (e.g. LangSmith tracing flags) need a real load.
# load_dotenv does not override variables already set in the environment.
load_dotenv()


class Settings(BaseSettings):
    """Central configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MySQL ── (must be set in .env or environment)
    mysql_host: str = Field(default="", description="MySQL host")
    mysql_port: int = Field(default=3306, description="MySQL port")
    mysql_user: str = Field(default="", description="MySQL user")
    mysql_password: str = Field(default="", description="MySQL password")
    mysql_database: str = Field(default="", description="MySQL database name")

    @property
    def mysql_url(self) -> str:
        """SQLAlchemy async-style URL is not needed; we use sync PyMySQL."""
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @property
    def mysql_url_async(self) -> str:
        """Async URL for SQLAlchemy async engine (aiomysql)."""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    # ── Redis ── (must be set in .env or environment)
    redis_host: str = Field(default="", description="Redis host")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_db: int = Field(default=0, description="Redis db index")
    redis_password: str = Field(default="", description="Redis password")

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ── LLM ──
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o"

    # ── Search ──
    # 搜索后端:auto=tavily(有 key)→bing→ddg;也可显式 tavily / bing / ddg
    search_provider: str = Field(default="auto", description="auto | tavily | bing | ddg")
    tavily_api_key: str = Field(default="", description="Tavily API key;留空则回落 bing/ddg")

    # ── Evaluator (三阶段评估门) ──
    # 弱模型档:仅用于 phase 3 criteria 打分(rubric 机械对照);留空回落主模型档。
    evaluator_score_model: str = Field(default="", description="打分用弱模型名;空则回落 llm_model")
    evaluator_score_api_key: str = Field(default="", description="打分模型 api key;空则回落 openai_api_key")
    evaluator_score_base_url: str = Field(default="", description="打分模型 base url;空则回落 openai_base_url")
    # 反向挑战 agent 的工具轮数上限(react 循环,不设限会失控)
    reverse_max_tool_calls: int = Field(default=5, description="反向 agent 搜索轮数上限")
    # 争议回应(defend)轮数预算,独立于 stage 重试预算
    max_defense_rounds: int = Field(default=1, description="每 stage 争议回应轮数上限")
    # stage 加权分通过线
    score_pass_threshold: float = Field(default=0.7, description="stage 加权分通过阈值")
    # 超过此权重的 criterion 得 0 分即整体不通过(主命题保护)
    heavy_criteria_weight: float = Field(default=0.3, description="主命题权重判线")
    # 评估器自身 LLM 调用熔断:超限后跳过 phase 2,只跑规则+打分
    max_eval_llm_calls_per_task: int = Field(default=100, description="每任务评估 LLM 调用熔断上限")
    # 每阶段重试上限(含首跑)——从 controller 常量迁出
    max_stage_retries: int = Field(default=3, description="每 stage 最大尝试次数(含首跑)")
    # 依赖感知交接:被依赖阶段的全量结论内联进当前阶段 prompt 时的字符上限
    max_inline_conclusion_chars: int = Field(
        default=4000, description="内联被依赖阶段结论的每阶段字符上限"
    )
    # web_fetch 把正文压成「开头摘录+段落提纲+结尾摘录」的摘要并落库全文,
    # 避免整页正文灌入 agent 现场上下文。以下为各段字符预算。
    web_fetch_digest_head_chars: int = Field(default=1200, description="web_fetch 摘要开头摘录字符数")
    web_fetch_digest_tail_chars: int = Field(default=400, description="web_fetch 摘要结尾摘录字符数")
    web_fetch_digest_outline_chars: int = Field(default=600, description="web_fetch 摘要段落提纲字符预算")

    # ── Checkpointer ──
    # 图状态(checkpoint)后端:mysql=PyMySQLSaver 落库,跨进程可恢复;
    # memory=进程内 MemorySaver,重启即丢(仅开发/测试)。
    checkpoint_backend: str = Field(default="mysql", description="mysql | memory")

    # ── Worker ──
    worker_id: str = "worker-1"
    worker_poll_interval_seconds: int = 5

    # ── Budget Defaults ──
    default_max_tokens: int = 200_000
    default_max_seconds: int = 1800
    default_max_tool_calls: int = 50
    default_max_cost: float = 5.0

    # ── API ──
    api_host: str = "0.0.0.0"
    api_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
