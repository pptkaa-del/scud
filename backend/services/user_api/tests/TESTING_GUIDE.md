# Руководство по тестированию

Это руководство описывает структуру тестов проекта User API и правила добавления новых тестов.

> 💡 **Примечание**: Это руководство фокусируется только на тестировании. Для общей информации о разработке см. [../DEVELOPER_GUIDE.md](../DEVELOPER_GUIDE.md).

## Структура тестов

Тесты разделены на два основных типа:

```
tests/
├── unit/                          # Unit-тесты с моками
│   ├── __init__.py
│   ├── conftest.py                # Общие фикстуры для unit-тестов
│   ├── test_user_api.py           # Тесты API пользователей
│   └── test_contractor_api.py     # Тесты API подрядчиков
│
├── integration/                   # Интеграционные тесты
│   ├── __init__.py
│   ├── conftest.py                # Фикстуры для интеграционных тестов
│   ├── test_user_integration.py   # Интеграционные тесты пользователей
│   └── test_contractor_integration.py  # Интеграционные тесты подрядчиков
│
└── __init__.py
```

## Типы тестов

### Unit-тесты (`tests/unit/`)

**Назначение**: Тестируют логику обработчиков в изоляции с использованием моков базы данных.

**Когда использовать**:
- Тестирование логики обработки запросов
- Проверка валидации данных
- Тестирование обработки ошибок
- Быстрые тесты, не требующие реальной БД

**Ключевые особенности**:
- Используют `AsyncHTTPTestCase` из Tornado
- Мокируют базу данных через `MagicMock` и `AsyncMock`
- Быстро выполняются (без реальных запросов к БД)
- Изолированы друг от друга

### Интеграционные тесты (`tests/integration/`)

**Назначение**: Тестируют полный цикл работы API с реальной базой данных и HTTP-запросами.

**Когда использовать**:
- Тестирование полного цикла CRUD операций
- Проверка взаимодействия компонентов
- Тестирование сценариев использования
- Проверка работы с реальной БД

**Ключевые особенности**:
- Используют библиотеку `requests` для HTTP-запросов
- Работают с реальной БД (требуют настройки через переменные окружения)
- Медленнее unit-тестов
- Требуют подготовки данных (фикстура `setup_test_data`)

## Запуск тестов

### Все тесты
```bash
pytest
```

### Только unit-тесты
```bash
pytest tests/unit/
```

### Только интеграционные тесты
```bash
pytest tests/integration/
```

### Конкретный файл
```bash
pytest tests/unit/test_user_api.py
```

### Конкретный тест
```bash
pytest tests/unit/test_user_api.py::TestUserApi::test_create_user_success
```

### С выводом результатов
```bash
pytest -v
```

## Добавление новых тестов

### Добавление unit-теста

1. **Выберите подходящий файл**:
   - Для API пользователей → `tests/unit/test_user_api.py`
   - Для API подрядчиков → `tests/unit/test_contractor_api.py`
   - Для нового модуля → создайте новый файл `test_<module>_api.py`

2. **Создайте тест в классе**:
```python
def test_your_feature(self):
    """Краткое описание того, что проверяет тест"""
    # Настройка моков
    self.db_mock.fetch_one.return_value = self.auth_user_row

    # Выполнение запроса
    response = self.fetch("/api/v1/endpoint", method="POST",
                         body=json.dumps({"key": "value"}),
                         headers={"X-Remote-User": "1"})

    # Проверки
    self.assertEqual(response.code, 200)
    data = json.loads(response.body)
    self.assertEqual(data["key"], "expected_value")
```

3. **Используйте фикстуры из `conftest.py`**:
   - `db_mock` - мок базы данных
   - `auth_user_row` - данные аутентифицированного пользователя

### Добавление интеграционного теста

1. **Выберите подходящий файл**:
   - Для API пользователей → `tests/integration/test_user_integration.py`
   - Для API подрядчиков → `tests/integration/test_contractor_integration.py`
   - Для нового модуля → создайте новый файл `test_<module>_integration.py`

2. **Создайте функцию теста**:
```python
def test_your_integration_scenario(api_client):
    """Краткое описание интеграционного сценария"""
    # Создание данных
    payload = {"key": "value"}
    response = api_client.post("/api/v1/endpoint", json=payload)
    assert response.status_code == 201

    # Проверка созданных данных
    created_id = response.json()["id"]
    response = api_client.get(f"/api/v1/endpoint/{created_id}")
    assert response.status_code == 200

    # Очистка (если нужно)
    response = api_client.delete(f"/api/v1/endpoint/{created_id}")
    assert response.status_code == 204
```

3. **Используйте фикстуры из `conftest.py`**:
   - `api_client` - клиент с настроенными заголовками авторизации
   - `wait_for_api` - ожидание готовности API (автоматически)

## Соглашения по именованию

### Классы тестов (unit)
- Формат: `Test<Entity>Api`
- Примеры: `TestUserApi`, `TestContractorApi`

### Функции тестов
- Формат: `test_<action>_<scenario>`
- Примеры:
  - `test_create_user_success` - успешное создание
  - `test_create_user_conflict` - создание с конфликтом
  - `test_get_user_not_found` - получение несуществующего объекта
  - `test_unauthorized_missing_header` - проверка авторизации

