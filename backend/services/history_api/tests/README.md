# History API Tests

Полный набор интеграционных тестов для History API, созданный на основе OpenAPI спецификации.

## Структура тестов

```
tests/
├── integration/
│   ├── __init__.py
│   ├── conftest.py                    # Фикстуры и конфигурация
│   ├── test_telemetry.py             # Тесты телеметрии датчиков
│   ├── test_security_events.py       # Тесты событий безопасности
│   ├── test_command_history.py       # Тесты истории команд
│   ├── test_photos.py                # Тесты управления фотографиями
│   ├── test_analytics.py             # Тесты аналитики и агрегатов
│   └── test_unauthorized.py          # Тесты авторизации
└── unit/
    ├── __init__.py
    ├── conftest.py                    # Мок БД (AsyncMock)
    ├── test_aggregation.py            # Тесты AggregationService
    └── test_retention.py              # Тесты RetentionService
```

## Запуск тестов

Тесты запускаются исключительно внутри Docker-контейнеров.

### Полный прогон (unit + integration)

```bash
cd backend
docker compose --profile tests up --build history_api_tests
```

### Конкретный файл / тест

Образ `history_api_tests` (см. `services/history_api/Dockerfile.tests`) использует
`pytest` как entrypoint и по умолчанию прогоняет все тесты. Для точечного запуска
переопределите команду контейнера:

```bash
cd backend
docker compose --profile tests run --rm history_api_tests pytest tests/integration/test_telemetry.py -v
docker compose --profile tests run --rm history_api_tests \
  pytest tests/integration/test_telemetry.py::test_telemetry_batch_create -v
```

Аналогичные паттерны для других сервисов см. в `services/locker_api/tests/README.md`
и `services/user_api/tests/README.md`.

## Конфигурация

Тесты используют переменные окружения для конфигурации:

| Переменная | Значение по умолчанию | Описание |
|------------|----------------------|----------|
| `TEST_BASE_URL` | `http://localhost:8002` | URL History API |
| `DB_HOST` | `localhost` | Хост PostgreSQL |
| `DB_PORT` | `5432` | Порт PostgreSQL |
| `DB_USER` | `admin` | Пользователь БД |
| `DB_PASSWORD` | `password` | Пароль БД |
| `DB_NAME` | `scud_history` | Имя БД |
| `TEST_API_TIMEOUT` | `90` | Таймаут ожидания API (секунды) |

## Покрытие API

### 1. Telemetry API (`test_telemetry.py`)
- ✅ `POST /api/v1/telemetry` - batch-запись телеметрии
- ✅ `GET /api/v1/telemetry/list` - получение с фильтрами
- Фильтры: `locker_id`, `sensor_id`, `sensor_type_code`, `from`, `to`
- Пагинация: `limit`, `offset`
- Валидация: пустые массивы, лимиты (>1000), обязательные поля
- Типы значений: числовые, текстовые, метаданные

**Тесты (8):**
- `test_telemetry_batch_create` - batch-вставка
- `test_telemetry_list_with_filters` - все фильтры
- `test_telemetry_pagination` - пагинация
- `test_telemetry_batch_validation` - валидация
- `test_telemetry_text_value` - текстовые значения
- `test_telemetry_with_metadata` - метаданные

### 2. Security Events API (`test_security_events.py`)
- ✅ `POST /api/v1/events/security` - создание события
- ✅ `GET /api/v1/events/security` - список с фильтрами
- ✅ `PATCH /api/v1/events/security/{eventId}` - закрытие события
- Фильтры: `locker_id`, `event_type`, `severity`, `resolved`, `from`, `to`
- Типы событий: `threshold_violation`, `intrusion`, `offline`, `door_tamper`
- Уровни: `low`, `medium`, `high`, `critical`

**Тесты (8):**
- `test_security_event_lifecycle` - полный цикл (создание → закрытие)
- `test_security_events_list_filters` - все фильтры
- `test_resolve_event` - закрытие события
- `test_resolve_nonexistent_event` - 404 для несуществующего
- `test_security_event_validation` - валидация полей
- `test_security_event_pagination` - пагинация
- `test_security_event_with_violated_thresholds` - нарушенные пороги

