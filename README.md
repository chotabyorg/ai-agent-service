# AI Agent Service

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-REST-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-config%20storage-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-dialog%20memory-DC382D?logo=redis&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-LLM-412991?logo=openai&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-isolated%20tools-111111)

`ai-agent-service` — это SaaS-микросервис для маршрутизации пользовательских сообщений к настроенным AI-агентам. Сервис хранит конфигурации агентов в PostgreSQL, историю диалогов в Redis, генерирует ответы через OpenAI-compatible API и умеет подключать MCP-инструменты в изолированных subprocess-процессах под конкретного агента.

## Что умеет сервис

- CRUD для конфигураций AI-агентов.
- Хранение tenant-specific credentials для внешних инструментов.
- Память диалогов по `session_id` в Redis.
- Обработка чата через `/v1/chat/completions`.
- Изолированный запуск MCP-серверов с динамическим пробросом `env` под каждого агента.

## Технологический стек

- Python 3.11+
- FastAPI
- SQLAlchemy 2.x + async sessions
- PostgreSQL
- Redis
- OpenAI-compatible API (`openai.AsyncOpenAI`)
- Model Context Protocol (`mcp`)
- Bitrix24 MCP server (`fast-bitrix24`)

## Архитектура

Сервис следует слоистой архитектуре.

```text
ai-agent-service/
├── api/           # HTTP слой: роутеры и зависимости FastAPI
├── core/          # Конфиг, Redis lifecycle
├── db/            # Async SQLAlchemy engine/session
├── models/        # ORM модели PostgreSQL
├── schemas/       # Pydantic DTO
├── services/      # Бизнес-логика
├── mcp_clients/   # MCP manager и встроенные MCP server scripts
└── main.py        # Точка входа FastAPI
```

### Layered Architecture

- `api`: принимает HTTP-запросы, валидирует payload и преобразует ошибки в HTTP-ответы.
- `schemas`: описывает request/response модели.
- `services`: содержит бизнес-флоу, не завязанный на FastAPI.
- `models` и `db`: отвечают за доступ к PostgreSQL.
- `core`: хранит настройки приложения и lifecycle инфраструктуры.
- `mcp_clients`: инкапсулирует tenant-isolated запуск MCP subprocess.

## Multi-tenancy и изоляция MCP

Ключевое архитектурное решение — не хранить бизнес-секреты в глобальном процессе приложения.

Как это работает:

1. Credentials агента хранятся в PostgreSQL в таблице `AgentCredentials`.
2. При обработке чата `LLMService` загружает credentials конкретного агента.
3. Для каждого поддерживаемого сервиса создается `MCPManager` (реестр сервисов в `core/config.py` → `mcp_services`).
4. `MCPManager` читает конфиг сервиса из реестра, маппит credentials в env-переменные через `env_mapping`.
5. После этого поднимается отдельный MCP subprocess через `StdioServerParameters`.
6. По завершении ответа subprocess закрывается через `AsyncExitStack`.

Каждый MCP-инструмент имеет метаданные `category` и `tier` (core/extended), доступные через ресурс `tools://metadata`.

**Семантика `enabled_tools`:**
- `null` — включены только core-инструменты (базовые).
- `["*"]` — включены все инструменты.
- `["tool1", "tool2"]` — включены только перечисленные.

Итог:

- нет общего глобального `B24_WEBHOOK_URL` для всех клиентов;
- инструменты одного агента изолированы от другого;
- секреты пробрасываются только на время конкретной обработки сообщения.

## Память диалогов

- История хранится в Redis.
- Ключ истории: `chat_history:{session_id}`.
- В Redis сохраняются только сообщения `user/assistant`.
- `system_prompt` не пишется в память, а добавляется только на время LLM-запроса.

## Основной флоу `/v1/chat/completions`

1. Найти агента по `agent_id`.
2. Проверить, что агент активен.
3. Найти `ConversationSession` по `session_id` и убедиться, что она принадлежит этому агенту.
4. Загрузить Redis-историю.
5. Поднять MCP managers для доступных credentials.
6. Передать в LLM:
   - `system_prompt`
   - историю диалога
   - новое сообщение пользователя
   - OpenAI-compatible tools, собранные из MCP
7. Если модель вернула `tool_calls`, выполнить их через `MCPManager.call_tool()` и сделать следующий виток цикла.
8. Когда модель возвращает обычный текст, сохранить новый `user/assistant` turn в Redis и вернуть ответ клиенту.
9. Перед отдачей ответа клиенту `FormatService` преобразует сырой Markdown в формат, совместимый с `channel` (HTML для сайтов/Telegram, Plain Text для Instagram).

## Переменные окружения

