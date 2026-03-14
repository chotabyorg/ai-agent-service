from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

import main
from api.dependencies import get_db
from api.v1 import agents as agents_api
from api.v1 import chat as chat_api


@dataclass
class FakeAgent:
    id: UUID
    user_id: UUID
    name: str
    system_prompt: str
    llm_provider: str
    model_name: str
    temperature: float
    is_active: bool


@dataclass
class FakeConversationSession:
    id: UUID
    agent_id: UUID
    external_client_id: str
    channel: str


class InMemoryAgentService:
    def __init__(self) -> None:
        self.agents: dict[UUID, FakeAgent] = {}

    async def create_agent(self, session: object, payload: object) -> FakeAgent:
        agent = FakeAgent(id=uuid4(), **payload.model_dump())
        self.agents[agent.id] = agent
        return agent

    async def get_agent_by_id(self, session: object, agent_id: UUID) -> FakeAgent | None:
        return self.agents.get(agent_id)

    async def get_credentials(self, session: object, agent_id: UUID) -> list[object]:
        return []


class InMemorySessionService:
    def __init__(self) -> None:
        self.sessions: dict[tuple[UUID, str, str], FakeConversationSession] = {}

    async def get_or_create_session(
        self,
        session: object,
        agent_id: UUID,
        external_client_id: str,
        channel: str,
    ) -> FakeConversationSession:
        key = (agent_id, external_client_id, channel)
        if key not in self.sessions:
            self.sessions[key] = FakeConversationSession(
                id=uuid4(),
                agent_id=agent_id,
                external_client_id=external_client_id,
                channel=channel,
            )
        return self.sessions[key]


class InMemoryHistoryService:
    def __init__(self) -> None:
        self.history_by_session: dict[UUID, list[dict[str, str]]] = {}

    async def load_history(self, session_id: UUID) -> list[dict[str, str]]:
        return list(self.history_by_session.get(session_id, []))

    async def save_history(self, session_id: UUID, history: list[dict[str, str]]) -> None:
        self.history_by_session[session_id] = [dict(item) for item in history]


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        messages = kwargs["messages"]
        user_messages = [message for message in messages if message["role"] == "user"]
        last_user_message = user_messages[-1]["content"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f"Echo: {last_user_message}",
                        tool_calls=None,
                    )
                )
            ]
        )


class FakeLLMClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


def test_chat_completion_auto_creates_and_reuses_session(monkeypatch) -> None:
    fake_agent_service = InMemoryAgentService()
    fake_session_service = InMemorySessionService()
    fake_history_service = InMemoryHistoryService()
    fake_llm_client = FakeLLMClient()

    async def noop() -> None:
        return None

    monkeypatch.setattr(main, "init_redis", noop)
    monkeypatch.setattr(main, "close_redis", noop)

    dummy_db_session = object()

    async def override_get_db():
        yield dummy_db_session

    main.app.dependency_overrides[get_db] = override_get_db

    original_agent_service = agents_api.agent_service
    original_llm_agent_service = chat_api.llm_service.agent_service
    original_llm_session_service = chat_api.llm_service.session_service
    original_llm_history_service = chat_api.llm_service.history_service
    original_llm_client = chat_api.llm_service.llm

    agents_api.agent_service = fake_agent_service
    chat_api.llm_service.agent_service = fake_agent_service
    chat_api.llm_service.session_service = fake_session_service
    chat_api.llm_service.history_service = fake_history_service
    chat_api.llm_service.llm = fake_llm_client

    try:
        with TestClient(main.app) as client:
            agent_response = client.post(
                "/v1/agents/",
                json={
                    "user_id": str(uuid4()),
                    "name": "Support agent",
                    "system_prompt": "Be helpful.",
                    "llm_provider": "openrouter",
                    "model_name": "fake-model",
                    "temperature": 0.3,
                    "is_active": True,
                },
            )

            assert agent_response.status_code == 201
            agent_id = UUID(agent_response.json()["id"])

            first_response = client.post(
                "/v1/chat/completions",
                json={
                    "agent_id": str(agent_id),
                    "external_client_id": "telegram:42",
                    "channel": "telegram",
                    "message": "Hello there",
                },
            )

            assert first_response.status_code == 200
            assert len(fake_session_service.sessions) == 1

            created_session = next(iter(fake_session_service.sessions.values()))
            assert first_response.json() == {
                "session_id": str(created_session.id),
                "answer": "Echo: Hello there",
            }
            assert created_session.agent_id == agent_id
            assert fake_history_service.history_by_session[created_session.id] == [
                {"role": "user", "content": "Hello there"},
                {"role": "assistant", "content": "Echo: Hello there"},
            ]

            second_response = client.post(
                "/v1/chat/completions",
                json={
                    "agent_id": str(agent_id),
                    "external_client_id": "telegram:42",
                    "channel": "telegram",
                    "message": "How are you?",
                },
            )

            assert second_response.status_code == 200
            assert second_response.json() == {
                "session_id": str(created_session.id),
                "answer": "Echo: How are you?",
            }
            assert len(fake_session_service.sessions) == 1
            assert set(fake_history_service.history_by_session) == {created_session.id}
            assert fake_history_service.history_by_session[created_session.id] == [
                {"role": "user", "content": "Hello there"},
                {"role": "assistant", "content": "Echo: Hello there"},
                {"role": "user", "content": "How are you?"},
                {"role": "assistant", "content": "Echo: How are you?"},
            ]

            second_call_messages = fake_llm_client.chat.completions.calls[1]["messages"]
            assert second_call_messages[1:] == [
                {"role": "user", "content": "Hello there"},
                {"role": "assistant", "content": "Echo: Hello there"},
                {"role": "user", "content": "How are you?"},
            ]
    finally:
        agents_api.agent_service = original_agent_service
        chat_api.llm_service.agent_service = original_llm_agent_service
        chat_api.llm_service.session_service = original_llm_session_service
        chat_api.llm_service.history_service = original_llm_history_service
        chat_api.llm_service.llm = original_llm_client
        main.app.dependency_overrides.clear()
