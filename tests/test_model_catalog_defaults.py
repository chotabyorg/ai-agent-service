"""Catalog, unchanged API selection contract, and runtime default precedence."""
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from config.llm_models import AVAILABLE_MODELS, get_default_model_id
from core.config import Settings
from models.agent import AgentTemplate
from services.llm_service import LLMService
from services.prompt_builder import ChatContext
from tests.fakes import ScriptedLLMClient, build_llm_response


EXPECTED = [
    {"display_name": "GPT-6 Luna", "model_id": "openai/gpt-6-luna"},
    {"display_name": "Kimi K3", "model_id": "moonshotai/kimi-k3"},
    {"display_name": "Qwen3.8 27B", "model_id": "qwen/qwen3.8-27b"},
    {"display_name": "Muse Glimmer 30B", "model_id": "meta/muse-glimmer-30b"},
]


def test_exact_catalog_order_and_derived_default():
    assert AVAILABLE_MODELS == EXPECTED
    assert get_default_model_id() == EXPECTED[0]["model_id"]


def test_runtime_default_and_environment_override(monkeypatch):
    monkeypatch.delenv("LLM_DEFAULT_MODEL", raising=False)
    assert Settings(_env_file=None).llm_default_model == "openai/gpt-6-luna"
    # Overrides remain explicit, including old environment values that must be
    # changed in GitOps. Do not pretend the code default can override an env var.
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "google/gemini-2.5-flash")
    assert Settings(_env_file=None).llm_default_model == "google/gemini-2.5-flash"
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "moonshotai/kimi-k3")
    assert Settings(_env_file=None).llm_default_model == "moonshotai/kimi-k3"


@pytest.mark.integration
async def test_first_discovered_model_creates_luna_and_edit_preserves_choice(api_client, db_session):
    catalog = await api_client.get("/v1/models")
    assert catalog.status_code == 200
    assert catalog.json() == EXPECTED
    owner = uuid4()
    headers = {"X-User-Id": str(owner)}
    payload = {"name": "Synthetic catalog default", "system_prompt": "Synthetic prompt",
               "model_name": catalog.json()[0]["model_id"]}
    response = await api_client.post("/v1/agents/", headers=headers, json=payload)
    assert response.status_code == 201
    agent_id = UUID(response.json()["id"])
    assert response.json()["model_name"] == "openai/gpt-6-luna"
    async with db_session() as session:
        stored = await session.get(AgentTemplate, agent_id)
        assert stored.model_name == "openai/gpt-6-luna"
    selected = EXPECTED[1]["model_id"]
    edited = await api_client.put(f"/v1/agents/{agent_id}", headers=headers, json={"model_name": selected})
    assert edited.status_code == 200 and edited.json()["model_name"] == selected
    preserved = await api_client.put(f"/v1/agents/{agent_id}", headers=headers, json={"name": "Renamed"})
    assert preserved.json()["model_name"] == selected
    # Required-field API schema and arbitrary explicit model choices are unchanged.
    payload.pop("model_name")
    assert (await api_client.post("/v1/agents/", headers=headers, json=payload)).status_code == 422
    payload["model_name"] = "synthetic/explicit-custom-model"
    explicit = await api_client.post("/v1/agents/", headers=headers, json=payload)
    assert explicit.status_code == 201
    assert explicit.json()["model_name"] == payload["model_name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,override,expected", [
    ("", None, "openai/gpt-6-luna"),
    (None, None, "openai/gpt-6-luna"),
    ("", "moonshotai/kimi-k3", "moonshotai/kimi-k3"),
    ("openai/gpt-6-luna", "google/gemini-2.5-flash", "openai/gpt-6-luna"),
    ("moonshotai/kimi-k3", None, "moonshotai/kimi-k3"),
])
async def test_actual_completion_and_managed_envelope_model_precedence(monkeypatch, stored, override, expected):
    monkeypatch.delenv("LLM_DEFAULT_MODEL", raising=False)
    service = LLMService()
    service.settings = Settings(_env_file=None, **({"llm_default_model": override} if override else {}))
    agent = SimpleNamespace(id=uuid4(), user_id=uuid4(), model_name=stored, temperature=0.4,
                            prompt_config=None, system_prompt="Synthetic prompt", timezone="Europe/Minsk")
    client = ScriptedLLMClient()
    client.chat.completions.queue(build_llm_response(content="Synthetic answer"))
    answer = await service._execute_core(
        None, agent=agent, llm_client=client, credentials_records=[],
        communication=[{"role": "user", "content": "Hello"}],
        chat_context=ChatContext(channel="website", external_client_id="synthetic"), query="Hello")
    assert answer == "Synthetic answer"
    assert client.chat.completions.calls[0]["model"] == expected
    assert service._managed_envelope(agent, [], [])["model"] == expected
