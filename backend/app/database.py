"""
SQLAlchemy setup: engine, session factory, and declarative base.

check_same_thread=False is required because FastAPI handles requests across
multiple threads, and SQLite's default only allows access from the thread
that opened the connection.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

# Several background workers (polling_service, the download/SLC/EGMS queues,
# batch_submission, storage_move) each open their own SessionLocal() from a
# separate thread and commit independently, sometimes at nearly the same
# moment as a request handler's own session. SQLite's default rollback-
# journal mode serializes writers and errors out fast ("database is locked")
# under exactly that pattern; WAL mode lets readers and a writer proceed
# concurrently, and a longer busy timeout gives a genuinely-contending
# writer time to retry instead of failing the request/thread outright.
connect_args: dict = {"check_same_thread": False}
if _is_sqlite:
    connect_args["timeout"] = 30  # seconds to wait on a lock before raising

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency: one DB session per request, closed on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
