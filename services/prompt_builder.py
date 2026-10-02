"""Render owner-controlled business instructions, with a compatible chat facade."""

from dataclasses import dataclass


@dataclass
class ChatContext:
    """Runtime context known only at the moment of the LLM request."""

    channel: str
    external_client_id: str


class PromptBuilder:
    """Render business configuration; delegate request-time policy to PromptComposer."""

    PLATFORM_RULES = {
        "telegram": (
            "- Поддерживается Markdown: *жирный*, _курсив_\n"
            "- Максимум 4096 символов на сообщение\n"
            "- Обрабатывай команды /start, /help"
        ),
        "instagram": (
            "- Только plain text, без форматирования\n"
            "- Максимум 1000 символов на сообщение\n"
            "- Сообщения должны быть короче чем в Telegram"
        ),
        "website": "- Отвечай короткими сообщениями, удобными для виджета сайта",
    }

    def build(self, agent: object, context: ChatContext) -> str:
        """Compatibility entrypoint for chat and preview; platform policy always applies."""
        from services.prompt_composer import PromptComposer

        return PromptComposer(business_builder=self).compose(agent, context)

    def build_static(self, agent: object) -> str:
        """Render business text only: no frozen clock, channel, permissions or policy.

        Existing stored raw prompts are returned verbatim. Structured prompt saves
        may cache this business rendering, but chat always renders from config.
        """
        config = getattr(agent, "prompt_config", None)
        if config is None:
            return getattr(agent, "system_prompt", None) or ""
        return self._build_from_config(config, agent)

    def _build_from_config(self, config: dict, agent: object) -> str:
        company = config.get("company", {})
        personality = config.get("personality", {})

        sections: list[str] = []

        sections.append(self._build_identity(
            agent_name=getattr(agent, "name", ""),
            company_name=company.get("name", ""),
            company_description=company.get("description", ""),
        ))

        # Historical "capabilities" means human responsibilities, not executable
        # capability grants. An explicitly empty new field takes precedence too.
        responsibilities = config.get("responsibilities", config.get("capabilities", []))
        if responsibilities:
            sections.append(self._build_responsibilities(responsibilities))

        sections.append(self._build_personality(personality))

        fields = config.get("data_collection_fields", [])
        if fields:
            sections.append(self._build_data_collection(fields))

        algorithms = config.get("algorithms", [])
        if algorithms:
            sections.append(self._build_algorithms(algorithms))

        sections.append(self._build_guardrails(
            restricted_claims=config.get("restricted_claims", []),
            fallback_policy=config.get("fallback_policy", ""),
        ))

        sections.append(self._build_output_format(personality))

        greeting = config.get("greeting_template", "")
        if greeting:
            greeting_rendered = greeting.replace("{{company_name}}", company.get("name", ""))
            sections.append(
                f"<greeting>\nПри первом сообщении от пользователя (/start, \"привет\"):\n"
                f"{greeting_rendered}\n</greeting>"
            )

        return "\n\n".join(sections)

    @staticmethod
    def _build_identity(
        agent_name: str,
        company_name: str,
        company_description: str,
    ) -> str:
        lines = [
            "<identity>",
            f"Ты — {agent_name}, AI-ассистент компании {company_name}.",
        ]
        if company_description:
            lines.append(company_description)
        lines.append("</identity>")
        return "\n".join(lines)

    @staticmethod
    def _build_responsibilities(responsibilities: list[str]) -> str:
        items = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(responsibilities))
        return (
            f"<business_responsibilities>\n"
            f"Ты специализируешься на следующих задачах:\n"
            f"{items}\n\n"
            f"Ты НЕ являешься универсальным ассистентом. Запросы за пределами этой зоны ты вежливо отклоняешь.\n"
            f"</business_responsibilities>"
        )

    @staticmethod
    def _build_personality(personality: dict) -> str:
        tone_map = {
            "friendly": "Дружелюбный и профессиональный — как опытный сотрудник поддержки",
            "neutral": "Нейтральный и деловой — чёткие ответы без лишних эмоций",
            "formal": "Формальный и уважительный — как представитель крупной компании",
            "bold": "Энергичный и прямой — уверенный стиль общения",
        }
        tone = personality.get("tone", "friendly")
        tone_desc = tone_map.get(tone, tone_map["friendly"])

        use_emoji = personality.get("use_emoji", True)
        emoji_rule = "Используй эмодзи умеренно — 1-2 на сообщение" if use_emoji else "Не используй эмодзи"

        return (
            f"<personality>\n"
            f"Твой стиль общения:\n"
            f"- {tone_desc}\n"
            f"- Лаконичный — короткие сообщения по 1-3 предложения\n"
            f"- Естественный — разговорный, но грамотный язык\n"
            f"- Проактивный — предлагай следующий шаг\n"
            f"- {emoji_rule}\n\n"
            f"Чего ты НЕ делаешь:\n"
            f"- Не начинай сообщения со слов «Конечно!», «Разумеется!» — звучит шаблонно\n"
            f"- Не извиняйся за каждую мелочь\n"
            f"- Не используй канцелярский стиль\n"
            f"- Не пиши длинные абзацы\n"
            f"</personality>"
        )

    @staticmethod
    def _build_data_collection(fields: list[dict]) -> str:
        items = "\n".join(
            f"- {f['name']}{' (обязательно)' if f.get('required') else ' (опционально)'}"
            for f in fields
        )
        return (
            f"<data_collection>\n"
            f"При общении с клиентом собирай следующую информацию:\n"
            f"{items}\n\n"
            f"Собирай данные естественно в ходе диалога, не задавай все вопросы сразу.\n"
            f"</data_collection>"
        )

    @staticmethod
    def _build_algorithms(algorithms: list[dict]) -> str:
        blocks = []
        for alg in algorithms:
            steps = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(alg.get("steps", [])))
            blocks.append(
                f'<algorithm name="{alg["name"]}">\n'
                f'<trigger>{alg.get("trigger", "")}</trigger>\n'
                f'<steps>\n{steps}\n</steps>\n'
                f'</algorithm>'
            )
        return "<algorithms>\n" + "\n\n".join(blocks) + "\n</algorithms>"

    @staticmethod
    def _build_guardrails(restricted_claims: list[str], fallback_policy: str) -> str:
        lines = [
            "<guardrails>",
            "Ограничения бизнеса:",
        ]
        if restricted_claims:
            lines.append("")
            lines.append("Дополнительные ограничения:")
            for claim in restricted_claims:
                lines.append(f"- {claim}")
        if fallback_policy:
            lines.append("")
            lines.append(f"Политика эскалации: {fallback_policy}")
        lines.append("</guardrails>")
        return "\n".join(lines)

    @staticmethod
    def _build_platform_rules(channel: str) -> str:
        rules = PromptBuilder.PLATFORM_RULES.get(channel, "- Используй короткий plain text")
        return f"<platform_rules>\nПравила для {channel}:\n{rules}\n</platform_rules>"

    @staticmethod
    def _build_output_format(personality: dict) -> str:
        languages = personality.get("languages", ["ru"])
        lang_str = ", ".join(languages)
        return (
            "<output_format>\n"
            "Правила ответов:\n"
            "- Пиши короткими сообщениями по 1-3 предложения\n"
            "- Каждое сообщение содержит: ответ ИЛИ следующий шаг ИЛИ вопрос\n"
            "- Не оставляй пользователя «в воздухе»\n"
            f"- Поддерживаемые языки: {lang_str}\n"
            "- Отвечай на языке пользователя\n"
            "</output_format>"
        )
