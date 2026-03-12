### **Техническое задание (ТЗ): Микросервис ai-agent-service (MVP)**

**1\. Цель сервиса**

Предоставление единого API для маршрутизации сообщений пользователей к настроенным ИИ-агентам, генерации ответов с использованием LLM, вызова внешних инструментов (MCP) и сохранения контекста диалога.

**2\. Стек технологий (MVP)**

*   **Язык:** Python 3.11+
    
*   **Фреймворк:** FastAPI (идеален для микросервисов, нативный async, автогенерация Swagger/OpenAPI).
    
*   **База данных (Конфигурации):** PostgreSQL (через SQLAlchemy или SQLModel).
    
*   **Кэш/Память диалогов:** Redis (уже есть хорошая реализация).
    
*   **Взаимодействие с LLM:** openai (для OpenRouter/OpenAI API).
    
*   **Интеграция инструментов:** MCP (Model Context Protocol).
    

**3\. Основные сущности базы данных (PostgreSQL)**

*   **AgentTemplate (Конфигурация агента):**
    
*   id: UUID (Primary Key)
    
*   user\_id: UUID (ID владельца бизнеса из UserService)
    
*   name: String (Например, "Администратор клиники", "Менеджер по продажам")
    
*   system\_prompt: Text
    
*   llm\_provider: String (Например, "openrouter", "openai")
    
*   model\_name: String (Например, "google/gemini-2.5-flash")
    
*   temperature: Float
    
*   is\_active: Boolean
    
*   **AgentCredentials (Секреты для инструментов агента):**
    
*   agent\_id: UUID (Foreign Key)
    
*   service\_name: String ("bitrix", "google\_workspace")
    
*   credentials: JSONB (Зашифрованные токены, URL вебхуков. В MVP можно хранить просто в JSONB).
    
*   **ConversationSession (Сессии диалогов):**
    
*   id: UUID (Primary Key)
    
*   agent\_id: UUID (Foreign Key)
    
*   external\_client\_id: String (ID клиента в Instagram/TG)
    
*   channel: String ("instagram", "telegram", "widget")
    
*   created\_at: DateTime
    

**4\. Публичное API (REST для внутренних сервисов)**

*   POST /v1/agents — Создать нового агента (вызывается из панели управления сайтом).
    
*   PUT /v1/agents/{agent\_id} — Обновить промпт или настройки.
    
*   POST /v1/chat/completions — Основной эндпоинт для отправки сообщений.
    
*   _Payload:_ session\_id, agent\_id, message, client\_id, channel.
    
*   _Response:_ Server-Sent Events (SSE) стриминг ответа, либо синхронный JSON (в зависимости от нужд фронтенда/Instagram API).
    

**5\. Жизненный цикл обработки сообщения (Флоу)**

1.  Внешний сервис (например, вебхук-приемник от Instagram) шлет POST-запрос на POST /v1/chat/completions в наш сервис.
    
2.  Сервис идет в БД (PostgreSQL) по agent\_id и достает system\_prompt и настройки модели.
    
3.  Сервис достает из AgentCredentials ключи (например, URL Битрикса клиента).
    
4.  Сервис идет в Redis и достает историю диалога по session\_id.
    
5.  Сервис инициализирует нужные MCP-серверы (или подключается к уже запущенным пулам), передавая им нужные переменные окружения клиента.
    
6.  Происходит вызов LLM (твой метод ask\_stream).
    
7.  Ответ сохраняется в Redis, а финальный текст отдается обратно внешнему сервису для отправки в Instagram.
    

**6\. Главные архитектурные вызовы для решения в MVP**

*   **Изоляция MCP:** Если 10 разных клиентов используют интеграцию с Битрикс24, у них 10 разных B24\_WEBHOOK\_URL. Текущий код запускает MCP сервер глобально (через os.environ.copy()). В SaaS нужно будет либо динамически передавать URL внутрь MCP-сервера при каждом вызове инструмента, либо поднимать изолированные легковесные процессы/контейнеры для каждого клиента. Для MVP проще всего модифицировать bitrix\_server.py так, чтобы инструменты принимали webhook\_url как аргумент (скрытый от LLM, подставляемый сервисом), а не брали из ENV.

**Структура проекта (Слоистая архитектура):**
Проект должен строго следовать принципам Dependency Injection и разделения ответственности (Layered Architecture).

```
app/
├── api/             # Endpoints (Routers/Controllers). Только валидация ввода/вывода.
│   ├── dependencies.py # Зависимости FastAPI (например, get_db, get_current_user)
│   └── v1/          # Версионирование API
├── core/            # Настройки, конфигурация (Pydantic BaseSettings), логирование
├── db/              # Настройки подключения к БД (PostgreSQL, SQLAlchemy async session)
├── models/          # ORM модели базы данных (SQLAlchemy)
├── schemas/         # Pydantic v2 модели (DTO для Request/Response)
├── services/        # Бизнес-логика. Здесь живет класс AIAgentService и работа с БД.
├── mcp_clients/     # Логика инициализации и управления сессиями MCP серверов
└── main.py          # Точка входа FastAPI
```

**Стратегия изоляции MCP-инструментов (Multi-tenancy):**
В системе будут агенты разных клиентов. Нельзя использовать глобальные переменные окружения (`os.environ`) для ключей доступа (например, `B24_WEBHOOK_URL`).
1. Ключи доступа клиента хранятся в БД (таблица `AgentCredentials`).
2. При поступлении нового сообщения от пользователя, `AIAgentService` достает ключи из БД.
3. Сервис инициализирует `StdioServerParameters` для MCP-сервера локально, передавая ключи конкретного клиента ИСКЛЮЧИТЕЛЬНО в параметр `env` этого вызова. 
4. Таким образом, для каждого активного агента поднимается свой изолированный sub-process MCP-сервера со своими доступами. Когда сессия общения завершается/засыпает, процесс можно гасить (или держать в пуле).