Ниже перечислены настройки из `core/config.py`.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `APP_NAME` | `ai-agent-service` | Имя приложения |
| `APP_VERSION` | `0.1.0` | Версия сервиса |
| `DEBUG` | `false` | Режим отладки FastAPI |
| `API_V1_PREFIX` | `/v1` | Базовый API prefix |
| `POSTGRES_HOST` | `localhost` | Хост PostgreSQL |
| `POSTGRES_PORT` | `5432` | Порт PostgreSQL |
| `POSTGRES_DB` | `ai_agent_service` | Имя базы |
| `POSTGRES_USER` | `postgres` | Пользователь PostgreSQL |
| `POSTGRES_PASSWORD` | `postgres` | Пароль PostgreSQL |
| `POSTGRES_ECHO` | `false` | SQLAlchemy SQL echo |
| `REDIS_HOST` | `redis` | Хост Redis |
| `REDIS_PORT` | `6379` | Порт Redis |
| `REDIS_DB` | `0` | База Redis |
| `REDIS_PASSWORD` | `null` | Пароль Redis |
| `HISTORY_TTL_SECONDS` | `86400` | TTL истории диалога |
| `HISTORY_MAX_ITEMS` | `20` | Максимум сообщений в истории |
| `LLM_PROVIDER` | `openrouter` | Логический provider label |
| `LLM_API_KEY` | `""` | API ключ OpenAI/OpenRouter |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Base URL OpenAI-compatible API |
| `LLM_DEFAULT_MODEL` | `google/gemini-2.5-flash` | Модель по умолчанию |
| `LLM_DEFAULT_TEMPERATURE` | `0.7` | Температура по умолчанию |
| `LLM_HTTP_REFERER` | `null` | Опциональный `HTTP-Referer` для OpenRouter |
| `LLM_APP_TITLE` | `null` | Опциональный `X-Title` для OpenRouter |
| `MCP_SERVER_PATH` | `mcp_service/mcp_server.py` | Резервный путь для custom MCP server |
| `MCP_BITRIX_PATH` | `mcp_clients/servers/bitrix_server.py` | Локальный путь к Bitrix MCP server |
| `MCP_GOOGLE_PATH` | `mcp_service/google_server.py` | Резервный путь для Google MCP server |
| `INTERNAL_API_TOKEN` | `""` | Токен для service-to-service вызовов `/internal/*` эндпоинтов. Проверяется через заголовок `X-Service-Token`. Пустое значение отключает доступ (fail-closed). |

### Минимальный `.env` пример

```env
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=ai_agent_service
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

LLM_API_KEY=your_api_key
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_DEFAULT_MODEL=google/gemini-2.5-flash
```

## Установка и запуск

Все команды ниже выполняются из директории `ai-agent-service/`.

### 1. Создать виртуальное окружение

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Установить зависимости

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Подготовить переменные окружения

Создай `.env` рядом с `main.py` и укажи значения для PostgreSQL, Redis и LLM.

Для запуска через Docker Compose используй сервисные хосты:

```env
POSTGRES_HOST=postgres
REDIS_HOST=redis
```

Если запускаешь приложение вне Docker, обычно нужны:

```env
POSTGRES_HOST=localhost
REDIS_HOST=localhost
```

### 4. Запустить сервис

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

То же самое через `make`:

```bash
make run
```

После старта:

- healthcheck: `GET http://localhost:8000/health`
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Управление базой данных

Для работы с Alembic используйте команды из `Makefile`:

```bash
make revision msg="Initial setup"
make upgrade
make downgrade
```

- `make run` — запускает FastAPI-приложение через `uvicorn` с `--reload`.
- `make revision msg="message"` — создает новую Alembic-миграцию через `--autogenerate`.
- `make upgrade` — применяет все миграции до `head`.
- `make downgrade` — откатывает базу на одну ревизию назад.

## Запуск через Docker

Для локального запуска сервиса вместе с PostgreSQL и Redis используйте:

```bash
docker-compose up --build -d
```

Контейнер приложения при старте сначала выполняет `alembic upgrade head`, а затем поднимает `uvicorn` на `0.0.0.0:8000`.

## API Endpoints

### Health

- `GET /health` — проверить, что сервис поднят.

### Agents API

- Все endpoints управления агентами ожидают заголовок `X-User-Id`, который прокидывается API Gateway после валидации JWT.
- `POST /v1/agents/` — создать агента.
- `GET /v1/agents/` — получить список агентов текущего пользователя.
- `GET /v1/agents/{agent_id}` — получить конфигурацию агента.
- `PUT /v1/agents/{agent_id}` — обновить настройки агента.
- `DELETE /v1/agents/{agent_id}` — удалить агента.
- `PUT /v1/agents/{agent_id}/credentials` — добавить или обновить credentials для MCP.
- `GET /v1/agents/{agent_id}/credentials` — получить список подключённых credentials агента.
- `DELETE /v1/agents/{agent_id}/credentials/{service_name}` — удалить credentials агента по имени сервиса.
- `GET /v1/mcp-services` — получить список доступных MCP-интеграций.

Пример создания агента:

