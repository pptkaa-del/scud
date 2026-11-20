# Reports API

Подсистема асинхронной генерации отчётов системы ScudDocs (СКУД).

## Назначение

Reports API строит выгрузки по данным СКУД (события открытия шкафов и т.п.),
выполняя тяжёлую генерацию файлов в фоне, чтобы не блокировать HTTP-запрос
клиента. Готовый файл скачивается по ссылке, действительной ограниченное время.

## Технологический стек

- Python 3.11+
- Tornado (асинхронный веб-фреймворк)
- PostgreSQL 15 (БД `scud_reports`) с асинхронной обёрткой `shared.database`
- Docker

## Асинхронный поток

1. **Постановка задачи.** `POST /api/v1/reports` ставит задачу в очередь
   (status `pending`) и сразу возвращает `202 Accepted` с её идентификатором.
   Файл ещё не сгенерирован.
2. **Опрос статуса.** Клиент опрашивает `GET /api/v1/report/{id}` (или смотрит
   список `GET /api/v1/reports`). Задача проходит `pending` → `running` →
   `ready` (либо `failed`).
3. **Скачивание.** При статусе `ready` файл доступен через
   `GET /api/v1/report/{id}/download`. Ссылка действительна
   `REPORT_LINK_TTL_SECONDS` (по умолчанию 24 ч).
4. **Истечение.** После истечения TTL фоновый воркер удаляет файл с диска и
   помечает задачу `expired`; скачивание начинает отдавать `410 Gone`. То же
   происходит при `DELETE /api/v1/report/{id}` (soft-expire + удаление файла).

Фоновый воркер (`app/services/worker.py`) захватывает по одной `pending`-задаче
(`FOR UPDATE SKIP LOCKED`), генерирует файл, проставляет `expires_at`. При сбое
задача повторяется до `REPORT_MAX_ATTEMPTS` раз, затем помечается `failed`.
PII-строки отчёта не логируются.

### Имя отчёта (`name`)

Задача может иметь опциональное человекочитаемое имя `name` (строка, до 255
символов). Оно задаётся при создании (`POST /api/v1/reports`, поле `name` —
необязательное; обрезается по краям, пустое после обрезки или длиннее 255
символов → `400`) и в дальнейшем может быть установлено или изменено через
`PATCH /api/v1/report/{id}` с телом `{"name": "..."}` (имя обязательно,
непустое после обрезки, ≤ 255 символов). Эндпоинт возвращает полное
обновлённое состояние задачи (`200`). Поле `name` присутствует в ответах
`ReportJobCreated` и `ReportJob` (nullable).

## API Endpoints

Все маршруты доступны за Apache gateway и требуют роли OPERATOR или выше
(проверяется в ACM). Ошибки — в формате RFC 7807 (`application/problem+json`).

| Метод | Путь | Описание | Коды ответа |
|-------|------|----------|-------------|
| `POST` | `/api/v1/reports` | Поставить задачу в очередь | `202`, `400`, `401`, `403` |
| `GET` | `/api/v1/reports` | Постраничный список задач | `200`, `401`, `403` |
| `GET` | `/api/v1/report/{id}` | Получить задачу (poll) | `200`, `404` |
| `PATCH` | `/api/v1/report/{id}` | Переименовать отчёт (изменить `name`) | `200`, `400`, `404` |
| `GET` | `/api/v1/report/{id}/download` | Скачать готовый файл | `200`, `409`, `410`, `404` |
| `DELETE` | `/api/v1/report/{id}` | Soft-expire задачи + удаление файла | `204`, `404` |

Семантика скачивания: `409` — отчёт ещё не готов (`pending`/`running`);
`410` — отчёт `failed`/`expired` или файл недоступен на диске; `404` — задача
не найдена.

### Скоупинг по владельцу (защита от IDOR)

Отчёты содержат ПДн (ФИО + история перемещений), поэтому доступ скоупится по
владельцу (`requested_by` — id пользователя, поставившего задачу):

- `GET /api/v1/reports` (список) возвращает только свои задачи; `total`
  считается по тем же отчётам и не раскрывает количество чужих.
- `GET`/`download`/`PATCH`/`DELETE` `/api/v1/report/{id}` для чужой задачи
  возвращают `404` (не `403`) — существование чужого ресурса не раскрывается;
  `DELETE` чужой задачи не помечает её expired.

