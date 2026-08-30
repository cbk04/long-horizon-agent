# Alembic env.py — load models from app.storage.mysql for autogenerate support.

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add project root to sys.path so `app` is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.config import get_settings  # noqa: E402
from app.storage.mysql.database import Base  # noqa: E402
from app.storage.mysql import models as _mysql_models  # noqa: F401, E402  — register all models on Base.metadata
from app.harness import models as _harness_models  # noqa: F401, E402  — register TaskEvent on Base.metadata

config = context.config

# Override sqlalchemy.url with settings
config.set_main_option("sqlalchemy.url", get_settings().mysql_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
