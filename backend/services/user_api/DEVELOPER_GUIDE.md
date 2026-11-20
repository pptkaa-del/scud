# Руководство разработчика User API

Комплексное руководство по разработке и поддержке микросервиса User API для системы ScudDocs.

## Содержание

- [Введение](#введение)
- [Архитектура](#архитектура)
- [Структура проекта](#структура-проекта)
- [Как добавить новую сущность](#как-добавить-новую-сущность)
- [Как добавить новый эндпоинт](#как-добавить-новый-эндпоинт)
- [Работа с базой данных](#работа-с-базой-данных)
- [Валидация данных](#валидация-данных)
- [Обработка ошибок](#обработка-ошибок)
- [Авторизация](#авторизация)
- [Тестирование](#тестирование)
- [OpenAPI документация](#openapi-документация)
- [Переменные окружения](#переменные-окружения)
- [Запуск и отладка](#запуск-и-отладка)
- [Best Practices](#best-practices)

---

## Введение

**User API** — асинхронный REST API микросервис для управления пользователями и подрядчиками в системе ScudDocs.

### Технологический стек

- **Python 3.11+** - язык программирования
- **Tornado** - асинхронный веб-фреймворк
- **PostgreSQL** - база данных
- **psycopg2** - драйвер PostgreSQL с кастомной асинхронной оберткой
- **pytest** - фреймворк для тестирования
- **dacite** - конвертация dict → dataclass
- **Docker** - контейнеризация

### Основные возможности

- ✅ CRUD операции для пользователей и подрядчиков
- ✅ Асинхронная обработка запросов
- ✅ Валидация входных данных
- ✅ Централизованная обработка ошибок
- ✅ Авторизация через X-Remote-User header
- ✅ Retry-логика для работы с БД
- ✅ Connection pooling
- ✅ OpenAPI документация

---

## Архитектура

Сервис построен по **трёхуровневой архитектуре**:

```
┌─────────────────────────────────────────────┐
│           HTTP Layer (Handlers)             │  ← Обработка HTTP запросов
│    BaseHandler, UserHandler, Contractor     │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│        Business Logic (Repositories)        │  ← Бизнес-логика и валидация
│   UserRepository, ContractorRepository      │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│         Data Layer (AsyncPostgres)          │  ← Работа с БД
│    Connection pooling, Retry, Transactions  │
└─────────────────────────────────────────────┘
```

### Принципы

1. **Single Responsibility** - каждый модуль отвечает за одну задачу
2. **Dependency Injection** - БД передается через конструктор
3. **Repository Pattern** - изоляция логики работы с БД
4. **Async/Await** - неблокирующие операции ввода-вывода
5. **Dataclasses** - типизированные модели данных

---

## Структура проекта

```
backend/user_api/
├── app/                                 # Основное приложение
│   ├── __init__.py
│   ├── main.py                         # Точка входа, создание приложения
│   ├── validators.py                   # Валидаторы данных
│   │
│   ├── handlers/                       # HTTP обработчики
│   │   ├── __init__.py
│   │   ├── base.py                     # Базовый класс для всех хендлеров
│   │   ├── user.py                     # Хендлеры для /api/v1/users
│   │   └── contractor.py               # Хендлеры для /api/v1/contractors
│   │
│   └── user_data/                      # Слой работы с данными
│       ├── __init__.py
│       └── db/                         # Модуль базы данных
│           ├── __init__.py
│           ├── connection.py           # Асинхронный коннектор PostgreSQL
│           ├── errors.py               # Кастомные исключения БД
│           │
│           ├── models/                 # Модели данных (dataclasses)
│           │   ├── __init__.py
│           │   ├── user.py             # User, UserState
│           │   └── contractor.py       # Contractor
│           │
│           ├── repositories/           # Репозитории (data access layer)
│           │   ├── __init__.py
│           │   ├── user.py             # UserRepository
│           │   └── contractor.py       # ContractorRepository
│           │
│           └── migrations/             # SQL миграции
│               └── 001_init.sql        # Начальная схема БД
│
├── tests/                              # Тесты
│   ├── __init__.py
│   ├── TESTING_GUIDE.md               # Подробное руководство по тестированию
│   │
│   ├── unit/                          # Unit-тесты с моками
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   ├── test_user_api.py
│   │   └── test_contractor_api.py
│   │
│   └── integration/                   # Интеграционные тесты
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_user_integration.py
│       └── test_contractor_integration.py
│
├── openapi/                           # OpenAPI спецификация
│   ├── openapi.yaml                   # Главный файл спецификации
│   ├── components/
│   │   ├── parameters.yaml
│   │   ├── responses.yaml
│   │   └── securitySchemes.yaml
│   ├── paths/
│   │   ├── users.yaml
│   │   └── contractors.yaml
│   └── schemas/
│       ├── user.yaml
│       ├── contractor.yaml
│       ├── requests.yaml
│       └── error.yaml
│
├── Dockerfile                         # Docker образ
├── requirements.txt                   # Python зависимости
├── README.md                          # Краткое описание
└── DEVELOPER_GUIDE.md                 # Это руководство
```

### Описание ключевых модулей

#### `app/main.py`
- Точка входа приложения
- Создание Tornado Application
- Регистрация роутов
- Инициализация подключения к БД
- Логика ожидания готовности БД

#### `app/handlers/base.py`
- `BaseHandler` - базовый класс для всех HTTP обработчиков
- Авторизация через `X-Remote-User` header
- Утилиты: `write_json()`, `get_json_body()`, `write_error()`
- Сериализация dataclasses и datetime

#### `app/user_data/db/connection.py`
- `AsyncPostgres` - асинхронная обертка над psycopg2
- Connection pooling через `ThreadedConnectionPool`
- Retry-логика с exponential backoff
- Context manager для транзакций
- Методы: `fetch_one()`, `fetch_all()`, `execute()`

#### `app/user_data/db/repositories/`
- Паттерн Repository для изоляции SQL
- Преобразование row → dataclass через dacite
- Методы CRUD для каждой сущности

---

## Как добавить новую сущность

Пошаговая инструкция для добавления новой сущности (например, `Document`).

### Шаг 1: Создать миграцию БД

Создайте файл `app/user_data/db/migrations/00X_add_documents.sql`:

```sql
CREATE TABLE documents (
    id BIGSERIAL PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    content TEXT,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by BIGINT REFERENCES users(id)
);

CREATE INDEX idx_documents_user_id ON documents(user_id);
```

Примените миграцию вручную:
```bash
psql -h localhost -U admin -d scud -f app/user_data/db/migrations/00X_add_documents.sql
```

### Шаг 2: Создать модель данных

Создайте файл `app/user_data/db/models/document.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(slots=True)
class Document:
    id: int
    title: str
    content: Optional[str]
    user_id: int
    created_at: datetime
    created_by: Optional[int]
```

### Шаг 3: Создать репозиторий

Создайте файл `app/user_data/db/repositories/document.py`:

```python
from __future__ import annotations

from typing import Optional
from dacite import from_dict

from ..connection import AsyncPostgres
from ..models.document import Document


class DocumentRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, document_id: int) -> Optional[Document]:
        row = await self._db.fetch_one(
            "SELECT * FROM documents WHERE id = %s",
            (document_id,),
        )
        if not row:
            return None
        return from_dict(Document, row)

    async def get_all(self, user_id: Optional[int] = None) -> list[Document]:
        if user_id:
            rows = await self._db.fetch_all(
                "SELECT * FROM documents WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM documents ORDER BY created_at DESC"
            )
        return [from_dict(Document, row) for row in rows]

    async def create(
        self,
        title: str,
        content: Optional[str],
        user_id: int,
        created_by: int,
    ) -> Document:
        row = await self._db.fetch_one(
            """
            INSERT INTO documents (title, content, user_id, created_by)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (title, content, user_id, created_by),
            read_only=False,
        )
        return from_dict(Document, row)

    async def update(
        self,
        document_id: int,
        title: str,
        content: Optional[str],
    ) -> Optional[Document]:
        row = await self._db.fetch_one(
            """
            UPDATE documents
            SET title = %s, content = %s
            WHERE id = %s
            RETURNING *
            """,
            (title, content, document_id),
            read_only=False,
        )
        if not row:
            return None
        return from_dict(Document, row)

    async def delete(self, document_id: int) -> None:
        await self._db.execute(
            "DELETE FROM documents WHERE id = %s",
            (document_id,),
        )
```

### Шаг 4: Создать хендлеры

Создайте файл `app/handlers/document.py`:

```python
from .base import BaseHandler
from ..user_data.db.repositories.document import DocumentRepository


class DocumentListHandler(BaseHandler):
    """GET /documents, POST /documents"""

    async def get(self):
        """Получение списка документов"""
        user_id_str = self.get_query_argument("user_id", default=None)
        user_id = int(user_id_str) if user_id_str else None

        repo = DocumentRepository(self.db)
        documents = await repo.get_all(user_id=user_id)
        self.write_json(documents)

    async def post(self):
        """Создание документа"""
        if not self.current_user:
            self.write_json({"error": "Unauthorized"}, status_code=401)
            return

        body = self.get_json_body()
        title = body.get("title")
        content = body.get("content")
        user_id = body.get("user_id")

        if not title or not user_id:
            self.write_json({"error": "title and user_id are required"}, status_code=400)
            return

        repo = DocumentRepository(self.db)
        document = await repo.create(
            title=title,
            content=content,
            user_id=user_id,
            created_by=self.current_user.id,
        )
        self.write_json(document, status_code=201)


class DocumentDetailHandler(BaseHandler):
    """GET/PUT/DELETE /documents/{id}"""

    async def get(self, document_id: str):
        """Получение документа по ID"""
        repo = DocumentRepository(self.db)
        document = await repo.get_by_id(int(document_id))

        if not document:
            self.write_json({"error": "Document not found"}, status_code=404)
            return

        self.write_json(document)

    async def put(self, document_id: str):
        """Обновление документа"""
        body = self.get_json_body()
        title = body.get("title")
        content = body.get("content")

        if not title:
            self.write_json({"error": "title is required"}, status_code=400)
            return

        repo = DocumentRepository(self.db)
        document = await repo.update(int(document_id), title=title, content=content)

        if not document:
            self.write_json({"error": "Document not found"}, status_code=404)
            return

        self.write_json(document)

    async def delete(self, document_id: str):
        """Удаление документа"""
        repo = DocumentRepository(self.db)

        # Проверяем существование
        existing = await repo.get_by_id(int(document_id))
        if not existing:
            self.write_json({"error": "Document not found"}, status_code=404)
            return

        await repo.delete(int(document_id))
        self.set_status(204)
```

### Шаг 5: Зарегистрировать роуты

Отредактируйте `app/main.py`:

```python
from .handlers.document import DocumentListHandler, DocumentDetailHandler

def make_app(db: AsyncPostgres):
    return tornado.web.Application([
        (r"/api/v1/users/([^/]+)", UserHandler, dict(db=db)),
        (r"/api/v1/users", UserListHandler, dict(db=db)),
        (r"/api/v1/contractor/([^/]+)", ContractorDetailHandler, dict(db=db)),
        (r"/api/v1/contractors", ContractorListHandler, dict(db=db)),
        # Новые роуты:
        (r"/api/v1/documents/([^/]+)", DocumentDetailHandler, dict(db=db)),
        (r"/api/v1/documents", DocumentListHandler, dict(db=db)),
    ], debug=True)
```

### Шаг 6: Написать тесты

**Unit-тест** (`tests/unit/test_document_api.py`):

```python
import json
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import UserState


class TestDocumentApi(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        return make_app(self.db_mock)

    def setUp(self):
        super().setUp()
        self.auth_user_row = {
            "id": 1, "phone": "+79123456789", "full_name": "Admin",
            "contractor_id": None, "created_at": datetime.now(),
            "created_by": None, "updated_at": datetime.now(),
            "updated_by": None, "state": UserState.ACTIVE
        }

    def test_create_document_success(self):
        """Тест: успешное создание документа"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            {"id": 1, "title": "Test Doc", "content": "Content",
             "user_id": 1, "created_at": datetime.now(), "created_by": 1}
        ]

        body = {"title": "Test Doc", "content": "Content", "user_id": 1}
        response = self.fetch("/api/v1/documents", method="POST",
                             body=json.dumps(body),
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Test Doc")
```

**Интеграционный тест** (`tests/integration/test_document_integration.py`):

```python
def test_document_lifecycle(api_client):
    """Полный CRUD цикл для документов"""
    # Create
    payload = {"title": "Test Document", "content": "Test content", "user_id": 1}
    response = api_client.post("/api/v1/documents", json=payload)
    assert response.status_code == 201
    doc_id = response.json()["id"]

    # Read
    response = api_client.get(f"/api/v1/documents/{doc_id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Test Document"

    # Update
    response = api_client.put(f"/api/v1/documents/{doc_id}",
                             json={"title": "Updated", "content": "New content"})
    assert response.status_code == 200

    # Delete
    response = api_client.delete(f"/api/v1/documents/{doc_id}")
    assert response.status_code == 204
```

### Шаг 7: Обновить OpenAPI

Создайте `openapi/schemas/document.yaml`:

```yaml
Document:
  type: object
  properties:
    id:
      type: integer
      format: int64
    title:
      type: string
      maxLength: 255
    content:
      type: string
      nullable: true
    user_id:
      type: integer
      format: int64
    created_at:
      type: string
      format: date-time
    created_by:
      type: integer
      format: int64
      nullable: true
  required:
    - id
    - title
    - user_id
    - created_at
```

Создайте `openapi/paths/documents.yaml` и добавьте ссылку в `openapi/openapi.yaml`.

---

## Как добавить новый эндпоинт

Инструкция по добавлению нового эндпоинта к **существующей** сущности.

Например, добавим эндпоинт `PATCH /api/v1/users/{id}/change-phone` для изменения телефона.

### Шаг 1: Добавить метод в репозиторий

В `app/user_data/db/repositories/user.py`:

```python
async def update_phone(
    self,
    user_id: int,
    new_phone: str,
    updated_by: int,
) -> Optional[User]:
    """Обновление телефона пользователя"""
    row = await self._db.fetch_one(
        """
        UPDATE users
        SET phone = %s, updated_at = now(), updated_by = %s
        WHERE id = %s
        RETURNING *
        """,
        (new_phone, updated_by, user_id),
        read_only=False,
    )
    if not row:
        return None
    return from_dict(User, row, config=_DACITE_CONFIG)
```

### Шаг 2: Добавить метод в хендлер

В `app/handlers/user.py`, добавьте новый метод в `UserHandler`:

```python
async def patch(self, user_id: str):
    """PATCH /users/{id} - частичное обновление пользователя"""
    if not self.current_user:
        self.write_json({"error": "Unauthorized"}, status_code=401)
        return

    body = self.get_json_body()
    new_phone = body.get("phone")

    if not new_phone:
        self.write_json({"error": "phone is required"}, status_code=400)
        return

    # Валидация
    if not validate_phone(new_phone):
        self.write_json({
            "error": "Invalid phone format",
            "details": "Phone must be in format +79999999999"
        }, status_code=400)
        return

    repo = UserRepository(self.db)

    # Проверка на конфликт
    existing = await repo.get_by_phone(new_phone)
    if existing and existing.id != int(user_id):
        self.write_json({"error": "Phone already in use"}, status_code=409)
        return

    # Обновление
    user = await repo.update_phone(
        int(user_id),
        new_phone,
        self.current_user.id
    )

    if not user:
        self.write_json({"error": "User not found"}, status_code=404)
        return

    self.write_json(user)
```

### Шаг 3: Написать тесты

**Unit-тест**:

```python
def test_update_phone_success(self):
    """Тест: успешное изменение телефона"""
    self.db_mock.fetch_one.side_effect = [
        self.auth_user_row,  # auth
        None,  # check phone conflict
        {"id": 2, "phone": "+79001112233", "full_name": "User",
         "contractor_id": None, "created_at": datetime.now(),
         "created_by": 1, "updated_at": datetime.now(),
         "updated_by": 1, "state": UserState.ACTIVE}
    ]

    body = {"phone": "+79001112233"}
    response = self.fetch("/api/v1/users/2", method="PATCH",
                         body=json.dumps(body),
                         headers={"X-Remote-User": "1"})

    self.assertEqual(response.code, 200)
    data = json.loads(response.body)
    self.assertEqual(data["phone"], "+79001112233")
```

### Шаг 4: Обновить OpenAPI

В `openapi/paths/users.yaml`, добавьте описание для PATCH метода.

---

## Работа с базой данных

### AsyncPostgres

Кастомный асинхронный коннектор, построенный на psycopg2 + ThreadPoolExecutor.

#### Инициализация

```python
from app.user_data.db.connection import AsyncPostgres, PostgresConfig

config = PostgresConfig(
    host="localhost",
    port=5432,
    user="admin",
    password="password",
    database="scud",
    connect_timeout_sec=5,
)

db = AsyncPostgres(
    config,
    max_workers=5,           # Размер thread pool
    min_connections=1,       # Минимум соединений в пуле
    retries=3,               # Количество повторов при ошибке
    retry_delay_sec=0.2,     # Начальная задержка между попытками
)
```

#### Методы

**`fetch_one()` - получить одну строку**:

```python
row = await db.fetch_one(
    "SELECT * FROM users WHERE id = %s",
    (user_id,)
)
# row: Optional[dict]
```

**`fetch_all()` - получить все строки**:

```python
rows = await db.fetch_all(
    "SELECT * FROM users WHERE state = %s",
    ('active',)
)
# rows: list[dict]
```

**`execute()` - выполнить команду без возврата данных**:

```python
await db.execute(
    "UPDATE users SET state = %s WHERE id = %s",
    ('deactivated', user_id)
)
```

#### Параметр `read_only`

Для `fetch_one()` можно указать `read_only=False`, если запрос изменяет данные:

```python
row = await db.fetch_one(
    "INSERT INTO users (...) VALUES (...) RETURNING *",
    (...),
    read_only=False,
)
```

#### Retry-логика

Коннектор автоматически повторяет запросы при временных ошибках:
- Connection errors (08xxx)
- Resource errors (53xxx)
- Server shutdown errors (57Pxx)

Используется **exponential backoff** с ограничением максимальной задержки.

#### Транзакции

Каждый запрос выполняется в транзакции:
- При успехе - автоматический `COMMIT`
- При ошибке - автоматический `ROLLBACK`

### Создание миграций

1. Создайте файл в `app/user_data/db/migrations/`:
   ```
   00X_description.sql
   ```

2. Напишите SQL:
   ```sql
   -- Создание таблицы
   CREATE TABLE my_table (...);

   -- Создание индексов
   CREATE INDEX idx_my_table_field ON my_table(field);

   -- Изменение существующей таблицы
   ALTER TABLE users ADD COLUMN new_field VARCHAR(255);
   ```

3. Примените миграцию:
   ```bash
   psql -h localhost -U admin -d scud -f app/user_data/db/migrations/00X_description.sql
   ```

### Best Practices для SQL

✅ **DO**:
- Используйте параметризованные запросы (`%s`)
- Используйте `RETURNING *` для INSERT/UPDATE
- Добавляйте индексы для полей в WHERE
- Используйте транзакции

❌ **DON'T**:
- Не используйте string interpolation для SQL (уязвимость к SQL injection)
- Не делайте N+1 запросов (используйте JOIN или IN)
- Не забывайте про LIMIT для больших выборок

---

## Валидация данных

### Существующие валидаторы

#### `validate_phone(phone: str) -> bool`

Проверяет формат российского номера телефона.

```python
from app.validators import validate_phone

if not validate_phone("+79001234567"):
    raise ValueError("Invalid phone format")
```

**Формат**: `+7XXXXXXXXXX` (ровно 12 символов)

### Создание нового валидатора

Добавьте функцию в `app/validators.py`:

```python
import re

EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

def validate_email(email: str) -> bool:
    """
    Проверяет, что email соответствует формату

    Args:
        email: Email для проверки

    Returns:
        True если email валиден, False в противном случае

    Examples:
        >>> validate_email("test@example.com")
        True
        >>> validate_email("invalid")
        False
    """
    if not email:
        return False
    return EMAIL_PATTERN.match(email) is not None
```

Используйте в хендлере:

```python
from ..validators import validate_email

email = body.get("email")
if not validate_email(email):
    self.write_json({
        "error": "Invalid email format"
    }, status_code=400)
    return
```

### Валидация сложных структур

Для валидации сложных JSON объектов рекомендуется использовать **pydantic** или **marshmallow**.

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

Централизованная обработка ошибок в `app/handlers/base.py`:

```python
def write_error(self, status_code: int, **kwargs: Any) -> None:
    if "exc_info" in kwargs:
        _, e, _ = kwargs["exc_info"]

        # Ошибки БД → 503
        if isinstance(e, AsyncPostgresError):
            self.set_status(503)
            self.finish(json.dumps({
                "error": "Database error",
                "details": str(e)
            }))
            return

        # ValueError → 400
        if isinstance(e, ValueError):
            self.set_status(400)
            self.finish(json.dumps({
                "error": "Invalid input",
                "details": str(e)
            }))
            return

    # Дефолтная обработка
    message = self._reason
    self.finish(json.dumps({"error": message}))
```

### Типы ошибок

| HTTP код | Тип ошибки | Когда использовать |
|----------|------------|-------------------|
| **400** | Bad Request | Невалидные входные данные |
| **401** | Unauthorized | Отсутствует/невалиден X-Remote-User |
| **404** | Not Found | Сущность не найдена |
| **409** | Conflict | Конфликт (например, дублирующийся телефон) |
| **500** | Internal Server Error | Непредвиденная ошибка |
| **503** | Service Unavailable | Ошибка БД |

### Возврат ошибок из хендлера

```python
# 400 - Bad Request
self.write_json({"error": "field is required"}, status_code=400)

# 404 - Not Found
self.write_json({"error": "User not found"}, status_code=404)

# 409 - Conflict
self.write_json({"error": "Phone already exists"}, status_code=409)

# 401 - Unauthorized (обрабатывается в BaseHandler.prepare)
```

### Кастомные исключения

Определены в `app/user_data/db/errors.py`:

```python
class AsyncPostgresError(Exception):
    """Базовое исключение для ошибок БД"""
    pass

class ConnectionPoolExhaustedError(AsyncPostgresError):
    """Пул соединений исчерпан"""
    pass

class RetryExhaustedError(AsyncPostgresError):
    """Все попытки повтора исчерпаны"""
    pass
```

---

## Авторизация

### Механизм X-Remote-User

Сервис использует упрощенную схему авторизации через заголовок `X-Remote-User`.

#### Как это работает

1. Клиент отправляет запрос с заголовком:
   ```http
   GET /api/v1/users/123
   X-Remote-User: 1
   ```

2. `BaseHandler.prepare()` извлекает `user_id` из заголовка

3. Загружается пользователь из БД:
   ```python
   self._current_user = await repo.get_by_id(user_id)
   ```

4. Если пользователь не найден → `401 Unauthorized`

5. Пользователь доступен в хендлере через `self.current_user`

#### Использование в хендлере

```python
class MyHandler(BaseHandler):
    async def post(self):
        # current_user гарантированно заполнен
        # (prepare() вернет 401 если нет)
        created_by = self.current_user.id

        # Можно проверить права
        if self.current_user.contractor_id is None:
            # Только системные пользователи
            pass
```

#### Защита от гонки

Хотя `prepare()` гарантирует наличие `current_user`, добавьте проверку для безопасности:

```python
if not self.current_user:
    self.write_json({"error": "Unauthorized"}, status_code=401)
    return
```

### Будущие улучшения

В production окружении рекомендуется заменить на:
- JWT токены
- OAuth 2.0
- Session-based auth

---

## Тестирование

Подробное руководство доступно в **[tests/TESTING_GUIDE.md](tests/TESTING_GUIDE.md)**.

### Краткий обзор

#### Unit-тесты

- Находятся в `tests/unit/`
- Используют моки БД
- Быстрые, изолированные
- Тестируют логику обработчиков

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

#### Написание тестов

**Unit-тест**:
```python
class TestMyFeature(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        return make_app(self.db_mock)

    def test_something(self):
        self.db_mock.fetch_one.return_value = {"id": 1, ...}
        response = self.fetch("/api/v1/endpoint")
        self.assertEqual(response.code, 200)
```

**Интеграционный тест**:
```python
def test_full_flow(api_client):
    response = api_client.post("/api/v1/entities", json={"name": "Test"})
    assert response.status_code == 201
    entity_id = response.json()["id"]

    response = api_client.get(f"/api/v1/entities/{entity_id}")
    assert response.status_code == 200
```

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
│   ├── users.yaml            # Эндпоинты /users
│   └── contractors.yaml      # Эндпоинты /contractors
└── schemas/
    ├── user.yaml             # Схема User
    ├── contractor.yaml       # Схема Contractor
    ├── requests.yaml         # Схемы запросов
    └── error.yaml            # Схема ошибки
```

### Обновление документации

1. **При добавлении поля в модель**:

   Обновите соответствующий файл в `schemas/`:

   ```yaml
   # schemas/user.yaml
   User:
     type: object
     properties:
       id:
         type: integer
       # Новое поле:
       email:
         type: string
         format: email
   ```

2. **При добавлении эндпоинта**:

   Добавьте описание в `paths/`:

   ```yaml
   # paths/users.yaml
   /users/{id}/change-phone:
     patch:
       summary: Change user phone
       parameters:
         - name: id
           in: path
           required: true
           schema:
             type: integer
       requestBody:
         content:
           application/json:
             schema:
               type: object
               properties:
                 phone:
                   type: string
       responses:
         '200':
           description: Success
   ```

3. **Проверка**:

   Используйте Swagger Editor для валидации:
   ```bash
   # Установите swagger-cli
   npm install -g @apidevtools/swagger-cli

   # Валидация
   swagger-cli validate openapi/openapi.yaml
   ```

### Важные правила форматирования

При работе с разделенными на файлы OpenAPI спецификациями необходимо соблюдать правила именования якорей (anchors):

1. **Ключи в файлах `paths/*.yaml`**:

   ✅ **Правильно** - без начального `/`:
   ```yaml
   # paths/emergency_requests.yaml
   emergency-requests:
     get:
       summary: Получить список заявок

   emergency-request/{id}:
     get:
       summary: Получить заявку

   emergency-request/{id}/logs:
     get:
       summary: Получить логи заявки
   ```

   ❌ **Неправильно** - с начальным `/`:
   ```yaml
   # НЕ ДЕЛАЙТЕ ТАК!
   /emergency-requests:
     get:
       summary: ...
   ```

2. **Ссылки в главном файле `openapi.yaml`**:

   При ссылке на якоря, содержащие `/` в имени, используйте экранирование `~1` (JSON Pointer синтаксис):

   ```yaml
   # openapi.yaml
   paths:
     /emergency-requests:
       $ref: './paths/emergency_requests.yaml#/emergency-requests'
     /emergency-request/{id}:
       $ref: './paths/emergency_requests.yaml#/emergency-requests~1{id}'
     /emergency-request/{id}/logs:
       $ref: './paths/emergency_requests.yaml#/emergency-requests~1{id}~1logs'
   ```

   **Объяснение**: `~1` экранирует символ `/` в JSON Pointer, `~0` экранирует `~`.

3. **Внутренние ссылки в схемах**:

   ✅ **Правильно** - относительная ссылка в том же файле:
   ```yaml
   # schemas/emergency_request.yaml
   EmergencyRequestListResponse:
     properties:
       items:
         type: array
         items:
           $ref: '#/EmergencyRequestWithDetails'
   ```

   ❌ **Неправильно** - попытка использовать components/schemas:
   ```yaml
   # НЕ ДЕЛАЙТЕ ТАК в отдельном файле!
   items:
     $ref: '#/components/schemas/EmergencyRequestWithDetails'
   ```

### Просмотр документации

Для просмотра OpenAPI документации в красивом интерфейсе используйте Redoc:

```bash
# Запустите Redoc через Docker
docker run --rm -p 8080:80 \
  -e SPEC_URL=/spec/openapi.yaml \
  -v $(pwd)/openapi:/usr/share/nginx/html/spec \
  redocly/redoc

# Для Windows PowerShell используйте полный путь:
docker run --rm -p 8080:80 `
  -e SPEC_URL=/spec/openapi.yaml `
  -v C:\Users\username\path\to\project\backend\user_api\openapi:/usr/share/nginx/html/spec `
  redocly/redoc
```

**Параметры**:
- `-p 8080:80` - проброс порта (доступ через http://localhost:8080)
- `-e SPEC_URL=/spec/openapi.yaml` - указание пути к спецификации
- `-v path:/usr/share/nginx/html/spec` - монтирование директории с OpenAPI файлами

После запуска откройте в браузере: **http://localhost:8080**

**Альтернативы**:
- **Swagger UI**: `docker run -p 8080:8080 -e SWAGGER_JSON=/spec/openapi.yaml -v $(pwd)/openapi:/spec swaggerapi/swagger-ui`
- **Swagger Editor**: https://editor.swagger.io (загрузите файл вручную)

---

## Переменные окружения

### Приложение

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `DB_HOST` | Хост PostgreSQL | `localhost` |
| `DB_PORT` | Порт PostgreSQL | `5432` |
| `DB_USER` | Пользователь БД | `postgres` |
| `DB_PASSWORD` | Пароль БД | `postgres` |
| `DB_NAME` | Имя базы данных | `scud` |
| `APP_PORT` | Порт API сервера | `8888` |

### Тесты (интеграционные)

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `TEST_BASE_URL` | URL API для тестов | `http://localhost:8000` |
| `TEST_API_TIMEOUT` | Таймаут ожидания API (сек) | `90` |
| `DB_HOST` | Хост БД для тестов | `localhost` |
| `DB_PORT` | Порт БД для тестов | `5432` |
| `DB_USER` | Пользователь БД | `admin` |
| `DB_PASSWORD` | Пароль БД | `password` |
| `DB_NAME` | Имя БД | `scud` |

### Пример .env файла

```bash
# .env
DB_HOST=localhost
DB_PORT=5432
DB_USER=admin
DB_PASSWORD=secure_password
DB_NAME=scud
APP_PORT=8000
```

---

## Запуск и отладка

### Docker Compose (единственный поддерживаемый способ)

```bash
cd backend
docker compose up --build user_api
```

Сервис будет доступен на `http://localhost:8000`.

> **Локальный запуск без Docker не поддерживается.** Backend-сервисы
> зависят от пакетов из сертифицированного `main` Astra Linux apt
>. `pip install` из PyPI ставит
> библиотеки с другими версиями и не сертифицирован — `requirements.txt`
> содержит indicative versions для IDE-автокомплита, но production-сборка
> идёт через `apt install` в `Dockerfile`.
>
> Тесты тоже только в Docker:
> ```bash
> cd backend && docker compose --profile tests up --build user_api_tests
> ```

### Отладка

#### Логирование

Используйте стандартный модуль `logging`:

```python
import logging

logger = logging.getLogger(__name__)

async def my_handler(self):
    logger.info(f"Processing request for user {self.current_user.id}")
    logger.debug(f"Request body: {self.get_json_body()}")
```

#### Подключение отладчика

Для PyCharm/VSCode добавьте breakpoint:

```python
async def post(self):
    import pdb; pdb.set_trace()  # Breakpoint
    # ...
```

#### Просмотр SQL запросов

Включите логирование в psycopg2:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('psycopg2').setLevel(logging.DEBUG)
```

---

## Best Practices

### Соглашения по именованию

#### Файлы и модули
- `snake_case` для файлов: `user_repository.py`
- Один класс на файл (обычно)

#### Классы
- `PascalCase`: `UserRepository`, `BaseHandler`
- Суффикс `Handler` для HTTP обработчиков
- Суффикс `Repository` для репозиториев

#### Функции и методы
- `snake_case`: `get_by_id()`, `create_user()`
- Префикс `get_` для получения данных
- Префикс `create_` для создания
- Префикс `update_` для обновления
- Префикс `delete_` или `deactivate_` для удаления

#### Переменные
- `snake_case`: `user_id`, `created_at`
- Не используйте однобуквенные имена (кроме `i`, `j` в циклах)

### Структура кода

#### Порядок импортов

```python
# 1. Стандартная библиотека
from __future__ import annotations
import os
from datetime import datetime
from typing import Optional

# 2. Сторонние библиотеки
import tornado.web
from dacite import from_dict

# 3. Локальные модули
from .base import BaseHandler
from ..user_data.db.repositories.user import UserRepository
```

#### Порядок методов в классе

```python
class MyHandler(BaseHandler):
    # 1. __init__ и инициализация
    def initialize(self, db):
        ...

    # 2. HTTP методы в порядке: GET, POST, PUT, PATCH, DELETE
    async def get(self):
        ...

    async def post(self):
        ...

    # 3. Вспомогательные методы
    def _validate_input(self):
        ...
```

### Работа с async/await

#### ✅ DO

```python
# Используйте await для асинхронных вызовов
user = await repo.get_by_id(user_id)

# Используйте async def для асинхронных функций
async def my_handler(self):
    ...

# Обрабатывайте ошибки
try:
    await db.execute(query)
except AsyncPostgresError as e:
    logger.error(f"DB error: {e}")
```

#### ❌ DON'T

```python
# НЕ забывайте await
user = repo.get_by_id(user_id)  # ❌ Вернет coroutine, а не User!

# НЕ используйте sync код в async функциях
async def handler(self):
    time.sleep(1)  # ❌ Блокирует event loop!
    # Используйте:
    await asyncio.sleep(1)  # ✅

# НЕ делайте CPU-intensive операции в async функциях
async def handler(self):
    result = heavy_computation()  # ❌ Блокирует event loop!
    # Используйте ThreadPoolExecutor или ProcessPoolExecutor
```

### Безопасность

#### ✅ DO

```python
# Параметризованные запросы
await db.fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))

# Валидация входных данных
if not validate_phone(phone):
    raise ValueError("Invalid phone")

# Проверка авторизации
if not self.current_user:
    self.write_json({"error": "Unauthorized"}, status_code=401)
    return
```

#### ❌ DON'T

```python
# SQL injection!
await db.fetch_one(f"SELECT * FROM users WHERE id = {user_id}")

# Отсутствие валидации
user = await repo.create(phone=phone, ...)  # phone может быть любым!

# Отсутствие проверки прав
await repo.delete(user_id)  # Кто угодно может удалить!
```

### Производительность

- Используйте `fetch_all()` с LIMIT для больших выборок
- Избегайте N+1 запросов (используйте JOIN)
- Добавляйте индексы для полей в WHERE
- Используйте connection pooling (уже настроен в AsyncPostgres)

### Тестирование

- Пишите тесты для каждого нового эндпоинта
- Unit-тесты для логики, integration для сценариев
- Покрытие кода > 80%
- Проверяйте edge cases (пустые данные, null, ошибки БД)

---

## Полезные ссылки

- [Tornado Documentation](https://www.tornadoweb.org/en/stable/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [pytest Documentation](https://docs.pytest.org/)
- [OpenAPI Specification](https://swagger.io/specification/)
- [Python Dataclasses](https://docs.python.org/3/library/dataclasses.html)
- [dacite](https://github.com/konradhalas/dacite)

---

## Поддержка

При возникновении вопросов или проблем:

1. Проверьте это руководство
2. Изучите существующий код (User, Contractor - хорошие примеры)
3. Посмотрите тесты в `tests/`
4. Обратитесь к команде разработки

---

**Версия документа**: 1.0
**Последнее обновление**: 2026-02-16