Роль `administrator` (заголовок `X-Remote-Role`, проставляется gateway после
JWT-валидации) снимает скоуп: администратор видит и трогает отчёты всех
пользователей. Фильтр владельца уходит в SQL (`WHERE requested_by = …`), а не
проверяется постфактум.

### Фильтрация (GET /api/v1/reports)

| Параметр | Тип | Описание |
|----------|-----|----------|
| `status` | string | Фильтр по статусу (`pending`/`running`/`ready`/`failed`/`expired`) |
| `report_type` | string | Фильтр по типу отчёта |
| `q` | string | Подстрочный поиск по имени отчёта (без учёта регистра, экранируется, max 255) |
| `limit` | int | Лимит (default `50`, max `500`) |
| `offset` | int | Смещение (default `0`) |

Ответ — `PaginatedResponse` (`items`, `total`, `limit`, `offset`).

## Типы отчётов

| `report_type` | Название | Формат | Колонки |
|---------------|----------|--------|---------|
| `locker_open` | Отчёт по открытию | `.xlsx` | № п/п, ID пользователя, Инв. номер шкафа, ФИО, Время открытия |

`locker_open` принимает `params = {date_from, date_to}` (ISO-8601 datetime,
`date_from < date_to`); данные тянутся из history_api через обобщённый
внутренний эндпоинт `GET /internal/api/v1/commands/history` с фильтрами
`command_type=open`, `status=executed`, окном `executed_from`/`executed_to`
и сортировкой `order=executed_at_asc`.

Файл выгружается как оформленный `.xlsx`: строка-заголовок с периодом отчёта,
цветная жирная шапка колонок, тонкие рамки ячеек, чередующаяся заливка строк
(«зебра»), авто-ширина колонок, закреплённая шапка и автофильтр.

## Как добавить новый тип отчёта

Точка расширения — реестр генераторов; БД, воркер и хендлеры менять **не нужно**.

1. Реализовать класс по протоколу `ReportGenerator`
   (`app/reports/base.py`) в `app/reports/generators/`: атрибуты
   `report_type` и `output_format`, методы `validate_params(raw)` (валидирует
   сырой dict из запроса) и `async generate(params, out_path)` (пишет файл,
   возвращает `GenerationResult` с числом строк).
2. Зарегистрировать его в `build_registry` (`app/reports/registry.py`) —
   достаточно добавить экземпляр в список `generators`.
3. Если нужен новый формат файла — добавить член в `ReportFormat`
   (`app/reports/formats.py`) и соответствующий writer-backend в `shared`.
4. Добавить `report_type` (и при необходимости формат) в OpenAPI-спецификацию
   `openapi/openapi.yaml`.
5. Покрыть unit- и integration-тестами.

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `DB_HOST` | `localhost` | Хост PostgreSQL |
| `DB_PORT` | `5432` | Порт PostgreSQL |
| `DB_USER` | `admin` | Пользователь БД |
| `DB_PASSWORD` | `postgres` | Пароль БД (или Docker secret `postgres_reports_password`) |
| `DB_NAME` | `scud_reports` | Имя базы данных |
| `APP_PORT` | `8006` | Порт HTTP-сервера |
| `REPORTS_STORAGE_PATH` | `/data/reports` | Каталог хранения сгенерированных файлов (volume) |
| `REPORT_LINK_TTL_SECONDS` | `86400` | TTL ссылки на готовый файл (сек), по умолчанию 24 ч |
| `REPORT_WORKER_INTERVAL_S` | `5` | Интервал опроса очереди воркером (сек) |
| `REPORT_MAX_ATTEMPTS` | `3` | Число попыток генерации до status=failed |
| `HISTORY_API_URL` | `http://history_api:8002` | Базовый URL history_api |

## Тестирование

Все тесты запускаются исключительно в Docker-контейнере:

```bash
cd backend
docker compose --profile tests up --build reports_api_tests
```

## Документация

OpenAPI спецификация: [`openapi/openapi.yaml`](openapi/openapi.yaml).

```bash
# ReDoc
docker compose -f docker-compose.docs.yaml up
# http://localhost:8086
```