### Файлы тестов
- Unit: `test_<module>_api.py`
- Integration: `test_<module>_integration.py`

## Настройка окружения для интеграционных тестов

Интеграционные тесты используют переменные окружения для подключения к БД:

```bash
export TEST_BASE_URL="http://localhost:8000"  # URL API
export DB_HOST="localhost"                     # Хост БД
export DB_PORT="5432"                          # Порт БД
export DB_USER="admin"                         # Пользователь БД
export DB_PASSWORD="password"                  # Пароль БД
export DB_NAME="scud"                          # Имя БД
export TEST_API_TIMEOUT="90"                   # Таймаут ожидания API (сек)
```

## Паттерны тестирования

### Тестирование валидации

**Unit-тест**:
```python
def test_create_user_invalid_phone(self):
    """Тест: невалидный формат телефона"""
    self.db_mock.fetch_one.return_value = self.auth_user_row

    body = {"phone": "invalid", "full_name": "Test"}
    response = self.fetch("/api/v1/users", method="POST",
                         body=json.dumps(body),
                         headers={"X-Remote-User": "1"})

    self.assertEqual(response.code, 400)
    data = json.loads(response.body)
    self.assertEqual(data["error"], "Invalid phone format")
```

### Тестирование CRUD цикла

**Интеграционный тест**:
```python
def test_entity_lifecycle(api_client):
    """Полный CRUD цикл для сущности"""
    # Create
    response = api_client.post("/api/v1/entities", json={"name": "Test"})
    assert response.status_code == 201
    entity_id = response.json()["id"]

    # Read
    response = api_client.get(f"/api/v1/entities/{entity_id}")
    assert response.status_code == 200

    # Update
    response = api_client.put(f"/api/v1/entities/{entity_id}",
                             json={"name": "Updated"})
    assert response.status_code == 200

    # Delete
    response = api_client.delete(f"/api/v1/entities/{entity_id}")
    assert response.status_code == 204
```

### Тестирование авторизации

**Unit-тест**:
```python
def test_unauthorized_missing_header(self):
    """Тест: отсутствие заголовка авторизации"""
    response = self.fetch("/api/v1/users/1", method="GET")
    self.assertEqual(response.code, 401)
```

### Мокирование множественных запросов к БД

**Unit-тест**:
```python
def test_with_multiple_db_calls(self):
    """Тест с несколькими вызовами БД"""
    self.db_mock.fetch_one.side_effect = [
        self.auth_user_row,  # Первый вызов - авторизация
        {"id": 2, "name": "Entity"},  # Второй вызов - данные
        None  # Третий вызов - не найдено
    ]
    # ... код теста
```

## Отладка тестов

### Вывод отладочной информации
```python
def test_debug_example(api_client):
    response = api_client.get("/api/v1/users")
    print(f"Status: {response.status_code}")
    print(f"Body: {response.json()}")
    assert response.status_code == 200
```

Запуск с выводом print:
```bash
pytest -s tests/integration/test_user_integration.py::test_debug_example
```

### Остановка на первой ошибке
```bash
pytest -x
```

### Запуск последних неудачных тестов
```bash
pytest --lf
```

## Частые проблемы

### "Database not ready" в интеграционных тестах
**Решение**: Увеличьте `TEST_API_TIMEOUT` или убедитесь, что БД запущена и доступна.

### "Connection refused" в интеграционных тестах
**Решение**: Убедитесь, что API запущен на `TEST_BASE_URL`.

### Тесты не изолированы (влияют друг на друга)
**Решение**: Убедитесь, что каждый тест очищает созданные данные или используйте уникальные значения (например, UUID в телефонных номерах).

### Мок не возвращает нужные данные
**Решение**: Используйте `side_effect` для множественных вызовов:
```python
self.db_mock.fetch_one.side_effect = [value1, value2, value3]
```

## Полезные команды

```bash
# Запуск с покрытием кода
pytest --cov=app --cov-report=html

# Запуск параллельно (требует pytest-xdist)
pytest -n auto

# Запуск с профилированием
pytest --durations=10

# Запуск с метками (если используются)
pytest -m "smoke"  # только smoke-тесты
pytest -m "not slow"  # все кроме медленных
```

## Контрольный список для добавления теста

- [ ] Выбран правильный тип теста (unit/integration)
- [ ] Выбран правильный файл или создан новый
- [ ] Имя теста следует соглашениям
- [ ] Добавлен docstring с описанием теста
- [ ] Используются фикстуры из conftest.py
- [ ] Тест проверяет один конкретный сценарий
- [ ] Добавлены проверки (assertions) для всех важных условий
- [ ] Тест запускается успешно: `pytest path/to/test_file.py::test_name`
- [ ] Тест не влияет на другие тесты (изолирован)

## Дополнительные ресурсы

- [Документация pytest](https://docs.pytest.org/)
- [Документация Tornado Testing](https://www.tornadoweb.org/en/stable/testing.html)
- [Документация unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
- [Документация requests](https://requests.readthedocs.io/)
