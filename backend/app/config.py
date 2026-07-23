"""
Central app configuration via pydantic-settings.

DATABASE_URL defaults to a local SQLite file. Swap it for a Postgres URL
to move to a multi-user setup without touching any other code.
"""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = "sqlite:///./insar_app.db"
    downloads_dir: str = "./downloads"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    # Fernet key for encrypting stored credentials.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If absent, a new key is generated at startup (credentials won't survive restarts).
    secret_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
Path(settings.downloads_dir).mkdir(parents=True, exist_ok=True)
