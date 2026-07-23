"""
SQLAlchemy setup: engine, session factory, and declarative base.

check_same_thread=False is required because FastAPI handles requests across
multiple threads, and SQLite's default only allows access from the thread
that opened the connection.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency: one DB session per request, closed on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
