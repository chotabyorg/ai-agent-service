# API Documentation

## PUT `/v1/agents/{agent_id}/credentials`

Создает или обновляет credentials конкретного MCP-сервиса для агента.

### Headers

- `X-User-Id: <uuid>` (обязателен)
- `Content-Type: application/json`

### Request body

```json
{
  "service_name": "bitrix",
  "credentials": {
    "portal_url": "https://your-portal.bitrix24.com",
    "webhook_url": "https://your-portal.bitrix24.com/rest/1/secret_token/"
  }
}
```

### Bitrix validation rules

- Сначала проверяется владение агентом: если агент не существует или не принадлежит пользователю, API возвращает `404 Agent not found` без внешней Bitrix валидации.
- `webhook_url` обязателен для текущего runtime (`B24_WEBHOOK_URL`).
- Если `webhook_url` передан, он должен быть в формате:
  `https://<portal>.bitrix24.<tld>/rest/<user_id>/<token>/`
  (допускаются method suffix и query params, они будут отброшены при нормализации),
  и дополнительно проверяется через `POST {webhook_url}server.time`.
- `portal_url` опционален, но если передан, должен быть формата `https://<portal>.bitrix24.<tld>`.
- До сохранения выполняется ping REST API через `server.time`.

### Responses

- `200 OK` — credentials сохранены.
- `404 Not Found` — агент не найден или не принадлежит текущему пользователю.
- `422 Unprocessable Entity` — неверный формат `portal_url`/`webhook_url`, портал недоступен, webhook невалиден или недостаточно прав.
- `502 Bad Gateway` — временная проблема у Bitrix REST API (rate-limit/overload/ошибка апстрима).

### Example: minimal Bitrix payload

```json
{
  "service_name": "bitrix",
  "credentials": {
    "webhook_url": "https://your-portal.bitrix24.com/rest/1/secret_token/"
  }
}
```

## GET `/v1/mcp-services`

Возвращает список поддерживаемых MCP-интеграций и обязательные поля credentials.

### Example response

```json
[
  {
    "service_name": "bitrix",
    "display_name": "Bitrix24 CRM",
    "description": "Интеграция с вашей CRM Bitrix24",
    "required_keys": ["webhook_url"]
  }
]
```
*Поведение форматирования:*
Сервис автоматически применяет форматирование к полю `answer` в зависимости от переданного `channel`:
- `telegram`: Возвращает безопасный Telegram HTML (поддерживает `<b>`, `<i>`, `<a>`, `<code>`, `<pre>`).
- `instagram`: Возвращает Plain Text, очищенный от Markdown. Абзацы начинаются с красной строки (символ `\u2003`).
- `website_widget`: Возвращает полноценный HTML (теги `<p>`, `<br>`, `<ul>` и т.д.) со стилизацией отступов.