### 3. Command History API (`test_command_history.py`)
- ✅ `POST /api/v1/commands/history` - запись истории
- ✅ `GET /api/v1/commands/history` - список с фильтрами
- Фильтры: `locker_id`, `user_id`, `command_type`, `status`, `from`, `to`
- Типы команд: `open`, `close`, `reboot`, `config`
- Статусы: `pending`, `sent`, `delivered`, `executed`, `failed`
- Денормализация: `user_phone`, `user_full_name`, `locker_inventory_number`

**Тесты (8):**
- `test_command_history_create` - создание записи
- `test_command_history_list_filters` - все фильтры
- `test_command_history_denormalized_data` - денормализованные данные
- `test_duplicate_command_conflict` - 409 для дубликатов
- `test_command_history_status_lifecycle` - все статусы
- `test_command_history_with_parameters` - параметры команд
- `test_command_history_pagination` - пагинация
- `test_command_history_validation` - валидация

### 4. Media API (`test_media_api.py`, `test_media_handlers.py`, `test_media_repository.py`)
- ✅ `POST /api/v1/media` - загрузка медиа (multipart/form-data, BYTEA)
- ✅ `GET /api/v1/media` - список медиа с фильтрами
- ✅ `GET /api/v1/media/{id}` - метаданные записи
- ✅ `DELETE /api/v1/media/{id}` - удаление (soft-delete)
- ✅ `GET /api/v1/events/security/{id}/media` - медиа для события безопасности
- Форматы: JPEG, PNG
- Фильтры: `locker_id`, `from`, `to`

### 5. Analytics API (`test_analytics.py`)
- ✅ `GET /api/v1/analytics/sensors` - агрегаты датчиков
- ✅ `GET /api/v1/analytics/lockers/{lockerId}/summary` - сводка по шкафу
- Типы агрегации: `hourly`, `daily`, `monthly`
- Метрики: `avg`, `min`, `max`, `sum`, `stddev`
- Фильтры: `sensor_id` (обязательный), `from`, `to`

**Тесты (13):**
- `test_sensor_analytics` - базовые агрегаты
- `test_sensor_analytics_with_aggregation_type` - типы агрегации
- `test_sensor_analytics_with_metrics` - все метрики
- `test_sensor_analytics_date_range` - фильтрация по датам
- `test_sensor_analytics_validation` - валидация параметров
- `test_locker_summary` - сводка по шкафу
- `test_locker_summary_with_aggregation_type` - типы агрегации для сводки
- `test_locker_summary_date_range` - период для сводки
- `test_locker_summary_nonexistent_locker` - 404 для несуществующего
- `test_locker_summary_multiple_sensors` - несколько датчиков
- `test_analytics_empty_results` - пустые результаты
- `test_analytics_sensor_aggregation_fields` - структура данных

### 6. Authorization (`test_unauthorized.py`)
- ✅ Проверка авторизации для всех эндпоинтов
- ✅ Заголовок `X-Remote-User` обязателен
- ✅ Валидация формата user_id

**Тесты (10):**
- `test_telemetry_endpoints_without_auth`
- `test_security_events_endpoints_without_auth`
- `test_command_history_endpoints_without_auth`
- `test_media_endpoints_without_auth`
- `test_analytics_endpoints_without_auth`
- `test_invalid_auth_header` - некорректный формат
- `test_empty_auth_header` - пустой заголовок
- `test_all_endpoints_require_auth` - массовая проверка
- `test_valid_auth_header` - валидная авторизация
- `test_numeric_user_id_formats` - различные форматы ID

### 7. AggregationService (`test_aggregation.py`)
- ✅ Жизненный цикл start/stop
- ✅ Вычисление границ периодов (hourly, daily, monthly)
- ✅ Выполнение SQL upsert для каждого типа агрегации
- ✅ Логирование количества обработанных строк
- ✅ Продолжение работы после ошибок БД

**Тесты (9):**
- `test_start_stop_lifecycle` — проверка _running флага
- `test_get_period_boundaries_hourly` — часовые границы
- `test_get_period_boundaries_daily` — дневные границы
- `test_get_period_boundaries_monthly` — месячные границы
- `test_aggregate_hourly_executes_query` — SQL с типом 'hourly'
- `test_aggregate_daily_executes_query` — SQL с типом 'daily'
- `test_aggregate_monthly_executes_query` — SQL с типом 'monthly'
- `test_run_aggregation_logs_count` — логирование результатов
- `test_error_handling_continues` — устойчивость к ошибкам

