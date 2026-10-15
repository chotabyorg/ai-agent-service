"""Application configuration for ai-agent-service."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.llm_models import get_default_model_id
from core.voice_latency import VoiceLatencyProfile


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
    llm_default_model: str = Field(default_factory=get_default_model_id)
    llm_default_temperature: float = Field(default=0.7)
    llm_max_tokens: int = Field(default=1024)
    llm_http_referer: str | None = Field(default=None)
    llm_app_title: str | None = Field(default=None)

    privacy_filter_enabled: bool = Field(default=False)
    privacy_filter_base_url: str = Field(default="http://privacy-filter-service:8000")
    privacy_filter_timeout_seconds: float = Field(default=30.0)

    encryption_key: SecretStr = Field(default="")
    mcp_credentials_encryption_enabled: bool = Field(default=False)

    internal_api_token: str = Field(default="")

    business_capabilities_enabled: bool = False
    business_crm_url: str = 'http://chota-crm-service:8000'
    business_crm_private_key: SecretStr = Field(default='', repr=False)
    business_crm_key_id: str = ''
    business_crm_timeout_seconds: float = Field(default=5.0, ge=1, le=30)
    business_config_origins: list[str] = Field(default_factory=lambda: ['https://chota.by', 'https://agents.chota.by'])

    # Dedicated credential for callers of /v1/chat/completions. It is
    # intentionally independent from INTERNAL_API_TOKEN, which protects the
    # existing management endpoints.
    chat_s2s_token: SecretStr = Field(default="")
    chat_s2s_auth_required: bool = Field(default=False)

    # Optional, dedicated permission boundary for managed Business effects.
    # An absent pair is read-only; partial or reused credentials fail startup.
    business_conversation_url: str = ""
    business_conversation_token: SecretStr = Field(default="", repr=False)
    business_conversation_timeout_seconds: float = Field(default=3.0, ge=0.2, le=10.0,
                                                          allow_inf_nan=False)

    # Handoff remains dark unless an exact Telegram scope is explicitly listed.
    # The lifecycle credential is a dedicated Conversation-only principal.
    handoff_ai_enrolled_scopes: SecretStr = Field(default="", repr=False)
    handoff_conversation_url: str = ""
    handoff_lifecycle_token: SecretStr = Field(default="", repr=False)
    handoff_lifecycle_timeout_seconds: float = Field(default=3.0, ge=0.2, le=10.0,
                                                       allow_inf_nan=False)

    # Optional and fail-closed. No production registration is created by this code.
    managed_ai_auth_principals: SecretStr = Field(default="", repr=False)
    managed_ai_execution_seconds: float = Field(default=90.0, ge=1, le=240, allow_inf_nan=False)
    managed_ai_context_bytes: int = Field(default=131072, ge=1024, le=1048576)
    # Explicit model IDs only; no default override, pattern matching or routing change.
    managed_openrouter_disable_reasoning_models: list[str] = Field(default_factory=list, max_length=16)
    # Empty by default; only authenticated managed VOICE requests can match.
    managed_voice_latency_profiles: list[VoiceLatencyProfile] = Field(default_factory=list, max_length=64)
    _managed_principals: tuple = PrivateAttr(default=())
    _handoff_scopes: tuple = PrivateAttr(default=())

    @model_validator(mode="after")
    def validate_managed_reasoning_transport(self) -> "Settings":
        if self.managed_openrouter_disable_reasoning_models or self.managed_voice_latency_profiles:
            if (self.privacy_filter_enabled
                    or self.llm_base_url.rstrip('/') != 'https://openrouter.ai/api/v1'):
                # Do not silently drop an unsupported proxy/provider parameter or
                # render settings (which contain credentials) in validation errors.
                raise RuntimeError("Managed reasoning control requires direct OpenRouter transport")
        return self

    @model_validator(mode="after")
    def validate_managed_registry(self) -> "Settings":
        from core.managed_security import parse_principals

        try:
            self._managed_principals = parse_principals(
                self.managed_ai_auth_principals.get_secret_value(),
                (self.chat_s2s_token.get_secret_value(), self.internal_api_token),
            )
        except ValueError:
            # RuntimeError avoids Pydantic's startup rendering of rejected env input.
            raise RuntimeError("Invalid managed AI authentication configuration") from None
        return self

    @model_validator(mode="after")
    def validate_business_conversation_authority(self) -> "Settings":
        from core.managed_security import valid_token

        url = self.business_conversation_url
        token = self.business_conversation_token.get_secret_value()
        if bool(url) != bool(token):
            raise RuntimeError("Invalid Business Conversation authority configuration")
        if not url:
            return self
        try:
            parsed = urlsplit(url)
            _ = parsed.port  # Reject malformed and out-of-range explicit ports.
            valid_url = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                         and parsed.username is None and parsed.password is None
                         and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                         and "?" not in url and "#" not in url)
        except ValueError:
            valid_url = False
        other_tokens = {self.internal_api_token, self.chat_s2s_token.get_secret_value()}
        other_tokens.update(principal.token.get_secret_value() for principal in self.managed_principals)
        if (not valid_url or url != url.strip() or not valid_token(token)
                or token in other_tokens):
            raise RuntimeError("Invalid Business Conversation authority configuration")
        return self

    @model_validator(mode="after")
    def validate_handoff_lifecycle(self) -> "Settings":
        from core.managed_security import parse_handoff_scopes, valid_token

        raw_scopes = self.handoff_ai_enrolled_scopes.get_secret_value()
        token = self.handoff_lifecycle_token.get_secret_value().strip()
        url = self.handoff_conversation_url
        try:
            scopes = parse_handoff_scopes(raw_scopes, self.managed_principals)
            parsed = urlsplit(url) if url else None
            if parsed is not None:
                _ = parsed.port
                valid_url = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                             and parsed.username is None and parsed.password is None
                             and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                             and "?" not in url and "#" not in url)
            else:
                valid_url = False
            other_tokens = {self.internal_api_token, self.chat_s2s_token.get_secret_value(),
                            self.business_conversation_token.get_secret_value()}
            other_tokens.update(principal.token.get_secret_value() for principal in self.managed_principals)
            if (bool(url) != bool(token) or (scopes and not (url and token))
                    or (url and (not valid_url or url != url.strip()))
                    or (token and (not valid_token(token) or token in other_tokens))):
                raise ValueError
            self._handoff_scopes = scopes
        except Exception:
            raise RuntimeError("Invalid Handoff lifecycle configuration") from None
        return self

    @property
    def managed_principals(self) -> tuple:
        return self._managed_principals

    @property
    def handoff_scopes(self) -> tuple:
        return self._handoff_scopes

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
