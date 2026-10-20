# P1a: реализация, проверки и ограничение приёмки

Дата: 20 октября 2026, Europe/Minsk. Изменён только `ai-agent-service`.
База — `05d229c55cf141edfdc3ad72e066baf53cbae5ed`; проверенный application commit —
`d8f847ca9b4a988bdf38455b2e4d1a81050144f3`.

**Полный локальный suite не зелёный:** candidate имеет 2218 PASS и один FAILED
в существующем Business VOICE cycle test. Этот же тест падал на исходном main;
он не помечен `xfail` и не переименован в expected security gap. Новые 52 P1a
проверки и все PostgreSQL safety cases P0a прошли.

## Что исправлено

Ownership-check и count/deactivate-all принимали один `INTERNAL_API_TOKEN`.
Поэтому credential, выданный для проверки принадлежности агента, также давал
management-доступ. Добавлен отдельный receiver-owned principal с единственным
purpose `agent-ownership-check`, точными owner/agent pair grants либо явно
назначенным multi-tenant predicate scope. Эти credentials не принимаются
management, chat, managed execution или Business authority механизмами.

У стабильного principal не более двух версий: current и optional previous.
Есть отзыв principal/версии, запрет коллизий credentials и неоднозначных headers,
безопасные startup errors и исключение нового секрета из MCP child environment.
Независимые owner/agent allowlists не используются. SQL owner predicate остаётся
обязательным даже при matching grant или multi-tenant scope.

Тело запроса, UUID validation и 204/404 контракт сохранены. Public gateway routes
не менялись; его API_DOCUMENTATION.md делегирует internal/S2S контракты сервисам.
Полный additive auth contract и prerequisites P1b находятся в
[internal-ownership-auth.md](internal-ownership-auth.md).

## Результаты

| Проверка | Результат |
| --- | --- |
| Исходный main, полный набор | 2167: 2143 PASS, 24 FAILED, 0 skipped |
| Candidate, полный набор | 2219: 2218 PASS, 1 FAILED, 0 skipped |
| Новые P1a tests, subset полного candidate | 52 PASS, включая реальный SQL owner filter |
| P0a ASGI, исходный main | 21 PASS, 2 EXPECTED SECURITY GAP |
| P0a ASGI, candidate | 22 PASS, 1 EXPECTED SECURITY GAP — прежний owner header trust/P1c |
| P0a AI PostgreSQL gate functions | Все 6 функций: 9 PASS с параметрами |
| Компиляция источников и Ruff 0.13.2, правила quality workflow | PASS |

Сохранены исходные assertions P0a PR1
`f0c8a94c035696260c2fb0ae1959d95aa36b2041` в `chota-metrics`.
AI-local adapter меняет только provisioning отдельного synthetic ownership
principal и снимает marker с исправленного ownership→management gap.
Management negative для chat по-прежнему получает именно management credential.
Unexpected failures не переводятся в известные gaps. Старый output P0a удаляется
перед worker; отсутствие свежего отчёта завершает запуск ошибкой.

Шесть PostgreSQL функций P0a, выполненных внутри полного candidate:

- `test_business_runtime::test_definitive_sidecar_denial_is_retained_no_dispatch`;
- `test_business_runtime::test_forged_receipt_cannot_claim_success`;
- `test_business_runtime::test_stale_confirmation_never_dispatches` — четыре параметра;
- `test_business_concurrency::test_receipt_finalize_overlap_and_stale_token_cannot_overwrite`;
- `test_business_concurrency::test_blind_unknown_restarts_keep_all_candidates_blocked`;
- `test_goal5_business_recovery::test_goal5_i2_try_again_keeps_unknown_recovery_read_only`.

## Исходные failures и повторная проверка

Из 24 baseline failures:

- 21 Google Workspace MCP subprocess test: runner задавал `PYTHONPATH=source`,
  из-за чего guard entrypoint не переносил project root на первое место и
  локальный `config` затенял `config.llm_models`. Все 21 тест повторены на
  неизменённом baseline без `PYTHONPATH`: PASS. Candidate проверен без этого
  runner override. Production/MCP код для этого не менялся.
- Один logging test: строка `private` в `/private/tmp` входила в
  `LogRecord.pathname` и нарушала blanket assertion по всему `repr(record)`.
  Исходный module в neutral-path worktree: 2 PASS. Сообщение лога оставалось
  generic; тест и sanitizer не менялись.
- Два `test_five_scoped_booking_cancel_cycles_keep_four_progressing_while_one_is_held`
  (`WEBSITE_WIDGET`, `VOICE`): вместо одного held `DISPATCHING` обнаружен
  `UNKNOWN`, при восьми `SUCCEEDED`. В candidate повторился только VOICE.

Независимый reviewer проверил неизменённый путь: test barrier удерживает provider
до завершения остальных четырёх scopes, dispatch ограничен 10 секундами, timeout
сохраняет `UNKNOWN` без blind retry. Наблюдение совместимо с превышением этого
лимита в локальном Docker runtime; точная длительность удержания отдельно не
измерялась, поэтому это вывод по коду и результатам, а не доказанный stage timing.
Business runtime, repository, registration и сам cycle test byte-identical
base/candidate. Нельзя считать полный suite зелёным по результату одного запуска.

