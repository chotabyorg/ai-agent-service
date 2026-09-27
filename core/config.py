"""Application configuration for ai-agent-service."""

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings for the microservice."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="ai-agent-service")
    app_version: str = Field(default="0.1.0")
    debug: bool = Field(default=False)
    api_v1_prefix: str = Field(default="/v1")

    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="ai_agent_service")
    postgres_user: str = Field(default="postgres")
    postgres_password: str = Field(default="postgres")
    postgres_echo: bool = Field(default=False)

    redis_host: str = Field(default="redis")
    redis_port: int = Field(default=6379)
    redis_db: int = Field(default=0)
    redis_password: str | None = Field(default=None)
    history_ttl_seconds: int = Field(default=86400)
    history_max_items: int = Field(default=20)

    llm_provider: str = Field(default="openrouter")
    llm_api_key: str = Field(default="")
    llm_base_url: str = Field(default="https://openrouter.ai/api/v1")
    llm_default_model: str = Field(default="google/gemini-2.5-flash")
    llm_default_temperature: float = Field(default=0.7)
    llm_max_tokens: int = Field(default=1024)
    llm_http_referer: str | None = Field(default=None)
    llm_app_title: str | None = Field(default=None)

    privacy_filter_enabled: bool = Field(default=False)
    privacy_filter_base_url: str = Field(default="http://privacy-filter-service:8000")
    privacy_filter_timeout_seconds: float = Field(default=30.0)

    encryption_key: SecretStr = Field(default="")

    internal_api_token: str = Field(default="")

    # Dedicated credential for callers of /v1/chat/completions. It is
    # intentionally independent from INTERNAL_API_TOKEN, which protects the
    # existing management endpoints.
    chat_s2s_token: SecretStr = Field(default="")
    chat_s2s_auth_required: bool = Field(default=False)

    @model_validator(mode="after")
    def require_chat_s2s_token_when_auth_is_strict(self) -> "Settings":
        """Fail startup if strict chat auth is enabled without its credential."""
        token = self.chat_s2s_token.get_secret_value().strip()
        if self.chat_s2s_auth_required and not token:
            raise ValueError(
                "CHAT_S2S_TOKEN must be configured when CHAT_S2S_AUTH_REQUIRED is enabled"
            )
        return self

    knowledge_base_enabled: bool = Field(default=False)
    knowledge_base_service_url: str = Field(default="http://knowledge-base-service:8000")
    knowledge_base_timeout_seconds: float = Field(default=2.0)
    knowledge_base_max_chars_per_chunk: int = Field(default=1000)
    knowledge_base_max_total_context_chars: int = Field(default=3000)

    subscription_service_url: str = Field(default="")
    subscription_check_enabled: bool = Field(default=False)

    mcp_services: dict = Field(default={
        "bitrix": {
            "script_path": "mcp_clients/servers/bitrix_server.py",
            "env_mapping": {"webhook_url": "B24_WEBHOOK_URL"},
        },
        "google_workspace": {
            "script_path": "mcp_clients/servers/google_workspace/server.py",
            "env_mapping": {
                "client_id": "GOOGLE_CLIENT_ID",
                "client_secret": "GOOGLE_CLIENT_SECRET",
                "refresh_token": "GOOGLE_REFRESH_TOKEN",
            },
            "optional_env_mapping": {
                "scopes": "GOOGLE_SCOPES",
                "redirect_uri": "GOOGLE_REDIRECT_URI",
                "token_storage_mode": "GOOGLE_TOKEN_STORAGE_MODE",
            },
        }
    })

    @property
    def postgres_dsn(self) -> str:
        return (
            "postgresql+asyncpg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        auth_part = ""
        if self.redis_password:
            auth_part = f":{self.redis_password}@"
        return f"redis://{auth_part}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_host)


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()
