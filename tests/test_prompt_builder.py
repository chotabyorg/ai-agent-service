"""Tests for PromptBuilder — XML-structured prompt assembly."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytz
import pytest

from services.prompt_builder import ChatContext, PromptBuilder


def _make_agent(
    name: str = "TestAgent",
    system_prompt: str = "legacy prompt",
    prompt_config: dict | None = None,
    timezone: str = "Europe/Minsk",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        system_prompt=system_prompt,
        prompt_config=prompt_config,
        timezone=timezone,
    )


def _minimal_config() -> dict:
    return {
        "version": 1,
        "company": {"name": "Acme", "description": "We sell stuff"},
        "personality": {"tone": "friendly", "languages": ["ru"], "use_emoji": True},
    }


TELEGRAM_CTX = ChatContext(channel="telegram", external_client_id="tg:1")
INSTAGRAM_CTX = ChatContext(channel="instagram", external_client_id="ig:1")


class TestBuildFromConfigBasic:
    def test_build_from_config_basic(self) -> None:
        agent = _make_agent(prompt_config=_minimal_config())
        result = PromptBuilder().build(agent, TELEGRAM_CTX)

        assert "<identity>" in result
        assert "</identity>" in result
        assert "<personality>" in result
        assert "</personality>" in result
        assert "<tools_guidance>" in result
        assert "<error_handling>" in result
        assert "<guardrails>" in result
        assert "<platform_rules>" in result
        assert "<output_format>" in result


class TestBuildIncludesDatetime:
    def test_build_includes_datetime(self) -> None:
        agent = _make_agent(prompt_config=_minimal_config(), timezone="Europe/Moscow")
        fixed_dt = datetime(2026, 3, 24, 15, 30, tzinfo=pytz.timezone("Europe/Moscow").localize(datetime(2026, 3, 24, 15, 30)).tzinfo)

        with patch("services.prompt_builder.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = PromptBuilder().build(agent, TELEGRAM_CTX)

        assert "2026-03-24" in result
        assert "15:30" in result


class TestBuildIncludesChannel:
    def test_telegram_platform_rules(self) -> None:
        agent = _make_agent(prompt_config=_minimal_config())
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert "Правила для telegram" in result
        assert "4096 символов" in result

    def test_instagram_platform_rules(self) -> None:
        agent = _make_agent(prompt_config=_minimal_config())
        result = PromptBuilder().build(agent, INSTAGRAM_CTX)
        assert "Правила для instagram" in result
        assert "1000 символов" in result


class TestBuildIncludesCapabilities:
    def test_capabilities_in_output(self) -> None:
        config = _minimal_config()
        config["capabilities"] = ["Запись на приём", "Консультация по ценам"]
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)

        assert "<core_capabilities>" in result
        assert "Запись на приём" in result
        assert "Консультация по ценам" in result

    def test_no_capabilities_section_when_empty(self) -> None:
        agent = _make_agent(prompt_config=_minimal_config())
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert "<core_capabilities>" not in result


class TestBuildIncludesAlgorithms:
    def test_algorithms_in_output(self) -> None:
        config = _minimal_config()
        config["algorithms"] = [
            {
                "name": "booking",
                "trigger": "клиент хочет записаться",
                "steps": ["Спросить дату", "Спросить время", "Подтвердить"],
            }
        ]
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)

        assert "<algorithms>" in result
        assert '<algorithm name="booking">' in result
        assert "<trigger>клиент хочет записаться</trigger>" in result
        assert "Спросить дату" in result


class TestBuildPersonalityTones:
    @pytest.mark.parametrize(
        "tone,expected_fragment",
        [
            ("friendly", "Дружелюбный и профессиональный"),
            ("neutral", "Нейтральный и деловой"),
            ("formal", "Формальный и уважительный"),
            ("bold", "Энергичный и прямой"),
        ],
    )
    def test_tone_descriptions(self, tone: str, expected_fragment: str) -> None:
        config = _minimal_config()
        config["personality"]["tone"] = tone
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert expected_fragment in result

    def test_emoji_disabled(self) -> None:
        config = _minimal_config()
        config["personality"]["use_emoji"] = False
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert "Не используй эмодзи" in result


class TestBuildLegacyFallback:
    def test_returns_system_prompt_when_no_config(self) -> None:
        agent = _make_agent(system_prompt="You are a helpful bot.", prompt_config=None)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert result == "You are a helpful bot."

    def test_returns_empty_when_no_config_and_no_prompt(self) -> None:
        agent = _make_agent(system_prompt=None, prompt_config=None)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert result == ""


class TestBuildGreetingWithCompanyName:
    def test_company_name_substitution(self) -> None:
        config = _minimal_config()
        config["greeting_template"] = "Добро пожаловать в {{company_name}}!"
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)

        assert "<greeting>" in result
        assert "Добро пожаловать в Acme!" in result
        assert "{{company_name}}" not in result


class TestBuildGuardrailsWithRestrictions:
    def test_restricted_claims_in_output(self) -> None:
        config = _minimal_config()
        config["restricted_claims"] = ["Не обещай скидки", "Не называй цены конкурентов"]
        config["fallback_policy"] = "Передай менеджеру"
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build(agent, TELEGRAM_CTX)

        assert "<guardrails>" in result
        assert "Не обещай скидки" in result
        assert "Не называй цены конкурентов" in result
        assert "Передай менеджеру" in result

    def test_guardrails_without_restrictions(self) -> None:
        agent = _make_agent(prompt_config=_minimal_config())
        result = PromptBuilder().build(agent, TELEGRAM_CTX)
        assert "<guardrails>" in result
        assert "Дополнительные ограничения:" not in result


class TestBuildStaticForPreview:
    def test_build_static_works_without_context(self) -> None:
        config = _minimal_config()
        agent = _make_agent(prompt_config=config)
        result = PromptBuilder().build_static(agent)

        assert "<identity>" in result
        assert "Acme" in result
        assert "telegram" in result  # default dummy context uses telegram

    def test_build_static_legacy_fallback(self) -> None:
        agent = _make_agent(system_prompt="Legacy!", prompt_config=None)
        result = PromptBuilder().build_static(agent)
        assert result == "Legacy!"