Последовательный повтор всего cycle module без конкурирующих pytest на fresh
PostgreSQL16 DB дал одинаковый результат: baseline и candidate — WEBSITE_WIDGET
PASS, VOICE FAILED с тем же UNKNOWN вместо DISPATCHING. Исходный тест, assertions
и timeout не менялись. Это сохраняющееся ограничение локальной функциональной
проверки, а не новый P1a failure; оно не объявляется исправленным или expected gap.

## Среда и воспроизводимость

Python 3.12.12, pytest 9.0.2, pytest-asyncio 1.3.0, FastAPI 0.135.1,
Pydantic 2.12.5, SQLAlchemy 2.0.48, asyncpg 0.31.0. Pinned dependencies:
OpenAI 2.26.0, MCP 1.26.0, anyio 4.12.1, HTTPX 0.28.1, httpcore 1.0.9.

Полный набор собран один раз для каждого source, распределён целыми test modules
по четырём частям без пропусков. Каждая часть использует отдельную одноразовую
PostgreSQL16 DB в Docker Desktop, loopback port и tmpfs. Образ соответствует P0a:
`postgres:16-alpine@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685`.
Parent credentials/proxies/.env не передаются; используются synthetic credentials.
Контейнеры и временные credential inputs удаляются после завершения.

P0a работает в пустой временной папке с исходными network/process/.env guards.
Репозиторий metrics должен быть чистым и находиться на указанном PR1 SHA;
AI checkout тоже должен быть чистым и закоммиченным:

```bash
python scripts/run_p0a_ai.py \
  --metrics-root /path/to/chota-metrics \
  --report /tmp/p1a-ai-p0a.json
```

Локальные JUnit, source/node inventories и retry evidence сохранены в
`/private/tmp/chota-p1a-full-shards/`; guarded ASGI report для application commit —
`/private/tmp/chota-p1a-d8f847c-p0a.json`. Финальный PR HEAD проверяется отдельно
после documentation-only commit. GitHub Actions capacity по условию задачи
исчерпана; удалённый CI и deployment не подтверждаются.

## Независимое ревью

Fresh Sol 6.1 xhigh reviewer прочитал весь security-sensitive diff после исправлений.
Открытых P1a security findings не осталось. Закрыты два P2: утечка registry через
Pydantic after-validator и возможность использовать старый P0a output после
раннего worker failure. Supplemental verdict не объявляет весь SEC-002 или
общую Business functional acceptance завершёнными. Единственный candidate FAILED
уже воспроизводился на base; новых candidate-specific failures не найдено.

## Намеренно не включено

`INTERNAL_OWNERSHIP_AUTH_REQUIRED=false` сохраняет только существующий
аутентифицированный legacy path. Новые узкие credentials изолированы при их
настройке; удаление legacy ownership admission требует последующего strict flag.
Любой caller, который ещё имеет management token, сохраняет management права до
отдельной выдачи/удаления/ротации credentials. Generic owner header trust остаётся
P1c. Distributed reload/revocation, CNI isolation и staging здесь не проверялись.

Нет изменений GitOps, Kubernetes, Secrets, SEC-001/003, OTP, customer.find/ref,
Business RS256/mTLS, Goal5 permits/revisions/operation ownership и UNKNOWN retry
policy. Caller migration P1b/P1c не начата. API suppliers/consumers за пределами
AI repo не изменялись.

REPORT sections C/D/G и SECURITY_AUDIT сверены с current main; изменения main
после отчёта не меняют затронутый internal auth path. Запрошенный файл
`MAIN_CHOTA_SECURITY_FOLLOWUP_CONTEXT.md` не найден в workspace/attachments;
он не выдаётся за прочитанный источник.

## Изменённые файлы

| Файл | Назначение |
| --- | --- |
| `.env.example` | Optional registry и выключенный strict flag |
| `README.md` | Internal auth contract и ссылки |
| `api/dependencies.py` | Authentication, scope-before-DB, management header hardening |
| `api/internal/agents.py` | Разделение route dependencies |
| `core/config.py` | Safe registry/startup validation |
| `core/managed_security.py` | Purpose-specific values/parser, MCP secret exclusion |
| `docs/internal-ownership-auth.md` | Contract, compatibility, rotation, prerequisites P1b |
| `docs/p1a-validation.md` | Результаты и границы проверки |
| `scripts/run_p0a_ai.py` | Guarded pinned P0a runner |
| `tests/p0a_receiver_contract.py` | Compatible synthetic provisioning, исходные assertions |
| `tests/test_chat_auth.py` | Безопасный тип startup error |
| `tests/test_internal_auth_startup_privacy.py` | Startup traceback privacy regression |
| `tests/test_internal_purpose_auth.py` | Purpose/scope/version/revocation/compatibility coverage |
