"""
Central app configuration via pydantic-settings.

DATABASE_URL defaults to a local SQLite file. Swap it for a Postgres URL
to move to a multi-user setup without touching any other code.
"""

from pydantic_settings import BaseSettings
from pathlib import Path


APP_VERSION = "0.2.0"


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
    # rclone executable used for backups (name on PATH or a full path).
    rclone_path: str = "rclone"
    # When set, the whole API requires signing in with this password.
    # Leave empty only on a machine nobody else can reach (localhost, a VPN).
    app_password: str = ""
    # Folder holding the built frontend (index.html and assets). When set, the
    # backend serves the UI itself, so one port and one container is enough.
    frontend_dir: str = ""
    # On start, resume batches whose submission was cut short, queue downloads that
    # never ran and restart MintPy runs that were interrupted. Tests switch it off.
    recover_on_startup: bool = True

    class Config:
        env_file = ".env"


settings = Settings()
Path(settings.downloads_dir).mkdir(parents=True, exist_ok=True)
