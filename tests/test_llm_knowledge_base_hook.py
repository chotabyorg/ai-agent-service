from types import SimpleNamespace
from uuid import uuid4

import pytest

import services.llm_service as llm_service_module
from services.knowledge_base_client import KnowledgeBaseContext
from services.llm_service import LLMService


class HookFakeKnowledgeBaseClient:
    contexts: list[KnowledgeBaseContext] = []
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def retrieve_context(self, *, owner_id, agent_id, query):
        self.__class__.calls.append(
            {
                "owner_id": owner_id,
                "agent_id": agent_id,
                "query": query,
                "kwargs": self.kwargs,
            }
        )
        return SimpleNamespace(contexts=self.__class__.contexts, total=len(self.__class__.contexts))

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_llm_knowledge_base_hook_uses_agent_owner_and_formats_context(monkeypatch) -> None:
    agent = SimpleNamespace(id=uuid4(), user_id=uuid4())
    HookFakeKnowledgeBaseClient.contexts = [
        KnowledgeBaseContext(
            chunk_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            content="Company warranty is 12 months.",
            score=0.88,
            source_filename="faq.pdf",
            chunk_index=2,
            metadata={"raw_extra": "must-not-leak"},
        )
    ]
    HookFakeKnowledgeBaseClient.calls = []
    monkeypatch.setattr(llm_service_module, "KnowledgeBaseClient", HookFakeKnowledgeBaseClient)

    service = LLMService()
    monkeypatch.setattr(service.settings, "knowledge_base_enabled", True)
    monkeypatch.setattr(service.settings, "knowledge_base_service_url", "http://knowledge-base-service:8000")
    monkeypatch.setattr(service.settings, "knowledge_base_timeout_seconds", 2.0)
    monkeypatch.setattr(service.settings, "knowledge_base_max_chars_per_chunk", 1200)
    monkeypatch.setattr(service.settings, "knowledge_base_max_total_context_chars", 5000)
    monkeypatch.setattr(service.settings, "internal_api_token", "service-token")

    message = await service._build_knowledge_base_message(agent=agent, query="Warranty?")

    assert HookFakeKnowledgeBaseClient.calls == [
        {
            "owner_id": str(agent.user_id),
            "agent_id": agent.id,
            "query": "Warranty?",
            "kwargs": {
                "base_url": "http://knowledge-base-service:8000",
                "service_token": "service-token",
                "timeout": 2.0,
            },
        }
    ]
    assert message is not None
    assert '[1] source="faq.pdf" chunk_index=2 score=0.88' in message
    assert "Company warranty is 12 months." in message
    assert "must-not-leak" not in message


@pytest.mark.asyncio
async def test_llm_knowledge_base_hook_fail_opens_on_unexpected_formatter_error(monkeypatch) -> None:
    agent = SimpleNamespace(id=uuid4(), user_id=uuid4())
    HookFakeKnowledgeBaseClient.contexts = [
        KnowledgeBaseContext(
            chunk_id=uuid4(),
            document_id=uuid4(),
            knowledge_base_id=uuid4(),
            content="Context",
            score=0.5,
            source_filename="faq.pdf",
            chunk_index=0,
            metadata={},
        )
    ]
    monkeypatch.setattr(llm_service_module, "KnowledgeBaseClient", HookFakeKnowledgeBaseClient)

    def broken_formatter(*args, **kwargs):
        raise RuntimeError("formatter failed")

    monkeypatch.setattr(llm_service_module, "build_knowledge_base_system_message", broken_formatter)

    service = LLMService()
    monkeypatch.setattr(service.settings, "knowledge_base_enabled", True)
    monkeypatch.setattr(service.settings, "knowledge_base_service_url", "http://knowledge-base-service:8000")
    monkeypatch.setattr(service.settings, "knowledge_base_timeout_seconds", 2.0)
    monkeypatch.setattr(service.settings, "knowledge_base_max_chars_per_chunk", 1200)
    monkeypatch.setattr(service.settings, "knowledge_base_max_total_context_chars", 5000)
    monkeypatch.setattr(service.settings, "internal_api_token", "service-token")

    assert await service._build_knowledge_base_message(agent=agent, query="Question") is None
