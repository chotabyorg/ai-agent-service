"""Redis-backed conversation history service."""

import json
import logging
from typing import Any
from uuid import UUID

from redis.exceptions import RedisError

from core.config import get_settings
from core.redis import get_redis_client


logger = logging.getLogger(__name__)


class HistoryService:
    """Load and persist chat history for a conversation session."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @staticmethod
    def _get_history_key(session_id: UUID) -> str:
        return f"chat_history:{session_id}"

    def _trim_history(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        return history[-self.settings.history_max_items :]

    @staticmethod
    def _normalize_history(history: Any) -> list[dict[str, str]]:
        if not isinstance(history, list):
            return []

        normalized_history: list[dict[str, str]] = []
        for item in history:
            if not isinstance(item, dict):
                continue

            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue

            normalized_history.append({"role": role, "content": content})

        return normalized_history

    async def load_history(self, session_id: UUID) -> list[dict[str, str]]:
        redis_client = get_redis_client()
        if redis_client is None:
            return []

        try:
            raw_history = await redis_client.get(self._get_history_key(session_id))
            if not raw_history:
                return []

            return self._normalize_history(json.loads(raw_history))
        except (RedisError, json.JSONDecodeError, TypeError):
            logger.exception("redis_history_load_failed", extra={"session_id": str(session_id)})
            return []

    async def save_history(self, session_id: UUID, history: list[dict[str, str]]) -> None:
        redis_client = get_redis_client()
        if redis_client is None:
            return

        trimmed_history = self._trim_history(self._normalize_history(history))

        try:
            await redis_client.set(
                self._get_history_key(session_id),
                json.dumps(trimmed_history, ensure_ascii=False),
                ex=self.settings.history_ttl_seconds,
            )
        except RedisError:
            logger.exception("redis_history_save_failed", extra={"session_id": str(session_id)})
