import re
import markdown
import nh3


class FormatService:
    """Сервис форматирования Markdown от LLM в нативный формат каналов."""

    @classmethod
    def format_message(cls, text: str, channel: str) -> str:
        if not text:
            return text

        if channel == "telegram":
            return cls._to_telegram_html(text)
        elif channel == "instagram":
            return cls._to_instagram_text(text)
        elif channel == "website_widget":
            return cls._to_website_html(text)

        return text

    @classmethod
    def _to_telegram_html(cls, text: str) -> str:
        # 1. Защита блоков кода перед форматированием
        code_blocks = []

        def code_repl(match):
            code_blocks.append(match.group(1))
            return f"@@CODE_BLOCK_{len(code_blocks) - 1}@@"

        text = re.sub(r'```(.*?)```', code_repl, text, flags=re.DOTALL)

        inline_codes = []

        def inline_repl(match):
            inline_codes.append(match.group(1))
            return f"@@INLINE_CODE_{len(inline_codes) - 1}@@"

        text = re.sub(r'`([^`]+)`', inline_repl, text)

        # 2. Экранирование сырого HTML (чтобы Telegram не упал от случайных тегов модели)
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # 3. Безопасные ссылки (экранируем кавычки внутри URL)
        def link_repl(match):
            link_text = match.group(1)
            # Заменяем кавычки на URL-кодированные, чтобы не сломать атрибут href
            url = match.group(2).replace('"', '%22')
            return f'<a href="{url}">{link_text}</a>'

        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link_repl, text)

        # 4. Конвертация Markdown в Telegram HTML
        text = re.sub(r'\*\*(?!\s)(.+?)(?<!\s)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'__(?!\s)(.+?)(?<!\s)__', r'<b>\1</b>', text)
        text = re.sub(r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)', r'<i>\1</i>', text)
        text = re.sub(r'~~(?!\s)(.+?)(?<!\s)~~', r'<s>\1</s>', text)

        # 5. Восстановление блоков кода
        for i, code in enumerate(code_blocks):
            code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = text.replace(f"@@CODE_BLOCK_{i}@@", f"<pre>{code}</pre>")

        for i, code in enumerate(inline_codes):
            code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = text.replace(f"@@INLINE_CODE_{i}@@", f"<code>{code}</code>")

        return text.strip()

    @classmethod
    def _to_instagram_text(cls, text: str) -> str:
        text = re.sub(r'```(?:\w*\n)?(.*?)```', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        text = re.sub(r'\*\*(?!\s)(.+?)(?<!\s)\*\*', r'\1', text)
        text = re.sub(r'__(?!\s)(.+?)(?<!\s)__', r'\1', text)
        text = re.sub(r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)', r'\1', text)
        text = re.sub(r'~~(?!\s)(.+?)(?<!\s)~~', r'\1', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
        text = re.sub(r'^#{1,6}\s+(.*)$', r'\1', text, flags=re.MULTILINE)

        paragraphs = re.split(r'\n+', text)
        formatted_paragraphs = []
        for p in paragraphs:
            p = p.strip()
            if p:
                formatted_paragraphs.append(f'\u2003{p}')

        return '\n'.join(formatted_paragraphs)

    @classmethod
    def _to_website_html(cls, text: str) -> str:
        # Рендерим Markdown
        html = markdown.markdown(text, extensions=['extra', 'nl2br', 'sane_lists'])

        # Очищаем от потенциального XSS (разрешаем только форматирование)
        allowed_tags = {"b", "i", "em", "strong", "a", "code", "pre", "p", "br",
                        "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                        "blockquote", "table", "thead", "tbody", "tr", "th", "td"}
        allowed_attrs = {"a": {"href", "title", "target"}, "p": {"style"}}

        html = nh3.clean(html, tags=allowed_tags, attributes=allowed_attrs)

        # Добавляем стилизацию красной строки после санитизации
        html = html.replace('<p>', '<p style="text-indent: 1.5em; margin-bottom: 1em;">')
        return html