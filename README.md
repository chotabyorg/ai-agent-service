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
- Опциональная подстановка контекста из `knowledge-base-service` перед LLM-вызовом.
- Изолированный запуск MCP-серверов с динамическим пробросом `env` под каждого агента.
- Google Workspace MCP server для Google Sheets lead management (Stage 1).

## Технологический стек

- Python 3.11+
- FastAPI
- SQLAlchemy 2.x + async sessions
- PostgreSQL
- Redis
- OpenAI-compatible API (`openai.AsyncOpenAI`)
- Model Context Protocol (`mcp==1.26.0`). This exact version is pinned because the bundled Bitrix server imports `mcp.server.fastmcp`, which was removed in SDK 2.x; change the pin only with a server migration and stdio handshake coverage.
- Bitrix24 MCP server (`fast-bitrix24`)
- Google API Python Client для Google Workspace MCP

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

## Google Workspace MCP (Stages 1-4)

Google Workspace MCP server:

```text
mcp_clients/servers/google_workspace/server.py
```

Он запускается тем же механизмом `MCPManager`, что и Bitrix MCP: credentials конкретного агента пробрасываются в env только для изолированного subprocess.

Текущий статус:

- Google OAuth 2.0 configuration на refresh token.
- Google Sheets metadata/tabs/range read.
- Google Sheets append/update/create spreadsheet/create tab.
- Lead upsert в Google Sheets.
- Lead deduplication по `email`, `phone`, `telegram`, `website` или `custom:<field>`.
- Lead status update с timestamped note.
- Dry-run режим для write tools.
- Confirmation gate для `google_sheets_update_range`.
- Google Calendar list/search/create/update/delete event tools.
- Follow-up scheduling для лида через Google Calendar.
- Confirmation + `ENABLE_DESTRUCTIVE_TOOLS=true` gate для удаления Calendar events.
- Gmail search/read thread/create draft/reply draft tools.
- Gmail send draft tool с `ENABLE_GMAIL_SEND=true`, `requiresConfirmation=true` и отдельным `gmail.send` scope.
- Google Drive search/create folder/create text file tools.
- Google Contacts search/upsert tools with dedupe.
- Final hardening: least-privilege scopes, dry-run write paths, confirmation gates, bounded batch sizes and structured non-secret responses.

В credentials агента для сервиса `google_workspace` нужно сохранить:

```json
{
  "client_id": "...",
  "client_secret": "...",
  "redirect_uri": "http://localhost:8080/oauth2/callback",
  "refresh_token": "...",
  "scopes": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive.metadata.readonly https://www.googleapis.com/auth/contacts",
  "token_storage_mode": "local"
}
```

