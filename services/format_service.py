import re
import markdown


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
        """
        Telegram поддерживает ограниченный HTML.
        Необходимо экранировать <, > и & вне тегов.
        """
        # 1. Защита блоков кода перед форматированием
        code_blocks = []

        def code_repl(match):
            code_blocks.append(match.group(1))
            return f"@@CODE_BLOCK_{len(code_blocks) - 1}@@"

        # Убрали (?:\w*\n)? чтобы сохранить название языка (например, python\n) внутри <pre>
        text = re.sub(r'```(.*?)```', code_repl, text, flags=re.DOTALL)

        inline_codes = []

        def inline_repl(match):
            inline_codes.append(match.group(1))
            return f"@@INLINE_CODE_{len(inline_codes) - 1}@@"

        text = re.sub(r'`([^`]+)`', inline_repl, text)

        # 2. Экранирование спецсимволов для безопасного HTML
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # 3. Конвертация Markdown в Telegram HTML
        text = re.sub(r'\*\*(?!\s)(.+?)(?<!\s)\*\*', r'<b>\1</b>', text)  # Bold
        text = re.sub(r'__(?!\s)(.+?)(?<!\s)__', r'<b>\1</b>', text)  # Bold
        text = re.sub(r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)', r'<i>\1</i>', text)  # Italic
        text = re.sub(r'~~(?!\s)(.+?)(?<!\s)~~', r'<s>\1</s>', text)  # Strikethrough

        # Ссылки
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

        # 4. Восстановление блоков кода
        for i, code in enumerate(code_blocks):
            code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = text.replace(f"@@CODE_BLOCK_{i}@@", f"<pre>{code}</pre>")

        for i, code in enumerate(inline_codes):
            code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = text.replace(f"@@INLINE_CODE_{i}@@", f"<code>{code}</code>")

        return text.strip()

    @classmethod
    def _to_instagram_text(cls, text: str) -> str:
        """Instagram не поддерживает форматирование. Отдаем чистый текст с отступами."""
        # Удаляем обертки кода, оставляя сам текст (здесь язык отбрасываем, чтобы он не висел в тексте)
        text = re.sub(r'```(?:\w*\n)?(.*?)```', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'`([^`]+)`', r'\1', text)

        # Удаляем Markdown форматирование
        text = re.sub(r'\*\*(?!\s)(.+?)(?<!\s)\*\*', r'\1', text)
        text = re.sub(r'__(?!\s)(.+?)(?<!\s)__', r'\1', text)
        text = re.sub(r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)', r'\1', text)
        text = re.sub(r'~~(?!\s)(.+?)(?<!\s)~~', r'\1', text)

        # Преобразуем ссылки из [Текст](https...) в "Текст (https...)"
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)

        # Убираем символы заголовков
        text = re.sub(r'^#{1,6}\s+(.*)$', r'\1', text, flags=re.MULTILINE)

        # Нормализация абзацев
        paragraphs = re.split(r'\n+', text)
        formatted_paragraphs = []
        for p in paragraphs:
            p = p.strip()
            if p:
                formatted_paragraphs.append(f'\u2003{p}')

        return '\n'.join(formatted_paragraphs)

    @classmethod
    def _to_website_html(cls, text: str) -> str:
        """Полноценный HTML рендер для веб-виджета."""
        html = markdown.markdown(text, extensions=['extra', 'nl2br', 'sane_lists'])
        html = html.replace('<p>', '<p style="text-indent: 1.5em; margin-bottom: 1em;">')
        return html