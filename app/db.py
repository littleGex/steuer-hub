from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base
from .settings import get_settings

_engine = None
_factory = None


def configure(force: bool = False) -> None:
    """Create the engine and tables. SQLite must live on LOCAL disk, never on a network share."""
    global _engine, _factory
    if _engine is not None and not force:
        return
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{s.db_path}", connect_args={"check_same_thread": False, "timeout": 15}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):  # noqa: ANN001
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    _engine = engine
    _factory = sessionmaker(engine, expire_on_commit=False)


def get_db():
    """FastAPI dependency."""
    configure()
    with _factory() as session:
        yield session


@contextmanager
def session_scope():
    configure()
    with _factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
