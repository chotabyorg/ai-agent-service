"""Streaming turn endpoint: sentence events, then the authoritative final."""

import json

import pytest

from core.managed_boundary import MANAGED_PATH
from tests.test_managed_turns import TOKEN, execute, model, payload, setup


def sse_stream(texts, finish="stop"):
    from types import SimpleNamespace

    async def generate():
        for text in texts:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=text, tool_calls=None),
                        finish_reason=None,
                    )
                ]
            )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                    finish_reason=finish,
                )
            ]
        )

    return generate()


def sse_events(body):
    events = []
    for raw in body.decode("utf-8").split("\n\n"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):]))
    return events


@pytest.mark.integration
async def test_streaming_turn_emits_sentences_then_authoritative_final(
    api_client, db_session, fake_redis, monkeypatch, model
):
    agent, value = await setup(api_client, db_session, monkeypatch)
    value.update(contract_version=2, stream_partials=True, max_partial_chars=500)
    model.chat.completions.queue(
        sse_stream(["Здравствуйте! ", "Чем могу помочь?"])
    )
    async with api_client.stream(
        "POST", MANAGED_PATH, json=value,
        headers={"X-Service-Token": TOKEN},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = await response.aread()
    events = sse_events(body)
    partials = [event for event in events if not event.get("partial_final")]
    finals = [event for event in events if event.get("partial_final")]
    assert [event["partial_text"] for event in partials] == [
        "Здравствуйте! ",
        "Чем могу помочь?",
    ]
    assert len(finals) == 1
    final = finals[-1]
    assert final["status"] == "DRAFT_READY"
    assert final["draft_text"] == "Здравствуйте! Чем могу помочь?"
    assert set(model.chat.completions.calls[0]) >= {"model", "messages", "stream"}


@pytest.mark.integration
async def test_buffered_turn_stays_single_json_without_opt_in(
    api_client, db_session, fake_redis, monkeypatch, model
):
    from tests.fakes import build_llm_response

    agent, value = await setup(api_client, db_session, monkeypatch)
    model.chat.completions.queue(build_llm_response(content="Тихий ответ."))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY"
    assert result["draft_text"] == "Тихий ответ."
    assert result["partial_final"] is False
    assert "stream" not in model.chat.completions.calls[0]
