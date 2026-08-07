from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repo root, where data/ lives.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


class ClaudeModel(StrEnum):
    """Claude model IDs used by the agent layer."""

    HAIKU_4_5 = "claude-haiku-4-5"
    SONNET_5 = "claude-sonnet-5"
    OPUS_5 = "claude-opus-5"


class Settings(BaseSettings):
    """Application settings, loaded from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    anthropic_api_key: str | None = None
    # Haiku during development to conserve API credits; switch to OPUS_5 for the
    # final demo runs (see docs/tech-stack.md for the latency rationale).
    anthropic_model: ClaudeModel = ClaudeModel.HAIKU_4_5
    # Override with DATA_DIR in Docker, where data/ is mounted elsewhere.
    data_dir: Path = _REPO_ROOT / "data"

    # --- Local trace store (see docs/observability.md) ---
    obs_enabled: bool = True
    # SQLAlchemy URL. SQLite by default so `uv run uvicorn` and pytest need no
    # container; docker-compose overrides this with the Postgres service. The
    # store is deliberately reachable by URL alone — that is the whole
    # portability claim, so keep the SQL dialect-neutral.
    obs_database_url: str = f"sqlite+pysqlite:///{_REPO_ROOT / 'traces.db'}"
    # Prompts and completions are useful for debugging a bad plan, but the
    # message history grows every turn, so it is stored truncated.
    obs_capture_content: bool = True
    obs_content_max_chars: int = 2000
    obs_retention_days: int = 7
    # The retention sweep piggybacks on span export rather than a scheduler.
    obs_sweep_interval_seconds: int = 3600


settings = Settings()
