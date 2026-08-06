from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repo root, where data/ lives.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings, loaded from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    anthropic_api_key: str | None = None
    # Override with DATA_DIR in Docker, where data/ is mounted elsewhere.
    data_dir: Path = _REPO_ROOT / "data"


settings = Settings()
