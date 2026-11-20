# Руководство разработчика Locker API

Комплексное руководство по разработке и поддержке микросервиса Locker API для системы ScudDocs.

## Содержание

- [Введение](#введение)
- [Архитектура](#архитектура)
- [Структура проекта](#структура-проекта)
- [Как добавить новую сущность](#как-добавить-новую-сущность)
- [Как добавить новый эндпоинт](#как-добавить-новый-эндпоинт)
- [Работа с базой данных](#работа-с-базой-данных)
- [MQTT протокол (Device Gateway)](#mqtt-протокол-device-gateway)
- [Фоновые сервисы](#фоновые-сервисы)
- [Типы датчиков и справочники](#типы-датчиков-и-справочники)
- [Аутентификация](#аутентификация)
- [Обработка ошибок](#обработка-ошибок)
- [Тестирование](#тестирование)
- [OpenAPI документация](#openapi-документация)
- [Переменные окружения](#переменные-окружения)
- [Запуск и отладка](#запуск-и-отладка)
- [Best Practices](#best-practices)

---

## Введение

**Locker API** — асинхронный REST API + MQTT-микросервис для управления телекоммуникационными шкафами, датчиками и командами в системе ScudDocs.

### Технологический стек

- **Python 3.11+** - язык программирования
- **Tornado** - асинхронный веб-фреймворк (REST)
- **PostgreSQL** - база данных
- **psycopg2** - драйвер PostgreSQL с общей асинхронной оберткой (shared.database)
- **pytest** - фреймворк для тестирования
- **dacite** - конвертация dict → dataclass
- **Docker** - контейнеризация

### Основные возможности

- ✅ **Control Gateway (REST API)** - управление шкафами, датчиками, командами, пороговыми значениями
- ✅ **Device Gateway (MQTT)** - подключение релейных плат шкафов через брокер Mosquitto
- ✅ **Command Queue** - фоновая очередь команд для доставки на устройства
- ✅ **State Monitor** - фоновый мониторинг пороговых значений датчиков
- ✅ **Справочник типов датчиков** - гибкая система типов (таблица, не ENUM)
- ✅ **Денормализация данных** - для производительности JOIN запросов
- ✅ **Двойная аутентификация** - X-Remote-User для управления, брокерский логин/пароль (`relay-{id}`) для реле

---

## Архитектура

Сервис построен по **двухшлюзовой архитектуре** с фоновыми процессами:

```
┌────────────────────────────────────────────────────────────┐
│                     Control Gateway (REST)                 │
│  LockerHandler, SensorHandler, CommandHandler, etc.        │
│  Аутентификация: X-Remote-User (trust boundary)            │
└────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────┐
│              Business Logic (Repositories)                 │
│  LockerRepository, SensorRepository, CommandRepository     │
│  Денормализация: JOIN с sensor_types, contractor_name      │
└────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────┐
│           Data Layer (shared.database.AsyncPostgres)       │
│       Connection pooling, Retry, Transactions              │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│                   Device Gateway (MQTT)                     │
│              RelayIngest + MqttTransport (app/mqtt/)        │
│    Аутентификация: per-device логин/пароль в брокере        │
└────────────────────────────────────────────────────────────┘
                 ↕ (через брокер Mosquitto, топики stat/rep/photos/com)
┌────────────────────────────────────────────────────────────┐
│                  Физические устройства (Шкафы)             │
│   • Отправка телеметрии/статуса (топик stat)                │
│   • Получение команд (топик com/{relay_id})                 │
│   • Отправка результатов выполнения (топик rep)             │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│                  Background Services                       │
│  • CommandQueue (30s) - обработка очереди команд           │
│  • StateMonitor (60s) - проверка пороговых значений        │
└────────────────────────────────────────────────────────────┘
```

### Принципы

1. **Separation of Concerns** - Control Gateway и Device Gateway разделены
2. **Trust Boundary** - Control Gateway доверяет прокинутому user-context
3. **Event-Driven** - MQTT (брокер Mosquitto) для real-time взаимодействия с устройствами
4. **Background Processing** - фоновые задачи для автоматизации
5. **Denormalization for Performance** - денормализация для ускорения JOIN запросов

---

## Структура проекта

```
backend/locker_api/
├── app/                                  # Основное приложение
│   ├── __init__.py
│   ├── main.py                          # Точка входа, создание приложения
│   │
│   ├── handlers/                        # HTTP обработчики (Control Gateway)
│   │   ├── __init__.py
│   │   ├── base.py                      # Базовый класс для всех хендлеров
│   │   ├── locker.py                    # Хендлеры для /api/v1/lockers
│   │   ├── sensor.py                    # Хендлеры для /api/v1/locker/{id}/sensors
│   │   ├── sensor_type.py               # Хендлеры для /api/v1/sensor-types
│   │   ├── threshold.py                 # Хендлеры для пороговых значений
│   │   └── command.py                   # Хендлеры для команд
│   │
│   ├── mqtt/                             # MQTT обработчики (Device Gateway)
│   │   ├── __init__.py
│   │   ├── ingest.py                     # Демультиплексор входящих stat/rep/photos
│   │   ├── transport.py                  # MqttTransport (DeviceTransport для relay-плат)
│   │   ├── relay_protocol.py             # Перечисления/константы протокола
│   │   ├── relay_codec.py                # Кодек бинарных кадров
│   │   └── provisioning.py               # Провижининг credentials/ACL платы в брокере
│   │
│   ├── services/                        # Фоновые сервисы
│   │   ├── __init__.py
│   │   ├── command_queue.py             # Очередь команд (30s интервал)
│   │   └── state_monitor.py             # Мониторинг состояния (60s интервал)
│   │
│   └── locker_data/                     # Слой работы с данными
│       ├── __init__.py
│       └── db/                          # Модуль базы данных
│           ├── __init__.py
│           │
│           ├── models/                  # Модели данных (dataclasses)
│           │   ├── __init__.py
│           │   ├── locker.py            # Locker, LockerState
│           │   ├── sensor.py            # Sensor (с денормализацией)
│           │   ├── sensor_type.py       # SensorType
│           │   ├── command.py           # Command, CommandType, CommandStatus
│           │   └── threshold.py         # Threshold
│           │
│           ├── repositories/            # Репозитории (data access layer)
│           │   ├── __init__.py
│           │   ├── locker.py            # LockerRepository
│           │   ├── sensor.py            # SensorRepository
│           │   ├── sensor_type.py       # SensorTypeRepository
│           │   ├── command.py           # CommandRepository
│           │   └── threshold.py         # ThresholdRepository
│           │
│           └── migrations/              # SQL миграции
│               └── 001_init.sql         # Начальная схема БД
│
├── tests/                               # Тесты
│   ├── __init__.py
│   │
│   ├── unit/                           # Unit-тесты с моками
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   ├── test_locker_api.py
│   │   ├── test_sensor_api.py
│   │   └── test_command_api.py
│   │
│   └── integration/                    # Интеграционные тесты
│       ├── __init__.py
│       ├── conftest.py
│       └── test_locker_integration.py
│
├── openapi/                            # OpenAPI спецификация
│   ├── openapi.yaml                    # Главный файл спецификации
│   ├── components/
│   │   ├── parameters.yaml
│   │   ├── responses.yaml
│   │   └── securitySchemes.yaml
│   ├── paths/
│   │   ├── lockers.yaml
│   │   ├── sensors.yaml
│   │   ├── sensor_types.yaml
│   │   ├── thresholds.yaml
│   │   └── commands.yaml
│   └── schemas/
│       ├── locker.yaml
│       ├── sensor.yaml
│       ├── sensor_type.yaml
│       ├── threshold.yaml
│       ├── command.yaml
│       ├── common_fields.yaml
│       └── error.yaml
│
├── Dockerfile                          # Docker образ
├── requirements.txt                    # Python зависимости
├── README.md                           # Краткое описание
└── DEVELOPER_GUIDE.md                  # Это руководство
```

### Описание ключевых модулей

#### `app/main.py`
- Точка входа приложения
- Создание Tornado Application с роутами
- Инициализация подключения к БД (shared.database.AsyncPostgres)
- Запуск фоновых сервисов (CommandQueue, StateMonitor)
- Graceful shutdown при Ctrl+C

#### `app/handlers/base.py`
- `BaseHandler` - базовый класс для Control Gateway
- Авторизация через `X-Remote-User` header (trust boundary)
- Утилиты для JSON: `write_json()`, `get_json_body()`

#### `app/mqtt/ingest.py`
- `RelayIngest` - демультиплексор входящих MQTT-сообщений релейных плат (Device Gateway)
- Разбор топиков `stat` (телеметрия/статус), `rep` (результат команды), `photos` (кадры)
- Обновление connectivity/state шкафа, запись показаний датчиков
- Интеграция с CommandQueue/CommandResultApplier; полный протокол — `openapi/openapi_relay.md`

#### `app/services/command_queue.py`
- `CommandQueue` - очередь команд для устройств
- Регистрация транспортов устройств (`DeviceTransport`, реализация — `MqttTransport`)
- Фоновый процессор (интервал см. `COMMAND_QUEUE_PROCESSOR_INTERVAL_S`)
- Отправка pending команд на онлайн устройства через MQTT (топик `com/{relay_id}`)

#### `app/services/state_monitor.py`
- `StateMonitor` - мониторинг пороговых значений датчиков
- Фоновый процессор (60s интервал)
- Проверка значений датчиков на соответствие порогам
- Перевод шкафа в состояние ALARM при нарушениях
- При нарушении шлёт уведомление через `NotificationClient` → `notification_api`
- При нарушении публикует security event в `history_api` через продьюсер,
  выбираемый по `sensor_type_code` (см. ниже «History Producers»)

#### `app/services/history_producers/`
- `BaseHistoryProducer` — общая инфраструктура: enqueue события в
  `outbound_history_events` (атомарно с бизнес-операцией).
- `TelemetryProducer` (channel `telemetry`) — батчи показаний датчиков.
- `CommandHistoryProducer` (channel `command_history`) — переходы команд
  pending → sent → executed/failed (идемпотентно по `command_id`).
- `OfflineEventProducer`, `ThresholdViolationProducer`, `IntrusionProducer`,
  `DoorTamperProducer` — типизированные security events. Маршрутизация по
  `sensor_type_code` — `producer_for_sensor_type()`.
- `MediaProducer` (channel `media`) — медиа-файлы шкафов (фото/видео);
  payload содержит байты файла и метаданные; доставляется в history_api
  без задержки, к событию привязывается по `security_event_uid`.

#### `app/services/outbound_dispatcher.py`
- `OutboundDispatcher` — фоновый воркер доставки событий из
  `outbound_history_events` в `history_api`.
- Опрос с poll-интервалом 2с, batch=100, exp.backoff на ошибках, ретеншен
  доставленных через `OUTBOUND_RETENTION_DAYS` (default 7).
- Мульти-формат: JSON для большинства каналов; канал `media` отправляет
  JSON с base64-байтами в `POST /internal/api/v1/media`.
- Продьюсеры могут отложить доставку (`delay_seconds` в `enqueue` сдвигает
  `next_attempt_at`); `MediaProducer` ставит кадры без задержки.
- Гарантия at-least-once: history_api идемпотентен (см. upsert command_history,
  партиционированная telemetry).

#### Relay-фото — поток данных

Устройство отправляет фото кадрами в топик `photos` (каждый кадр несёт
`device_id`, `event_id`, `photo_id`, `photos_cnt` и тело — самостоятельный
JPEG). Кадры не склеиваются и не буферизуются в памяти:

```
device                          RelayIngest._handle_photo
  │── photos (photo_id=k) ─────► validate_frame (SOI, 1..16)
  │                               лок (шкаф, relay, event_id):
  │                               │  list_for_event → classify_frame
  │                               │  new_series/reset → rate-limit, supersede, create
  │                               │    (проиграл create или сбой → токен назад)
  │                               ├─ duplicate → _ensure_intrusion, конец
  │                               ├─ _ensure_intrusion → enqueue_intrusion (захват + outbox)
  │                               ├─ MediaProducer.publish (кадр, security_event_uid)
  │                               └─ record_frame (sha256 кадра в frame_sha)
```

Реестр серий — таблица `relay_photo_series` (`RelayPhotoSeriesRepository`),
чистая логика — `app/mqtt/photo_series.py`. Корректность при параллельных
кадрах держится на БД: частичный уникальный индекс
`ux_relay_photo_series_current` (одна текущая серия на шкаф+relay+event_id),
`create … ON CONFLICT DO NOTHING` с перечитыванием победителя и условные
UPDATE. Внутри процесса классификация и создание серии ещё и сериализованы
`asyncio.Lock` на (шкаф, relay, `event_id`): параллельный кадр новой серии
видит созданную строку и не тратит токен rate-limit, поэтому при последнем
токене второй кадр не отбрасывается. Кадр, проигравший `create` (гонка между
процессами), возвращает токен и переклассифицируется как `new_frame` (или
`duplicate`) серии победителя; при исключении в `supersede`/`create` токен
тоже возвращается.
Intrusion ставит `enqueue_intrusion` одной командой `WITH claim AS (UPDATE …
SET intrusion_enqueued_at … WHERE intrusion_enqueued_at IS NULL) INSERT INTO
outbound_history_events …`: конкурент ждёт блокировку строки серии и уже не
проходит условие, а сбой не оставляет отметку без события. uid события и кадров — `uuid5` от `series_uid`, поэтому редоставка
кадра или повтор после рестарта дедуплицируется в history_api.

history_api привязывает кадр к событию по `security_event_uid` в любом порядке
доставки: если кадр пришёл раньше события, он сохраняется без привязки и
дотягивается при записи события. Окно ±30 с остаётся только для legacy-медиа.

`run_photo_maintenance` (фоновый цикл в `main.py`) дозаэмитит intrusion для
серий, где он не встал в outbox, и удаляет серии старше
`RELAY_PHOTO_SERIES_RETENTION_DAYS`.

Полный формат кадра `photos` (и остальных топиков `stat`/`rep`/`com`) —
`openapi/openapi_relay.md`.

#### `app/locker_data/db/repositories/`
- Паттерн Repository для изоляции SQL
- **Денормализация**: JOIN с sensor_types в SensorRepository
- Преобразование row → dataclass через dacite

---

## Как добавить новую сущность

Пошаговая инструкция для добавления новой сущности (например, `SensorLog` - лог значений датчика).

### Шаг 1: Создать миграцию БД

Создайте файл `app/locker_data/db/migrations/002_add_sensor_logs.sql`:

```sql
-- Таблица логов значений датчиков
CREATE TABLE sensor_logs (
    id BIGSERIAL PRIMARY KEY,
    sensor_id UUID NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    value DOUBLE PRECISION,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB DEFAULT '{}'
);

-- Индекс для быстрого поиска логов по датчику
CREATE INDEX idx_sensor_logs_sensor_recorded ON sensor_logs(sensor_id, recorded_at DESC);

-- Индекс для фильтрации по времени
CREATE INDEX idx_sensor_logs_recorded ON sensor_logs(recorded_at DESC);
```

Примените миграцию:
```bash
psql -h localhost -U admin -d scud_lockers -f app/locker_data/db/migrations/002_add_sensor_logs.sql
```

### Шаг 2: Создать модель данных

Создайте файл `app/locker_data/db/models/sensor_log.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(slots=True)
class SensorLog:
    """
    Лог значения датчика.

    Attributes:
        id: ID записи
        sensor_id: UUID датчика
        value: Записанное значение
        recorded_at: Время записи
        metadata: Дополнительные метаданные
    """
    id: int
    sensor_id: str  # UUID
    value: Optional[float]
    recorded_at: datetime
    metadata: dict
```

### Шаг 3: Создать репозиторий

Создайте файл `app/locker_data/db/repositories/sensor_log.py`:

```python
from __future__ import annotations

from typing import Optional
from datetime import datetime
from dacite import from_dict

from shared.database import AsyncPostgres
from ..models.sensor_log import SensorLog


class SensorLogRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(
        self,
        sensor_id: str,
        value: Optional[float],
    ) -> SensorLog:
        """Создать запись лога"""
        row = await self._db.fetch_one(
            """
            INSERT INTO sensor_logs (sensor_id, value)
            VALUES (%s, %s)
            RETURNING *
            """,
            (sensor_id, value),
            read_only=False,
        )
        return from_dict(SensorLog, row)

    async def get_by_sensor(
        self,
        sensor_id: str,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[SensorLog]:
        """Получить логи датчика с фильтрацией по времени"""
        query = """
            SELECT *
            FROM sensor_logs
            WHERE sensor_id = %s
        """
        params = [sensor_id]

        if from_date:
            query += " AND recorded_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND recorded_at <= %s"
            params.append(to_date)

        query += " ORDER BY recorded_at DESC LIMIT %s"
        params.append(limit)

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(SensorLog, row) for row in rows]
```

### Шаг 4: Создать хендлеры

Создайте файл `app/handlers/sensor_log.py`:

```python
from .base import BaseHandler
from ..locker_data.db.repositories.sensor_log import SensorLogRepository


class SensorLogListHandler(BaseHandler):
    """GET /locker/{locker_id}/sensors/{sensor_id}/logs"""

    async def get(self, locker_id: str, sensor_id: str):
        """Получение логов датчика"""
        limit = int(self.get_query_argument("limit", default="100"))

        # Валидация
        if limit < 1 or limit > 1000:
            self.write_json({"error": "limit must be between 1 and 1000"}, status_code=400)
            return

        repo = SensorLogRepository(self.db)
        logs = await repo.get_by_sensor(sensor_id, limit=limit)
        self.write_json(logs)
```

### Шаг 5: Зарегистрировать роуты

Отредактируйте `app/main.py`:

```python
from .handlers.sensor_log import SensorLogListHandler

def make_app(db: AsyncPostgres, command_queue: CommandQueue):
    return tornado.web.Application([
        # Existing routes...

        # Новый роут для логов датчиков:
        (r"/api/v1/locker/([^/]+)/sensors/([^/]+)/logs",
         SensorLogListHandler, dict(db=db)),
    ], debug=True)
```

### Шаг 6: Написать тесты

**Unit-тест** (`tests/unit/test_sensor_log_api.py`):

```python
import json
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app


class TestSensorLogApi(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()

        # Mock CommandQueue
        self.command_queue_mock = MagicMock()

        return make_app(self.db_mock, self.command_queue_mock)

    def test_get_sensor_logs_success(self):
        """Тест: успешное получение логов датчика"""
        self.db_mock.fetch_all.return_value = [
            {
                "id": 1,
                "sensor_id": "sensor-uuid",
                "value": 25.5,
                "recorded_at": datetime.now(),
                "metadata": {}
            }
        ]

        response = self.fetch(
            "/api/v1/locker/locker-uuid/sensors/sensor-uuid/logs",
            method="GET"
        )

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["value"], 25.5)
```

### Шаг 7: Обновить OpenAPI

Создайте `openapi/schemas/sensor_log.yaml`:

```yaml
SensorLog:
  type: object
  properties:
    id:
      type: integer
      format: int64
    sensor_id:
      type: string
      format: uuid
    value:
      type: number
      format: double
      nullable: true
    recorded_at:
      type: string
      format: date-time
    metadata:
      type: object
      additionalProperties: true
  required:
    - id
    - sensor_id
    - recorded_at
    - metadata
```

Добавьте описание эндпоинта в `openapi/paths/sensors.yaml`.

---

## Как добавить новый эндпоинт

Инструкция по добавлению нового эндпоинта к **существующей** сущности.

Например, добавим `POST /api/v1/locker/{id}/reboot` для быстрой отправки команды перезагрузки.

### Шаг 1: Добавить метод в хендлер

В `app/handlers/locker.py`:

```python
class LockerRebootHandler(BaseHandler):
    """POST /locker/{id}/reboot - отправка команды перезагрузки"""

    async def post(self, locker_id: str):
        """Создать команду перезагрузки для шкафа"""
        if not self.current_user_id:
            self.write_json({"error": "Unauthorized"}, status_code=401)
            return

        # Проверка существования шкафа
        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)

        if not locker:
            self.write_json({"error": "Locker not found"}, status_code=404)
            return

        # Создание команды
        command_repo = CommandRepository(self.db)
        command = await command_repo.create(
            locker_id=locker_id,
            command_type="reboot",
            parameters={},
            created_by=self.current_user_id,
        )

        self.write_json(command, status_code=201)
```

### Шаг 2: Зарегистрировать роут

В `app/main.py`:

```python
from .handlers.locker import LockerRebootHandler

def make_app(db: AsyncPostgres, command_queue: CommandQueue):
    return tornado.web.Application([
        # Existing routes...

        (r"/api/v1/locker/([^/]+)/reboot", LockerRebootHandler, dict(db=db)),
    ], debug=True)
```

### Шаг 3: Написать тесты

```python
def test_reboot_locker_success(self):
    """Тест: успешная отправка команды перезагрузки"""
    locker_row = {
        "id": "locker-uuid",
        "inventory_number": "СО №123",
        # ... остальные поля
    }

    command_row = {
        "id": "command-uuid",
        "locker_id": "locker-uuid",
        "type": "reboot",
        "status": "pending",
        # ... остальные поля
    }

    self.db_mock.fetch_one.side_effect = [locker_row, command_row]

    response = self.fetch(
        "/api/v1/locker/locker-uuid/reboot",
        method="POST",
        headers={"X-Remote-User": "1"}
    )

    self.assertEqual(response.code, 201)
    data = json.loads(response.body)
    self.assertEqual(data["type"], "reboot")
```

### Шаг 4: Обновить OpenAPI

В `openapi/paths/lockers.yaml`:

```yaml
/locker/{id}/reboot:
  post:
    summary: Reboot locker
    tags:
      - Lockers
    parameters:
      - $ref: '../components/parameters.yaml#/LockerId'
    responses:
      '201':
        description: Command created
        content:
          application/json:
            schema:
              $ref: '../schemas/command.yaml#/Command'
      '404':
        $ref: '../components/responses.yaml#/NotFound'
```

---

## Работа с базой данных

### Использование shared.database.AsyncPostgres

Locker API использует общий асинхронный коннектор из `backend/shared/database.py`.

#### Инициализация

```python
from shared.database import AsyncPostgres, PostgresConfig

config = PostgresConfig(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", 5432)),
    user=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", "postgres"),
    database=os.getenv("DB_NAME", "scud_lockers"),
)

db = AsyncPostgres(config)
```

#### Методы

**`fetch_one()` - получить одну строку**:

```python
row = await db.fetch_one(
    "SELECT * FROM lockers WHERE id = %s",
    (locker_id,)
)
# row: Optional[dict]
```

**`fetch_all()` - получить все строки**:

```python
rows = await db.fetch_all(
    "SELECT * FROM sensors WHERE locker_id = %s",
    (locker_id,)
)
# rows: list[dict]
```

**`execute()` - выполнить команду без возврата данных**:

```python
await db.execute(
    "UPDATE lockers SET state = %s WHERE id = %s",
    ('alarm', locker_id)
)
```

### Денормализация для производительности

#### Проблема N+1 запросов

Без денормализации при получении 100 датчиков пришлось бы делать:
- 1 запрос для получения списка датчиков
- 100 запросов для получения типа каждого датчика

**❌ Плохо** (N+1 запросы):

```python
# Модель БЕЗ денормализации
@dataclass
class Sensor:
    id: str
    sensor_type_id: int  # FK к sensor_types
    # Нет полей code, name, unit, value_type

# Repository БЕЗ денормализации
async def get_by_locker(self, locker_id: str) -> list[Sensor]:
    rows = await self._db.fetch_all(
        "SELECT * FROM sensors WHERE locker_id = %s",
        (locker_id,)
    )
    sensors = [from_dict(Sensor, row) for row in rows]

    # N+1 проблема: для каждого датчика нужен отдельный запрос типа!
    for sensor in sensors:
        sensor_type = await sensor_type_repo.get_by_id(sensor.sensor_type_id)
        # ...

    return sensors
```

#### Решение: денормализация через JOIN

**✅ Хорошо** (один JOIN запрос):

```python
# Модель С денормализацией
@dataclass
class Sensor:
    id: str
    sensor_type_id: int
    # Денормализованные поля из sensor_types:
    code: str
    name: str
    unit: Optional[str]
    value_type: str

# Repository С денормализацией
async def get_by_locker(self, locker_id: str) -> list[Sensor]:
    rows = await self._db.fetch_all(
        """
        SELECT s.id, s.locker_id, s.serial_number, s.sensor_type_id,
               s.current_value, s.created_at, s.updated_at, s.is_active, s.metadata,
               st.code, st.name, st.unit, st.value_type
        FROM sensors s
        JOIN sensor_types st ON s.sensor_type_id = st.id
        WHERE s.locker_id = %s
        ORDER BY s.sensor_type_id, s.serial_number
        """,
        (locker_id,)
    )
    return [from_dict(Sensor, row) for row in rows]
```

**Преимущества**:
- ✅ Один запрос вместо N+1
- ✅ Быстрее на больших выборках
- ✅ Меньше нагрузки на БД

**Недостатки**:
- ⚠️ Дублирование данных в модели
- ⚠️ Нужно обновлять денормализованные поля при изменении sensor_types

### Создание миграций

1. Создайте файл в `app/locker_data/db/migrations/`:
   ```
   00X_description.sql
   ```

2. Напишите SQL:
   ```sql
   -- Создание таблицы
   CREATE TABLE my_table (...);

   -- Создание индексов
   CREATE INDEX idx_my_table_field ON my_table(field);

   -- Добавление ENUM типа
   CREATE TYPE my_status AS ENUM ('pending', 'completed');
   ```

3. Примените миграцию:
   ```bash
   psql -h localhost -U admin -d scud_lockers \
        -f app/locker_data/db/migrations/00X_description.sql
   ```

### Best Practices для SQL

✅ **DO**:
- Используйте параметризованные запросы (`%s`)
- Используйте `RETURNING *` для INSERT/UPDATE
- Добавляйте индексы для полей в WHERE
- Используйте UUID для distributed systems
- Используйте JSONB для метаданных

❌ **DON'T**:
- Не используйте string interpolation (SQL injection!)
- Не забывайте про CASCADE для внешних ключей
- Не делайте N+1 запросов (используйте JOIN)

---

## MQTT протокол (Device Gateway)

### Обзор

Device Gateway обеспечивает real-time взаимодействие с физическими устройствами
(релейными платами шкафов) через MQTT-брокер Mosquitto — это единственный
транспорт устройств (WebSocket device-транспорт удалён, `LockerTransport.ws`
в enum сохранён только для чтения старых строк). Полная спецификация бинарного
протокола — `openapi/openapi_relay.md`; здесь только обзор для разработки.

**Брокер**: `mqtt:1883` в `backend_net` (Mosquitto 2.0.23, MQTT 3.1.1).

**Топики**:

| Топик            | Направление       | Назначение                       |
|------------------|-------------------|-----------------------------------|
| `stat`           | плата → backend   | Телеметрия и статус устройства    |
| `rep`            | плата → backend   | Результат выполнения команды      |
| `photos`         | плата → backend   | Кадры фото (серия по event_id)    |
| `com/{relay_id}` | backend → плата   | Команда конкретной плате          |

Кадры бинарные, поля — `uint32` little-endian. Демультиплексор входящих
сообщений — `RelayIngest` (`app/mqtt/ingest.py`); отправка команд — `MqttTransport`
(`app/mqtt/transport.py`), зарегистрированный в `CommandQueue` как `DeviceTransport`.

### Процесс подключения

```
Плата                                   Backend (RelayIngest)
  |                                        |
  |-------- MQTT connect (login/pass) ---->|  (проверяет брокер, не locker_api)
  |                                        |
  |-------- publish stat ----------------->|  телеметрия/статус
  |                                        |
  |<------- publish com/{relay_id} --------|  pending-команда (если есть)
  |                                        |
  |-------- publish rep ------------------>|  результат команды
```

### Сообщения от устройства

#### `stat` — телеметрия/статус

Плата периодически публикует `device_id`, версию протокола, состояние реле
(v1) либо `door`/`power`/`relay_en` (v2), `status` (`DeviceStatus`), `signal`,
`alarm`. `RelayIngest` обновляет `lockers.connectivity`, показания датчиков
(`battery`, `signal`, `door`, `power`, `tamper`) и `last_seen_at`.

#### `rep` — результат команды

Эхо `cmd_id` (`relay_cmd_id`, выделяется транспортом в `prepare_send`) и код
`RepResult` (`SUCCESS`/`FAILURE`/`NOT_SUPPORTED`). Обрабатывается
`CommandResultApplier`, который переводит команду в терминальный статус:
- при `SUCCESS`: `status = 'executed'`, `executed_at = now()`
- при `FAILURE`/`NOT_SUPPORTED`: `status = 'failed'`, `failed_at = now()`

#### `photos` — кадры фото

Каждый кадр — отдельный JPEG; классифицируется по реестру `relay_photo_series`
и публикуется в `MediaProducer` (см. раздел «Relay-фото — поток данных» выше).

### Сообщения от сервера

#### `com/{relay_id}` — команда

Backend публикует бинарный кадр `com` (12 байт: `relay_cmd_id`, код команды,
доп. параметр) с QoS 1. Коды команд (`Command` в `relay_protocol.py`):
`RELAY_ON`/`RELAY_OFF`/`RELAY_REBOOT`/`SEND_STATUS`/`LOCK_OPEN`(v2)/`LOCK_CLOSE`(v2).
Соответствие доменным `CommandType` («open»/«close»/«reboot») — в `MqttTransport`.

### Безопасность MQTT

- ✅ Аутентификация — per-device логин/пароль (`relay-{id}` / детерминированный
  hash от логина), проверяемые самим брокером через ACL (`app/mqtt/provisioning.py`)
- ✅ ACL ограничивает плату: запись в общие `stat`/`rep`/`photos`, чтение только
  своего `com/{relay_id}`
- ⚠️ Phase 1: broker слушает без TLS, пароль вычислим из публичного `relay_id`
  (не секрет) — приемлемо для текущей плоской сети, TLS + случайные секреты
  запланированы отдельно (см. `openapi/openapi_relay.md`, §6)

---

## Фоновые сервисы

Locker API использует два фоновых процесса для автоматизации.

### CommandQueue

**Файл**: `app/services/command_queue.py`

**Назначение**: Обработка очереди команд и доставка на подключенные устройства.

**Интервал**: 30 секунд

**Алгоритм**:
1. Получить все команды со статусом `pending`
2. Для каждой команды:
   - Проверить, подключено ли устройство (транспорт зарегистрирован в `CommandQueue`)
   - Если подключено: отправить команду через MQTT (`com/{relay_id}`), обновить статус на `sent`
   - Если не подключено: пропустить (команда останется `pending`)

**Использование**:

```python
from app.services.command_queue import CommandQueue

# Инициализация
command_queue = CommandQueue()

# Запуск фонового процессора
asyncio.create_task(command_queue.start_queue_processor(db, check_interval=30))

# Регистрация транспорта устройства (MqttTransport)
command_queue.register_connection(locker_id, device_transport)

# Отправка команды вручную
await command_queue.send_command(locker_id, {
    "type": "command",
    "command_id": "uuid",
    "command_type": "open",
    "parameters": {}
})

# Остановка процессора
command_queue.stop_queue_processor()
```

**Ключевые методы**:

```python
def register_connection(locker_id: str, transport: DeviceTransport) -> None:
    """Зарегистрировать транспорт устройства (MqttTransport)"""

def unregister_connection(locker_id: str) -> None:
    """Удалить транспорт устройства"""

def is_connected(locker_id: str) -> bool:
    """Проверить, подключено ли устройство"""

async def send_command(locker_id: str, command_data: dict) -> bool:
    """Отправить команду на устройство (возвращает True если успешно)"""

async def start_queue_processor(db, check_interval: int = 30):
    """Запустить фоновый процесс обработки очереди"""

def stop_queue_processor():
    """Остановить фоновый процесс"""
```

### StateMonitor

**Файл**: `app/services/state_monitor.py`

**Назначение**: Мониторинг значений датчиков и проверка пороговых значений.

**Интервал**: 60 секунд

**Алгоритм**:
1. Получить все активные датчики с активными пороговыми значениями
2. Для каждого датчика:
   - Проверить `current_value` на соответствие `sensor_thresholds`
   - Если нарушение:
     - Обновить `lockers.operational_status = 'alarm'`
     - Отправить notification через `notification_api`
     - Записать security event в `history_api` через продьюсер,
       выбираемый по `sensor_type_code`:
       - `intrusion` → event_type=intrusion, severity=high
       - `tamper`    → event_type=door_tamper, severity=critical
       - прочие      → event_type=threshold_violation, severity=high

**Использование**:

```python
from app.services.state_monitor import StateMonitor

# Инициализация
state_monitor = StateMonitor(db)

# Запуск фонового процессора
asyncio.create_task(state_monitor.start_monitoring(check_interval=60))

# Остановка мониторинга
state_monitor.stop_monitoring()
```

**Ключевые методы**:

```python
async def handle_threshold_violation(
    locker_id: str,
    sensor_id: str,
    violations: list[str]
) -> None:
    """Обработать нарушение пороговых значений"""

async def start_monitoring(check_interval: int = 60):
    """Запустить фоновый процесс мониторинга"""

def stop_monitoring():
    """Остановить фоновый процесс"""
```

### Добавление нового фонового сервиса

Пример: создадим сервис для автоматического удаления старых логов.

**Шаг 1**: Создайте файл `app/services/log_cleaner.py`:

```python
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class LogCleaner:
    """
    Автоматическое удаление старых логов датчиков
    """

    def __init__(self, db):
        self.db = db
        self._running = False

    async def clean_old_logs(self, days_to_keep: int = 365) -> int:
        """Удалить логи старше N дней"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)

        result = await self.db.fetch_one(
            """
            DELETE FROM sensor_logs
            WHERE recorded_at < %s
            RETURNING COUNT(*)
            """,
            (cutoff_date,),
            read_only=False
        )

        deleted_count = result['count'] if result else 0
        logger.info(f"Deleted {deleted_count} old sensor logs")
        return deleted_count

    async def start_cleaning(
        self,
        check_interval: int = 86400,  # 24 hours
        days_to_keep: int = 365
    ):
        """Запустить фоновый процесс очистки"""
        self._running = True
        logger.info(f"Log cleaner started (interval: {check_interval}s)")

        while self._running:
            try:
                await self.clean_old_logs(days_to_keep)
                await asyncio.sleep(check_interval)
            except Exception as e:
                logger.error(f"Error in log cleaner: {e}", exc_info=True)
                await asyncio.sleep(check_interval)

        logger.info("Log cleaner stopped")

    def stop_cleaning(self):
        """Остановить фоновый процесс"""
        self._running = False
```

**Шаг 2**: Добавьте в `app/main.py`:

```python
from .services.log_cleaner import LogCleaner

async def main():
    # ... существующая инициализация ...

    # Инициализация Log Cleaner
    log_cleaner = LogCleaner(db)

    # Запуск фонового процесса (раз в сутки)
    log_cleaner_task = asyncio.create_task(
        log_cleaner.start_cleaning(check_interval=86400, days_to_keep=365)
    )

    print("Background services started:")
    print("  - Command Queue Processor (30s interval)")
    print("  - State Monitor (60s interval)")
    print("  - Log Cleaner (24h interval)")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        command_queue.stop_queue_processor()
        state_monitor.stop_monitoring()
        log_cleaner.stop_cleaning()

        await command_queue_task
        await state_monitor_task
        await log_cleaner_task
```

---

## Типы датчиков и справочники

### Почему sensor_types - таблица, а не ENUM?

**❌ ENUM подход** (негибкий):

```sql
CREATE TYPE sensor_type_enum AS ENUM (
    'temperature',
    'humidity',
    'shock'
);

CREATE TABLE sensors (
    id UUID PRIMARY KEY,
    sensor_type sensor_type_enum  -- Добавить новый тип = миграция!
);
```

**Проблемы**:
- Добавление нового типа требует миграции БД
- Невозможно добавить через API
- Нельзя хранить метаданные типа (unit, description)

**✅ Таблица-справочник** (гибкий):

```sql
CREATE TABLE sensor_types (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    unit VARCHAR(20),
    value_type VARCHAR(20) NOT NULL
);

CREATE TABLE sensors (
    id UUID PRIMARY KEY,
    sensor_type_id INTEGER REFERENCES sensor_types(id)  -- FK к справочнику
);
```

**Преимущества**:
- ✅ Новые типы добавляются через API (без миграции!)
- ✅ Хранятся метаданные (unit, description, value_type)
- ✅ Возможность деактивации типов
- ✅ История изменений

### Добавление нового типа датчика

**Через API** (рекомендуется):

```bash
curl -X POST http://localhost:8001/api/v1/sensor-types \
  -H "Content-Type: application/json" \
  -H "X-Remote-User: 1" \
  -d '{
    "code": "battery",
    "name": "Заряд аккумулятора",
    "description": "Уровень заряда встроенного аккумулятора",
    "unit": "%",
    "value_type": "numeric"
  }'
```

**Через миграцию** (для базовых типов):

```sql
INSERT INTO sensor_types (code, name, description, unit, value_type)
VALUES (
    'battery',
    'Заряд аккумулятора',
    'Уровень заряда встроенного аккумулятора',
    '%',
    'numeric'
);
```

### Использование типов датчиков в коде

**Создание датчика**:

```python
# 1. Получить ID типа по code
sensor_type_repo = SensorTypeRepository(db)
sensor_type = await sensor_type_repo.get_by_code("temperature")

if not sensor_type:
    # Тип не найден
    raise ValueError("Sensor type 'temperature' not found")

# 2. Создать датчик
sensor_repo = SensorRepository(db)
sensor = await sensor_repo.create(
    locker_id="locker-uuid",
    serial_number="TEMP-001",
    sensor_type_id=sensor_type.id,  # FK к sensor_types
    current_value=25.0
)

# sensor содержит денормализованные поля:
# sensor.code = "temperature"
# sensor.name = "Температура"
# sensor.unit = "°C"
# sensor.value_type = "numeric"
```

### Денормализация sensor_types

Каждый объект `Sensor` содержит денормализованные поля из `sensor_types`:

```python
@dataclass
class Sensor:
    id: str
    locker_id: str
    serial_number: str
    sensor_type_id: int  # FK к sensor_types

    # Денормализованные поля из sensor_types:
    code: str        # от sensor_types.code
    name: str        # от sensor_types.name
    unit: Optional[str]  # от sensor_types.unit
    value_type: str  # от sensor_types.value_type

    current_value: Optional[float]
    # ...
```

**JOIN запрос в SensorRepository**:

```python
async def get_by_id(self, sensor_id: str) -> Optional[Sensor]:
    row = await self._db.fetch_one(
        """
        SELECT s.id, s.locker_id, s.serial_number, s.sensor_type_id,
               s.current_value, s.created_at, s.updated_at, s.is_active, s.metadata,
               st.code, st.name, st.unit, st.value_type
        FROM sensors s
        JOIN sensor_types st ON s.sensor_type_id = st.id
        WHERE s.id = %s
        """,
        (sensor_id,),
    )
    if not row:
        return None

    return from_dict(Sensor, row)
```

**Зачем это нужно?**
- Избегаем N+1 запросов при получении списка датчиков
- Клиенту API не нужно делать дополнительные запросы для получения типа
- Быстрее на больших выборках

---

## Аутентификация

Locker API использует **два разных механизма аутентификации** для разных шлюзов.

### Control Gateway (REST API)

**Механизм**: Trust Boundary через `X-Remote-User` header

**Как это работает**:

1. Запрос проходит через API Gateway (вне Locker API)
2. API Gateway аутентифицирует пользователя
3. API Gateway проверяет права доступа
4. API Gateway проксирует запрос с заголовком `X-Remote-User: {user_id}`
5. Locker API **доверяет** прокинутому ID и использует его для логирования

**Пример**:

```python
class BaseHandler(JSONHandler):
    """Базовый handler для Control Gateway"""

    async def prepare(self) -> None:
        """Извлечь user ID из X-Remote-User header"""
        user_id_str = self.request.headers.get("X-Remote-User")

        if not user_id_str:
            logger.warning("Missing X-Remote-User header")
            self.set_status(401)
            self.write(json.dumps({
                "error": "Unauthorized",
                "details": "Missing X-Remote-User header"
            }))
            raise Finish()

        try:
            self._current_user_id = int(user_id_str)
        except ValueError:
            logger.warning(f"Invalid X-Remote-User header: {user_id_str}")
            self.set_status(401)
            self.write(json.dumps({
                "error": "Unauthorized",
                "details": "Invalid X-Remote-User header"
            }))
            raise Finish()
```

**Важно**:
- ✅ Locker API **НЕ валидирует** user_id в БД
- ✅ Locker API **НЕ проверяет** права доступа
- ✅ Эти проверки выполняет API Gateway
- ✅ User ID используется только для `created_by`, `updated_by`

### Device Gateway (MQTT)

**Механизм**: аутентификация устройства выполняет сам брокер Mosquitto
(логин/пароль + ACL), а не код Locker API — приложение получает уже
авторизованные сообщения из подписанных топиков.

**Как это работает** (`app/mqtt/provisioning.py`):

1. При провижининге релейной платы вычисляются credentials:
   `username = "relay-{relay_id}"`, `password = hex(sha256(username)[:16])`.
2. Backend дописывает пароль и ACL-блок в конфиг брокера (`passwordfile`,
   `aclfile` в общем volume `mqtt_config`); ACL разрешает плате писать в общие
   `stat`/`rep`/`photos` и читать только свой `com/{relay_id}`.
3. Брокер перечитывает конфиг по SIGHUP (watcher, ~2с) и дальше сам проверяет
   логин/пароль при MQTT-подключении платы.
4. `RelayIngest` доверяет сообщениям, пришедшим из брокера (аутентификация уже
   произошла на уровне транспорта); связывание с конкретным `locker_id`
   происходит по `device_id`/`relay_id` внутри payload.

**Пример** (вычисление credentials):

```python
def broker_username(relay_id: int) -> str:
    return f"relay-{relay_id}"

def broker_password(relay_id: int) -> str:
    digest = hashlib.sha256(broker_username(relay_id).encode("ascii")).digest()
    return digest[:16].hex()
```

**Безопасность** (Phase 1, см. `openapi/openapi_relay.md`):
- ✅ ACL на уровне брокера ограничивает плату её собственным `com/{relay_id}`
- ⚠️ Пароль детерминированно выводится из публичного `relay_id` — не секрет
- ⚠️ Брокер слушает без TLS в текущей фазе; TLS + случайные per-device секреты
  запланированы отдельно

---

## Обработка ошибок

### Архитектура обработки ошибок

```
Исключение в коде
       ↓
BaseHandler.write_error()
       ↓
JSON response с error
```

### BaseHandler.write_error()

Централизованная обработка ошибок (shared.auth.JSONHandler):

```python
def write_error(self, status_code: int, **kwargs: Any) -> None:
    if "exc_info" in kwargs:
        _, e, _ = kwargs["exc_info"]

        # Ошибки БД → 503
        if isinstance(e, Exception) and "database" in str(type(e)).lower():
            self.set_status(503)
            self.finish(json.dumps({
                "error": "Database error",
                "details": str(e)
            }))
            return

    # Дефолтная обработка
    message = self._reason if hasattr(self, '_reason') else "Internal Server Error"
    self.finish(json.dumps({"error": message}))
```

### Типы ошибок

| HTTP код | Тип ошибки | Когда использовать |
|----------|------------|-------------------|
| **400** | Bad Request | Невалидные входные данные |
| **401** | Unauthorized | Отсутствует/невалиден X-Remote-User |
| **404** | Not Found | Сущность не найдена |
| **409** | Conflict | Конфликт (например, дублирующийся inventory_number) |
| **500** | Internal Server Error | Непредвиденная ошибка |
| **503** | Service Unavailable | Ошибка БД или внешнего сервиса |

### Возврат ошибок из хендлера

```python
# 400 - Bad Request
self.write_json({"error": "sensor_type_id is required"}, status_code=400)

# 404 - Not Found
self.write_json({"error": "Locker not found"}, status_code=404)

# 409 - Conflict
self.write_json({"error": "Inventory number already exists"}, status_code=409)

# 401 - Unauthorized
self.write_json({"error": "Unauthorized"}, status_code=401)
```

---

## Тестирование

### Краткий обзор

#### Unit-тесты

- Находятся в `tests/unit/`
- Используют моки БД и CommandQueue
- Быстрые, изолированные
- Тестируют логику handlers

**Запуск**:
```bash
pytest tests/unit/
```

#### Интеграционные тесты

- Находятся в `tests/integration/`
- Используют реальную БД и HTTP
- Тестируют полные сценарии
- Требуют запущенного сервиса

**Запуск**:
```bash
pytest tests/integration/
```

#### Написание unit-теста

```python
from unittest.mock import MagicMock, AsyncMock
from tornado.testing import AsyncHTTPTestCase

from app.main import make_app


class TestLockerApi(AsyncHTTPTestCase):
    def get_app(self):
        # Mock БД
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()

        # Mock CommandQueue
        self.command_queue_mock = MagicMock()

        return make_app(self.db_mock, self.command_queue_mock)

    def test_create_locker_success(self):
        """Тест: успешное создание шкафа"""
        self.db_mock.fetch_one.return_value = {
            "id": "locker-uuid",
            "inventory_number": "СО №777",
            # ... остальные поля
        }

        body = {
            "inventory_number": "СО №777",
            "location_address": "ул. Ленина, 1"
        }

        response = self.fetch(
            "/api/v1/lockers",
            method="POST",
            body=json.dumps(body),
            headers={"X-Remote-User": "1"}
        )

        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["inventory_number"], "СО №777")
```

### Тестирование MQTT (Device Gateway)

Device Gateway тестируется как обычный Python-код (`RelayIngest`/`MqttTransport`
не привязаны к транспортному сокету напрямую) — без поднятия настоящего
брокера. См. `tests/unit/test_ingest.py`, `test_ingest_bulkhead.py`,
`test_ingest_metrics.py`, `test_transport.py`, `test_relay_codec.py`.

```python
import asyncio

from app.locker_data.db.models.locker import Connectivity
from app.mqtt.ingest import RelayIngest


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def test_stat_online_registers_transport_and_sets_online():
    """Тест: входящий stat-кадр (v2, status=ONLINE) регистрирует транспорт
    и переводит connectivity шкафа в online"""
    async def scenario():
        ingest = RelayIngest(repo=FakeRepo({5: "L5"}), command_queue=FakeQueue(),
                              command_results=FakeResults(), mqtt_client=object())
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
    asyncio.run(scenario())
```

Интеграционные сценарии end-to-end (плата → брокер → locker_api) — в
`tests/system/` через `RelayRunner`/эмулятор, не в unit-тестах сервиса.

---

## OpenAPI документация

### Структура

```
openapi/
├── openapi.yaml              # Главный файл с $ref
├── components/
│   ├── parameters.yaml       # Переиспользуемые параметры
│   ├── responses.yaml        # Переиспользуемые ответы
│   └── securitySchemes.yaml  # Схемы безопасности
├── paths/
│   ├── lockers.yaml          # Эндпоинты /lockers
│   ├── sensors.yaml          # Эндпоинты /locker/{id}/sensors
│   ├── sensor_types.yaml     # Эндпоинты /sensor-types
│   ├── thresholds.yaml       # Эндпоинты /thresholds
│   └── commands.yaml         # Эндпоинты /commands
└── schemas/
    ├── locker.yaml           # Схема Locker
    ├── sensor.yaml           # Схема Sensor (с денормализацией)
    ├── sensor_type.yaml      # Схема SensorType
    ├── threshold.yaml        # Схема Threshold
    ├── command.yaml          # Схема Command
    ├── common_fields.yaml    # Общие поля (created_at, updated_at, etc.)
    └── error.yaml            # Схема ошибки
```

### Денормализованные схемы

В OpenAPI схемах отражена денормализация:

```yaml
# schemas/sensor.yaml
Sensor:
  type: object
  properties:
    id:
      type: string
      format: uuid
    sensor_type_id:
      type: integer
      format: int32

    # Денормализованные поля из sensor_types:
    code:
      type: string
      example: "temperature"
    name:
      type: string
      example: "Температура"
    unit:
      type: string
      nullable: true
      example: "°C"
    value_type:
      type: string
      enum: [numeric, boolean, string]
      example: "numeric"

    current_value:
      type: number
      format: double
      nullable: true
    # ...
```

### Просмотр документации

Используйте Redoc для просмотра:

```bash
docker run --rm -p 8080:80 \
  -e SPEC_URL=/spec/openapi.yaml \
  -v "$(pwd)/openapi:/usr/share/nginx/html/spec" \
  redocly/redoc
```

Откройте: **http://localhost:8080**

---

## Переменные окружения

### Приложение

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `DB_HOST` | Хост PostgreSQL | `localhost` |
| `DB_PORT` | Порт PostgreSQL | `5432` |
| `DB_USER` | Пользователь БД | `postgres` |
| `DB_PASSWORD` | Пароль БД | `postgres` |
| `DB_NAME` | Имя базы данных | `scud_lockers` |
| `APP_PORT` | Порт API сервера | `8001` |

### Тесты (интеграционные)

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `TEST_BASE_URL` | URL API для тестов | `http://localhost:8001` |
| `TEST_API_TIMEOUT` | Таймаут ожидания API (сек) | `120` |

### Пример .env файла

```bash
# .env
DB_HOST=localhost
DB_PORT=5432
DB_USER=admin
DB_PASSWORD=secure_password
DB_NAME=scud_lockers
APP_PORT=8001
```

---

## Запуск и отладка

### Docker Compose

Самый простой способ:

```bash
cd backend
docker compose up --build locker_api
```

Сервис будет доступен на `http://localhost:8001`.

### Локальный запуск

> **Внимание:** локальный запуск без Docker **не поддерживается**.
> Backend-сервисы зависят от пакетов из сертифицированного `main` Astra
> Linux apt. `pip install` из PyPI
> ставит библиотеки с другими версиями и не сертифицирован — версии
> в `requirements.txt` оставлены как индикатив для IDE-автокомплита, но
> production-сборка идёт через apt в `Dockerfile`.
>
> Для разработки используйте `docker compose up --build locker_api`
> (см. выше); для тестов — `docker compose --profile tests up --build
> locker_api_tests`.

Если действительно нужно запустить вне Docker (например, для отладки в IDE):
поднимите PostgreSQL отдельно (`docker run -d -p 5433:5432 -e POSTGRES_USER=admin
-e POSTGRES_PASSWORD=password -e POSTGRES_DB=scud_lockers
registry.astralinux.ru/library/astra/ubi18-postgresql15:1.8.5`), примените миграции
из `app/locker_data/db/migrations/`, затем запустите Python-процесс. Зависимости
должны совпадать с теми, что ставит `services/locker_api/Dockerfile` через apt.

### Отладка MQTT

Кастомная сборка Mosquitto в этом проекте собрана без клиентов
`mosquitto_pub`/`mosquitto_sub` — используйте `shared.mqtt_client.MqttClient`
(тот же клиент, что и `app/mqtt/`) из Python-скрипта, авторизовавшись
per-device credentials из `app/mqtt/provisioning.py`:

```python
import asyncio

from shared.mqtt_client import MqttClient, MqttQoS


async def debug_publish_com(relay_id: int, frame: bytes) -> None:
    client = MqttClient(host="localhost", port=1883, client_id="debug-cli")
    await client.connect()
    await client.publish(f"com/{relay_id}", frame, qos=MqttQoS.AT_LEAST_ONCE)
    await client.disconnect()


asyncio.run(debug_publish_com(5, b"..."))
```

Для эмуляции целой платы (полный протокол `stat`/`rep`/`photos`) используйте
эмулятор из `emulator/` (`scud fleet ...`), а не ручные MQTT-скрипты —
он реализует и v1/v2 кадры, и провижининг брокерных credentials.

---

## Best Practices

### MQTT (Device Gateway)

✅ **DO**:
- Полагайтесь на ACL брокера как на аутентификацию — не принимайте от платы
  данные, не соответствующие её собственному `com/{relay_id}`
- Обрабатывайте отсутствие свежего `stat` gracefully (offline-монитор по TTL,
  update state to offline — прямой аналог бывшего heartbeat-таймаута WS-пути)
- Логируйте все важные события (регистрация транспорта, команды, offline-переходы)

❌ **DON'T**:
- Не забывайте обновлять `last_seen_at` при каждом `stat`
- Не храните состояние только в памяти (используйте БД); незавершённые
  фото-серии — исключение, они живут в памяти с TTL-сборкой мусора

### Фоновые процессы

✅ **DO**:
- Используйте try/except для обработки ошибок
- Логируйте все ошибки с traceback
- Реализуйте graceful shutdown (stop методы)
- Используйте reasonable интервалы (не слишком частые)

❌ **DON'T**:
- Не запускайте CPU-intensive задачи в фоновых процессах
- Не забывайте про await asyncio.sleep()
- Не игнорируйте исключения

### Денормализация

✅ **DO**:
- Используйте денормализацию для read-heavy операций
- Документируйте денормализованные поля в docstrings
- Обновляйте денормализованные поля при изменении источника

❌ **DON'T**:
- Не злоупотребляйте денормализацией (только для performance)
- Не забывайте про JOIN запросы в repositories

### Справочники

✅ **DO**:
- Используйте таблицы для справочников (не ENUM)
- Предоставляйте CRUD API для управления справочниками
- Добавляйте валидацию для code (уникальность, формат)

❌ **DON'T**:
- Не удаляйте записи из справочников (используйте is_active)
- Не изменяйте code после создания (используется как идентификатор)

---

## Полезные ссылки

- [Tornado Documentation](https://www.tornadoweb.org/en/stable/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [pytest Documentation](https://docs.pytest.org/)
- [OpenAPI Specification](https://swagger.io/specification/)
- [MQTT 3.1.1 Specification](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)
- Протокол релейных плат — `openapi/openapi_relay.md`

---

## Поддержка

При возникновении вопросов или проблем:

1. Проверьте это руководство
2. Изучите существующий код (Locker, Sensor, Command - хорошие примеры)
3. Посмотрите README.md для обзора архитектуры
4. Изучите тесты в `tests/`
5. Обратитесь к команде разработки

---

**Версия документа**: 1.0
**Последнее обновление**: 2026-03-15