### 8. RetentionService (`test_retention.py`)
- ✅ Жизненный цикл start/stop
- ✅ Удаление старых партиций (DROP TABLE)
- ✅ Удаление строк из всех таблиц (security_events, command_history, locker_media, telemetry_aggregations)
- ✅ Создание будущих партиций
- ✅ Конфигурация retention_months

**Тесты (10):**
- `test_start_stop_lifecycle` — проверка _running флага
- `test_drop_old_partitions` — удаление устаревших партиций
- `test_drop_old_partitions_none_to_drop` — нет старых партиций
- `test_delete_old_security_events` — DELETE security_events
- `test_delete_old_command_history` — DELETE command_history
- `test_delete_old_media` — DELETE locker_media
- `test_create_future_partitions` — создание партиций на 3 месяца вперёд
- `test_cleanup_photo_files` — os.remove с обработкой ошибок
- `test_error_handling_continues` — устойчивость к ошибкам
- `test_retention_months_config` — расчёт даты отсечки

## Общая статистика

- **Всего тестов:** 87
- **Файлов тестов:** 8
- **Покрытие эндпоинтов:** 13 из 13 (100%)
- **Типы проверок:**
  - ✅ CRUD операции
  - ✅ Фильтрация и поиск
  - ✅ Пагинация (limit/offset)
  - ✅ Валидация входных данных
  - ✅ Обработка ошибок (400, 401, 404, 409, 413, 415)
  - ✅ Авторизация (X-Remote-User)
  - ✅ Денормализованные данные
  - ✅ Multipart/form-data (загрузка файлов)
  - ✅ Binary responses (скачивание файлов)

## Особенности реализации

### 1. Генерация уникальных ID
Все тесты используют timestamp-based UUID генерацию для избежания конфликтов:
```python
def generate_unique_locker_id():
    timestamp = str(int(time.time() * 1000000))
    return f"550e8400-e29b-41d4-{timestamp[-12:-8]}-{timestamp[-8:]}"
```

### 2. Фикстура APIClient
Автоматически добавляет `X-Remote-User` ко всем запросам:
```python
@pytest.fixture
def api_client(wait_for_api):
    class APIClient:
        def get(self, path, **kwargs): ...
        def post(self, path, **kwargs): ...
        # ... и т.д.
```

### 3. Ожидание готовности
Тесты автоматически ждут готовности БД и API (до 90 секунд):
- Проверка подключения к PostgreSQL
- Проверка наличия таблиц (миграции)
- Проверка доступности API endpoints

### 4. Тестовые изображения
Для тестов загрузки фото используются минимальные валидные JPEG/PNG:
```python
def create_test_image(format="jpeg", size_kb=100):
    # Создает минимальный валидный JPEG/PNG в памяти
```

## Соответствие OpenAPI спецификации

Все тесты полностью соответствуют спецификации:
- `backend/services/history_api/openapi/openapi.yaml`
- Проверены все параметры из `components/parameters.yaml`
- Протестированы все схемы из `schemas/*.yaml`
- Проверены все пути из `paths/*.yaml`
- Покрыты все коды ответов (200, 201, 204, 400, 401, 404, 409, 413, 415, 500)

## Расширение

Для добавления новых тестов:

1. Добавьте тест-функции в соответствующий файл
2. Используйте `api_client` фикстуру для запросов
3. Генерируйте уникальные ID через helper-функции
4. Проверяйте все коды ответов согласно OpenAPI spec
5. Добавьте описание в этот README

## CI/CD Integration

Тесты готовы к интеграции в CI/CD (всё через Docker, без локального `pytest`):

```yaml
# .github/workflows/test.yml
- name: Run History API Integration Tests
  run: |
    cd backend
    docker compose --profile tests up --build --abort-on-container-exit history_api_tests
```

## Зависимости

Все необходимые зависимости уже в `requirements.txt`:
- `pytest==7.2.1` - тестовый фреймворк
- `requests==2.32.3` - HTTP клиент
- `psycopg2==2.9.5` - PostgreSQL драйвер
