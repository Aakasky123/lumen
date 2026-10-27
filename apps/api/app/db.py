from collections.abc import Generator
from threading import Lock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
_init_lock = Lock()


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    from app import models  # noqa: F401

    with _init_lock:
        Base.metadata.create_all(bind=engine)
        _ensure_sqlite_model_provider_columns()
        _ensure_sqlite_user_profile_columns()
        _ensure_sqlite_paired_device_columns()


def _ensure_sqlite_model_provider_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    additions = {
        "display_name": "VARCHAR(255) DEFAULT ''",
        "provider_type": "VARCHAR(64) DEFAULT 'custom_openai_compatible'",
        "model_name": "VARCHAR(255) DEFAULT ''",
        "base_url": "VARCHAR(1024)",
        "encrypted_api_key": "TEXT",
        "api_key_hint": "VARCHAR(64)",
        "temperature": "FLOAT DEFAULT 0.2",
        "max_tokens": "INTEGER DEFAULT 2048",
        "timeout_seconds": "INTEGER DEFAULT 30",
        "reasoning_effort": "VARCHAR(16)",
        "structured_output": "VARCHAR(16)",
        "endpoint_preference": "VARCHAR(128)",
        "endpoint_routing": "VARCHAR(256)",
        "enabled": "BOOLEAN DEFAULT 1",
        "is_active": "BOOLEAN DEFAULT 0",
        "health_status": "VARCHAR(64) DEFAULT 'unknown'",
        "last_error": "TEXT",
        "last_tested_at": "DATETIME",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='model_providers'"
        ).fetchone()
        if not table_exists:
            return
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(model_providers)").fetchall()}
        for column, column_type in additions.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE model_providers ADD COLUMN {column} {column_type}")


def _ensure_sqlite_user_profile_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    additions = {"agent_settings_json": "JSON"}
    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_profiles'"
        ).fetchone()
        if not table_exists:
            return
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user_profiles)").fetchall()}
        for column, column_type in additions.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE user_profiles ADD COLUMN {column} {column_type}")


def _ensure_sqlite_paired_device_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    additions = {"autonomy_until": "DATETIME"}
    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paired_devices'"
        ).fetchone()
        if not table_exists:
            return
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(paired_devices)").fetchall()}
        for column, column_type in additions.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE paired_devices ADD COLUMN {column} {column_type}")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