```bash
curl -X POST http://localhost:8000/v1/agents/ \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -d '{
    "name": "Sales Agent",
    "system_prompt": "You are a helpful sales assistant.",
    "model_name": "google/gemini-2.5-flash",
    "temperature": 0.7,
    "is_active": true
  }'
```

Пример сохранения Bitrix credentials:

```bash
curl -X PUT http://localhost:8000/v1/agents/11111111-1111-1111-1111-111111111111/credentials \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -d '{
    "service_name": "bitrix",
    "credentials": {
      "portal_url": "https://your-portal.bitrix24.by",
      "webhook_url": "https://your-domain.bitrix24.by/rest/1/secret/"
    }
  }'
```

Минимальный JSON payload для Bitrix credentials:

```json
{
  "service_name": "bitrix",
  "credentials": {
    "portal_url": "https://your-portal.bitrix24.by"
  }
}
```

Правила валидации Bitrix credentials на бэке:

- `portal_url` обязателен и должен быть в формате `https://<portal>.bitrix24.<tld>`.
- До сохранения выполняется проверка доступности портала через `POST {portal_url}/rest/server.time`.
- `webhook_url` опционален. Если передан, проверяется формат `https://<portal>.bitrix24.<tld>/rest/<user_id>/<token>/` и валидность через `server.time`.
- Для фактического выполнения Bitrix MCP tools в текущей реализации нужен валидный `webhook_url` (он пробрасывается в `B24_WEBHOOK_URL`).
- При `4xx` проблемах валидации API возвращает `422`, при проблемах апстрима (`rate limit`, перегрузка) — `502`.

Пример discovery-запроса доступных интеграций:

```bash
curl http://localhost:8000/v1/mcp-services
```

### Models API

- `GET /v1/models` — получить список доступных LLM-моделей.

### Internal API (service-to-service)

Эндпоинты под префиксом `/internal/v1` предназначены для вызовов из других внутренних сервисов (например, `subscription-service`) и **не проходят через API Gateway**. Каждый запрос должен содержать заголовок `X-Service-Token` со значением, равным `INTERNAL_API_TOKEN`. Сравнение выполняется через `secrets.compare_digest` для защиты от timing-атак. Если `INTERNAL_API_TOKEN` не задан на сервере — любой запрос отклоняется (fail-closed).

- `GET /internal/v1/agents/count?user_id=<uuid>` — вернуть количество агентов пользователя.
  - Response `200`: `{ "total": int, "active": int }` — `total` считает всех агентов пользователя, `active` — только тех, у кого `is_active = true`.
  - `400` — если `user_id` не UUID.
  - `401` — если заголовок `X-Service-Token` отсутствует или не совпадает.
- `POST /internal/v1/agents/deactivate-all` — массово деактивировать всех агентов пользователя.
  - Request body: `{ "user_id": "<uuid>" }`.
  - Response `200`: `{ "deactivated": int }` — количество записей, у которых `is_active` переведён с `true` на `false`. Если активных нет — возвращает `{ "deactivated": 0 }`, а не 404.
  - `401` — если заголовок `X-Service-Token` отсутствует или не совпадает.
  - `422` — если в теле нет `user_id` или он не UUID.

Пример проверки лимита агентов:

```bash
curl -H "X-Service-Token: $INTERNAL_API_TOKEN" \
  "http://localhost:8000/internal/v1/agents/count?user_id=11111111-1111-1111-1111-111111111111"
```

Пример массовой деактивации при истечении подписки:

```bash
curl -X POST http://localhost:8000/internal/v1/agents/deactivate-all \
  -H "Content-Type: application/json" \
  -H "X-Service-Token: $INTERNAL_API_TOKEN" \
  -d '{"user_id": "11111111-1111-1111-1111-111111111111"}'
```

### Chat API

- `POST /v1/chat/completions` — обработать сообщение пользователя через LLM + Redis history + MCP tools.

Пример запроса:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "11111111-1111-1111-1111-111111111111",
    "external_client_id": "telegram:123456",
    "channel": "telegram",
    "message": "Найди контакт Иван Петров в Bitrix"
  }'
```

Пример ответа:

```json
{
  "answer": "Я нашел контакт Иван Петров. Чем еще помочь?"
}
```

## Текущие ограничения MVP

- SSE streaming пока не реализован.
- Встроенный MCP manager сейчас настроен прежде всего под Bitrix24.
- Автоматическое применение миграций при старте сервиса не добавлено.
- Авторизация service-to-service реализована только для `/internal/v1/*` эндпоинтов (через `X-Service-Token`). Для публичных `/v1/*` эндпоинтов внутренний вызов ai-agent-service из других микросервисов по-прежнему полагается на доверие внутри Docker-сети.

## Полезные файлы

- `main.py` — точка входа FastAPI
- `core/config.py` — настройки приложения
- `core/redis.py` — lifecycle Redis
- `services/llm_service.py` — главный orchestration flow
- `mcp_clients/manager.py` — isolated MCP subprocess manager
- `api/v1/chat.py` — Chat API
