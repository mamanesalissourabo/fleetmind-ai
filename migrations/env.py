"""Exécution des migrations, avec la connexion définie dans DATABASE_URL."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from backend.core.config import get_settings
from backend.models import alert, chat_log, conversation, monitoring_run, user  # noqa: F401  (enregistre les tables)
from backend.models.base import Base

if context.config.config_file_name:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

DATABASE_URL = get_settings().database_url
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL n'est pas définie dans .env")

if context.is_offline_mode():
    context.configure(url=DATABASE_URL, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(DATABASE_URL).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
