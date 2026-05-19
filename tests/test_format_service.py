from services.format_service import FormatService


def test_telegram_raw_html_escaped():
    text = "Смотри код: <b>жирный</b> <script>alert(1)</script>"
    res = FormatService.format_message(text, "telegram")
    assert "&lt;b&gt;жирный&lt;/b&gt;" in res
    assert "&lt;script&gt;" in res


def test_telegram_links_with_quotes():
    text = 'Ссылка [нажми](https://example.com/?q="test")'
    res = FormatService.format_message(text, "telegram")
    # Кавычка внутри href должна быть заэнкожена
    assert '<a href="https://example.com/?q=%22test%22">нажми</a>' in res


def test_telegram_malformed_links():
    text = 'Сломанная [ссылка](https://example.com'
    res = FormatService.format_message(text, "telegram")
    # Регулярка не должна сработать, текст остается "как есть"
    assert '<a href' not in res
    assert 'Сломанная [ссылка](https://example.com' in res


def test_website_html_sanitized():
    text = "Текст <script>alert('xss')</script> **жирный**"
    res = FormatService.format_message(text, "website_widget")
    # XSS скрипт должен быть полностью вырезан nh3
    assert "<script>" not in res
    assert "alert('xss')" not in res
    assert "<strong>жирный</strong>" in res


def test_website_tables_and_lists():
    text = "- Пункт 1\n- Пункт 2\n\n| Заголовок |\n|---|\n| Ячейка |"
    res = FormatService.format_message(text, "website_widget")
    assert "<ul>" in res
    assert "<li>Пункт 1</li>" in res
    assert "<table>" in res
    assert "<th>Заголовок</th>" in res
    assert "<td>Ячейка</td>" in res