Минимальный локальный запуск MCP server:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python mcp_clients/servers/google_workspace/server.py
```

Документация:

- `docs/google-oauth-setup.md`
- `docs/tools.md`
- `docs/lead-workflows.md`
- `docs/security-hardening.md`

## Память диалогов

- История хранится в Redis.
- Ключ истории: `chat_history:{session_id}`.
- В Redis сохраняются только сообщения `user/assistant`.
- `system_prompt` не пишется в память, а добавляется только на время LLM-запроса.

## Основной флоу `/v1/chat/completions`

The chat endpoint keeps its existing JSON request and response. Trusted callers send `X-Service-Token` from the dedicated `CHAT_S2S_TOKEN` and the agent owner in `X-User-Id`; agent lookup is scoped by that owner before a session is created or an LLM/tool call begins. `CHAT_S2S_AUTH_REQUIRED=false` supports rolling out the headers to callers. Production must set it to `true` after the token is installed in KrakenD and every chat caller. This token is separate from `INTERNAL_API_TOKEN`, which continues to protect `/internal/v1/*` endpoints.

1. Найти агента по `agent_id`.
2. Проверить, что агент активен.
3. Найти `ConversationSession` по `session_id` и убедиться, что она принадлежит этому агенту.
4. Загрузить Redis-историю.
5. Если включен `KNOWLEDGE_BASE_ENABLED`, запросить внутренний retrieval context у `knowledge-base-service`.
6. Поднять MCP managers для доступных credentials.
7. Передать в LLM:
   - `system_prompt`
   - опциональный Knowledge Base context system message
   - историю диалога
   - новое сообщение пользователя
   - OpenAI-compatible tools, собранные из MCP
8. Если модель вернула `tool_calls`, выполнить их через `MCPManager.call_tool()` и сделать следующий виток цикла.
9. Когда модель возвращает обычный текст, сохранить новый `user/assistant` turn в Redis и вернуть ответ клиенту.

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
| `LLM_MAX_TOKENS` | `1024` | Максимум output/completion tokens для OpenAI-compatible provider |
| `LLM_HTTP_REFERER` | `null` | Опциональный `HTTP-Referer` для OpenRouter |
| `LLM_APP_TITLE` | `null` | Опциональный `X-Title` для OpenRouter |
| `PRIVACY_FILTER_ENABLED` | `false` | Если `true`, все внешние LLM-вызовы идут через privacy-filter-service |
| `PRIVACY_FILTER_BASE_URL` | `http://privacy-filter-service:8000` | Внутренний URL privacy-filter-service |
| `PRIVACY_FILTER_TIMEOUT_SECONDS` | `30` | Таймаут запроса к privacy-filter-service |
| `ENCRYPTION_KEY` | — | Существующий Fernet-ключ для API-ключей моделей и MCP credentials; потеря ключа сделает зашифрованные значения нечитаемыми |
| `MCP_CREDENTIALS_ENCRYPTION_ENABLED` | `false` | Шифровать новые MCP credentials; включать после выкладки совместимого dual-read образа |
| `KNOWLEDGE_BASE_ENABLED` | `false` | Временный global feature flag / kill switch для Knowledge Base retrieval |
| `KNOWLEDGE_BASE_SERVICE_URL` | `http://knowledge-base-service:8000` | Внутренний URL knowledge-base-service |
| `KNOWLEDGE_BASE_TIMEOUT_SECONDS` | `2.0` | Таймаут internal retrieval запроса |
| `KNOWLEDGE_BASE_MAX_CHARS_PER_CHUNK` | `1000` | Максимум символов одного KB chunk в prompt |
| `KNOWLEDGE_BASE_MAX_TOTAL_CONTEXT_CHARS` | `3000` | Максимум символов всего KB context system message |
| `MCP_SERVER_PATH` | `mcp_service/mcp_server.py` | Резервный путь для custom MCP server |
| `MCP_BITRIX_PATH` | `mcp_clients/servers/bitrix_server.py` | Локальный путь к Bitrix MCP server |
| `MCP_GOOGLE_PATH` | `mcp_service/google_server.py` | Резервный путь для Google MCP server |
| `INTERNAL_API_TOKEN` | `""` | Токен для service-to-service вызовов `/internal/*` эндпоинтов. Проверяется через заголовок `X-Service-Token`. Пустое значение отключает доступ (fail-closed). |
| `CHAT_S2S_TOKEN` | `""` | Separate credential for `/v1/chat/completions` callers; do not reuse `INTERNAL_API_TOKEN`. |
| `CHAT_S2S_AUTH_REQUIRED` | `false` | Compatibility switch; set to `true` only after KrakenD and all chat callers have the dedicated token and owner header. |

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

PRIVACY_FILTER_ENABLED=false
PRIVACY_FILTER_BASE_URL=http://privacy-filter-service:8000
```

### Анонимизация ПД через privacy-filter-service

По умолчанию сервис работает как раньше и напрямую вызывает OpenRouter. Если установить
`PRIVACY_FILTER_ENABLED=true`, `ai-agent-service` сохраняет текущую orchestration-логику
агентов, истории и MCP, но каждый внешний LLM-вызов отправляет во внутренний
`privacy-filter-service`.

В privacy mode сервис передает в proxy:
- `messages`, `tools`, модель, температуру и `parallel_tool_calls`;
- per-agent OpenRouter key;
- `ConversationSession.id` как `privacy_session_id`.
- общий `INTERNAL_API_TOKEN` как Bearer-токен внутреннего вызова.

Если privacy-filter-service недоступен или не настроен, запрос завершается ошибкой
и прямого fallback на OpenRouter нет. Это сделано специально, чтобы при включенной
анонимизации авария proxy не приводила к отправке ПД внешнему провайдеру.

### Knowledge Base retrieval hook

По умолчанию hook выключен: `KNOWLEDGE_BASE_ENABLED=false`. Это временный global
feature flag / kill switch для первого rollout. Per-agent/per-KB управление будет
добавлено отдельной фазой.

Когда hook включен, `LLMService` перед первым LLM-вызовом запрашивает:

```text
POST {KNOWLEDGE_BASE_SERVICE_URL}/internal/v1/retrieval/context
X-Service-Token: {INTERNAL_API_TOKEN}
```

Payload строится только из доверенного состояния сервиса:

- `owner_id` берется из `agent.user_id`;
- `agent_id` берется из загруженного агента;
- `query` — текущее сообщение пользователя.

`top_k` и `similarity_threshold` пока не передаются: defaults применяются внутри
`knowledge-base-service`.

Если retrieval вернул contexts, сервис добавляет отдельный system message сразу
после основного system prompt и перед Redis history/user message:

```text
<knowledge_base_context>
Use these snippets as company knowledge. If snippets are irrelevant or insufficient, do not invent facts.

[1] source="faq.pdf" chunk_index=2 score=0.88
...
</knowledge_base_context>
```

Контекст ограничивается `KNOWLEDGE_BASE_MAX_CHARS_PER_CHUNK` и
`KNOWLEDGE_BASE_MAX_TOTAL_CONTEXT_CHARS`. Raw metadata из retrieval response в
prompt не добавляется.

`LLM_MAX_TOKENS` ограничивает completion/output tokens для OpenAI-compatible
provider. Это важно для OpenRouter cost/credit control: сервис не должен
отправлять провайдеру неограниченный или чрезмерный output token budget.

Hook работает fail-open: timeout, недоступность `knowledge-base-service`, плохой
response или неожиданная ошибка форматирования логируются как warning, после чего
чат продолжается без KB context. Пользователь не получает internal retrieval
ошибку. При включенном privacy proxy KB context идет в тот же `messages` payload,
что и остальные system/history/user сообщения.

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

### Шифрование MCP credentials

MCP credentials используют существующий `ENCRYPTION_KEY`. Значение должно быть
одинаковым для всех pod и миграционной Job и сохраненным в backup: потеря ключа
сделает недоступными как MCP credentials, так и зашифрованные API-ключи моделей.
Текущая реализация Fernet не поддерживает идентификаторы ключей или автоматическую
ротацию; перед заменой ключа потребуется отдельная контролируемая расшифровка и
повторное шифрование всех зависимых значений.

Rollout выполняется двумя этапами, без остановки сервиса:

1. Развернуть образ с чтением legacy JSON objects и версионированных encrypted
   envelopes, оставив `MCP_CREDENTIALS_ENCRYPTION_ENABLED=false`.
2. После готовности всех совместимых pod включить flag и запустить отдельную
   Argo PostSync Job: `python -m scripts.encrypt_mcp_credentials --batch-size 100
   --max-passes 3`. Job обрабатывает ограниченные batches и безопасно продолжается
   при повторном запуске; код завершения 0 подтверждает, что plaintext objects не
   остались. Во время backfill сервис продолжает читать оба формата.

Откат после появления envelopes допустим только на образ, который умеет их читать.
При откате оставляйте flag включенным, чтобы записи оставались зашифрованными.
Отключение flag снова пишет plaintext для новых/обновленных строк. Автоматического
переписывания данных обратно в plaintext нет.

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
      "webhook_url": "https://your-domain.bitrix24.by/rest/1/secret/"
    }
  }'
```

Минимальный JSON payload для Bitrix credentials:

```json
{
  "service_name": "bitrix",
  "credentials": {
    "webhook_url": "https://your-domain.bitrix24.by/rest/1/secret/"
  }
}
```

Пример discovery-запроса доступных интеграций:

```bash
curl http://localhost:8000/v1/mcp-services
```

### Models API

- `GET /v1/models` — получить список доступных LLM-моделей.

### Internal API (service-to-service)

Эндпоинты под префиксом `/internal/v1` предназначены для вызовов из других внутренних сервисов (например, `subscription-service` и `control-plane-service`) и **не проходят через API Gateway**. Каждый запрос должен содержать заголовок `X-Service-Token` со значением, равным `INTERNAL_API_TOKEN`. Сравнение выполняется через `secrets.compare_digest` для защиты от timing-атак. Если `INTERNAL_API_TOKEN` не задан на сервере — любой запрос отклоняется (fail-closed).

- `GET /internal/v1/agents/count?user_id=<uuid>` — вернуть количество агентов пользователя.
  - Response `200`: `{ "total": int, "active": int }` — `total` считает всех агентов пользователя, `active` — только тех, у кого `is_active = true`.
  - `400` — если `user_id` не UUID.
  - `401` — если заголовок `X-Service-Token` отсутствует или не совпадает.
- `POST /internal/v1/agents/ownership-check` — проверить пару владельца и агента по JSON-телу `{ "agent_id": "<uuid>", "owner_id": "<uuid>" }`.
  - Response `204` — агент принадлежит указанному владельцу; тело ответа пустое.
  - `404` — агент не существует или принадлежит другому владельцу; оба случая имеют одинаковый ответ.
  - `401` — если заголовок `X-Service-Token` отсутствует или не совпадает.
  - `422` — если `agent_id` или `owner_id` не UUID.
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
