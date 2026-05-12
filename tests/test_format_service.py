import pytest
from services.format_service import FormatService

def test_telegram_unclosed_bold():
    # Незакрытый тег не должен ломать форматирование
    text = "Тут **жирный текст, а тут нет."
    res = FormatService.format_message(text, "telegram")
    assert "<b>" not in res
    assert "**жирный" in res

def test_telegram_nested_lists():
    text = "* Пункт 1\n  * Подпункт 1\n* Пункт 2"
    res = FormatService.format_message(text, "telegram")
    # Звездочки списков не должны превратиться в курсив (<i>)
    assert "<i>" not in res
    assert "* Пункт 1" in res

def test_telegram_html_escape():
    text = "Сравни: 5 < 10 & 2 > 1"
    res = FormatService.format_message(text, "telegram")
    assert "5 &lt; 10 &amp; 2 &gt; 1" in res

def test_telegram_code_blocks():
    text = "Вот код:\n```python\nprint('hello <world>')\n```"
    res = FormatService.format_message(text, "telegram")
    assert "<pre>python\nprint('hello &lt;world&gt;')\n</pre>" in res

def test_instagram_strip_markdown():
    text = "## Привет!\nЭто **важно** и *очень* нужно."
    res = FormatService.format_message(text, "instagram")
    assert "\u2003Привет!" in res
    assert "\u2003Это важно и очень нужно." in res
    assert "**" not in res
    assert "##" not in res

def test_website_html_generation():
    text = "Абзац 1\n\nАбзац 2"
    res = FormatService.format_message(text, "website_widget")
    assert '<p style="text-indent: 1.5em;' in res
    assert 'Абзац 1</p>' in res