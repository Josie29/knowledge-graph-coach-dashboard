from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    anthropic_api_key: str | None = None


settings = Settings()
