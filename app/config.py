"""Application configuration via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
