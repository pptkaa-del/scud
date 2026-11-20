"""Unit-тесты для RetentionService"""

import asyncio
from datetime import UTC, date, datetime, timezone
import logging
from unittest.mock import AsyncMock, call, patch

import pytest

from app.services.retention import RetentionService, _assert_safe_partition_name

# ------------------------------------------------------------------
# Жизненный цикл
# ------------------------------------------------------------------


def test_start_stop_lifecycle(mock_db):
    """Проверяет, что start устанавливает _running=True, а stop — _running=False"""
    svc = RetentionService(mock_db)

    assert svc._running is False

    async def run_and_stop():
        task = asyncio.create_task(svc.start(interval=1))
        await asyncio.sleep(0.05)
        assert svc._running is True
        svc.stop()
        await task

    asyncio.run(run_and_stop())
    assert svc._running is False


# ------------------------------------------------------------------
# Удаление старых партиций
# ------------------------------------------------------------------


def test_drop_old_partitions(mock_db):
    """Проверяет удаление устаревших партиций через DROP TABLE"""
    mock_db.fetch_all.return_value = [
        {"table_name": "sensor_telemetry_2022_01"},
        {"table_name": "sensor_telemetry_2022_02"},
        {"table_name": "sensor_telemetry_2026_01"},  # свежая — не удалять
    ]

    svc = RetentionService(mock_db, retention_months=24)

    # Фиксируем сейчас = 2026-04-12, cutoff = 2024-04-01
    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._drop_old_partitions())

    # Партиции 2022_01, 2022_02 < 2024-04 — должны быть удалены
    execute_calls = mock_db.execute.call_args_list
    drop_calls = [c for c in execute_calls if "DROP TABLE" in str(c)]
    assert len(drop_calls) == 2

    drop_sql = [str(c) for c in drop_calls]
    assert any("sensor_telemetry_2022_01" in s for s in drop_sql)
    assert any("sensor_telemetry_2022_02" in s for s in drop_sql)


def test_drop_old_partitions_none_to_drop(mock_db):
    """Проверяет корректную работу при отсутствии устаревших партиций"""
    mock_db.fetch_all.return_value = [
        {"table_name": "sensor_telemetry_2026_01"},
        {"table_name": "sensor_telemetry_2026_02"},
    ]

    svc = RetentionService(mock_db, retention_months=24)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._drop_old_partitions())

    # execute не должен вызываться для DROP
    execute_calls = mock_db.execute.call_args_list
    drop_calls = [c for c in execute_calls if "DROP TABLE" in str(c)]
    assert len(drop_calls) == 0


# ------------------------------------------------------------------
# Удаление старых строк
# ------------------------------------------------------------------


def test_delete_old_security_events(mock_db):
    """Проверяет выполнение DELETE для security_events"""
    svc = RetentionService(mock_db, retention_months=24)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._delete_old_rows())

    execute_calls = [str(c) for c in mock_db.execute.call_args_list]
    security_deletes = [c for c in execute_calls if "security_events" in c and "DELETE" in c]
    assert len(security_deletes) >= 1


def test_delete_old_command_history(mock_db):
    """Проверяет выполнение DELETE для command_history"""
    svc = RetentionService(mock_db, retention_months=24)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._delete_old_rows())

    execute_calls = [str(c) for c in mock_db.execute.call_args_list]
    cmd_deletes = [c for c in execute_calls if "command_history" in c and "DELETE" in c]
    assert len(cmd_deletes) >= 1


def test_delete_old_media(mock_db):
    """Проверяет удаление устаревших медиа-записей из locker_media (без COUNT)."""
    svc = RetentionService(mock_db, retention_months=24)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._delete_old_rows())

    execute_calls = [str(c) for c in mock_db.execute.call_args_list]
    media_deletes = [c for c in execute_calls if "locker_media" in c and "DELETE" in c]
    assert len(media_deletes) >= 1
    # COUNT-before-DELETE race устранён: fetch_one для locker_media не вызывается
    fetch_one_calls = [str(c) for c in mock_db.fetch_one.call_args_list]
    media_count_calls = [c for c in fetch_one_calls if "locker_media" in c and "COUNT" in c]
    assert len(media_count_calls) == 0


# ------------------------------------------------------------------
# COR-09: очистка DEFAULT-партиции
# ------------------------------------------------------------------


def test_delete_old_default_partition_rows(mock_db):
    """COR-09: retention DELETE-ит устаревшие строки из sensor_telemetry_default.

    DEFAULT-партиция ловит out-of-range строки и НЕ удаляется как целая
    партиция (_drop_old_partitions работает только с YYYY_MM). Без явного
    DELETE такие строки копятся вечно. Проверяем, что DELETE против
    sensor_telemetry_default выполняется с корректным cutoff по recorded_at.
    """
    svc = RetentionService(mock_db, retention_months=24)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._delete_old_rows())

    execute_calls = mock_db.execute.call_args_list
    default_deletes = [
        c for c in execute_calls
        if "sensor_telemetry_default" in str(c) and "DELETE" in str(c)
    ]
    assert len(default_deletes) >= 1, "DELETE против sensor_telemetry_default не выполнен"

    # cutoff = 2026-04 минус 24 месяца = 2024-04-01
    expected_cutoff = datetime(2024, 4, 1, 0, 0, 0, tzinfo=UTC)
    matched = False
    for c in default_deletes:
        sql = c.args[0]
        params = c.args[1]
        if "recorded_at" in sql and expected_cutoff in list(params):
            matched = True
            break
    assert matched, "DELETE default-партиции должен использовать cutoff по recorded_at"


# ------------------------------------------------------------------
# Создание будущих партиций
# ------------------------------------------------------------------


def test_create_future_partitions(mock_db):
    """Включает текущий месяц + LOOKAHEAD_MONTHS вперёд."""
    svc = RetentionService(mock_db)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._create_future_partitions())

    assert mock_db.execute.call_count == RetentionService.LOOKAHEAD_MONTHS + 1

    calls = mock_db.execute.call_args_list
    # Текущий апрель 2026 + 6 месяцев вперёд
    expected_months = [
        date(2026, 4, 1),
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]
    assert len(expected_months) == RetentionService.LOOKAHEAD_MONTHS + 1
    for i, expected_date in enumerate(expected_months):
        assert calls[i] == call(
            "SELECT create_monthly_partition(%s, %s)",
            ["sensor_telemetry", expected_date],
        )


def test_create_future_partitions_year_rollover(mock_db):
    """Переход через границу года: октябрь + 6 месяцев = до апреля следующего."""
    svc = RetentionService(mock_db)

    fixed_now = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._create_future_partitions())

    calls = mock_db.execute.call_args_list
    # Текущий октябрь 2026 + 6 месяцев вперёд = до апреля 2027
    expected = [
        date(2026, 10, 1),
        date(2026, 11, 1),
        date(2026, 12, 1),
        date(2027, 1, 1),
        date(2027, 2, 1),
        date(2027, 3, 1),
        date(2027, 4, 1),
    ]
    for i, expected_date in enumerate(expected):
        assert calls[i] == call(
            "SELECT create_monthly_partition(%s, %s)",
            ["sensor_telemetry", expected_date],
        )


# ------------------------------------------------------------------
# Обработка ошибок
# ------------------------------------------------------------------


def test_error_handling_continues(mock_db):
    """Проверяет, что сервис продолжает работу после ошибки"""
    call_count = 0

    async def failing_fetch_all(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("DB connection lost")
        return []

    mock_db.fetch_all = AsyncMock(side_effect=failing_fetch_all)
    svc = RetentionService(mock_db)

    iteration = 0

    async def controlled_start():
        nonlocal iteration
        svc._running = True
        while svc._running:
            try:
                await svc._drop_old_partitions()
                await svc._delete_old_rows()
                await svc._create_future_partitions()
            except Exception:
                pass
            iteration += 1
            if iteration >= 2:
                svc.stop()

    asyncio.run(controlled_start())

    assert iteration >= 2
    assert call_count >= 2


# ------------------------------------------------------------------
# Защита имени партиции от DDL injection
# ------------------------------------------------------------------


def test_assert_safe_partition_name_accepts_valid():
    """Валидное имя партиции проходит проверку без исключений."""
    _assert_safe_partition_name("sensor_telemetry_2026_04")
    _assert_safe_partition_name("sensor_telemetry_2099_12")


def test_assert_safe_partition_name_rejects_injection_attempts():
    """Произвольные имена и попытки инъекции должны отклоняться."""
    bad_names = [
        "users",
        "sensor_telemetry_2026_04; DROP TABLE users",
        "sensor_telemetry_2026_04--",
        "sensor_telemetry_2026_4",       # одна цифра в месяце
        "sensor_telemetry_26_04",        # двузначный год
        "sensor_telemetry__2026_04",
        "",
        "SENSOR_TELEMETRY_2026_04",      # uppercase
        "sensor_telemetry_2026_04 OR 1=1",
    ]
    for name in bad_names:
        try:
            _assert_safe_partition_name(name)
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for {name!r}")


# ------------------------------------------------------------------
# Конфигурация retention_months
# ------------------------------------------------------------------


def test_retention_months_config():
    """Проверяет корректное вычисление даты отсечки из retention_months"""
    mock_db = AsyncMock()

    # 12 месяцев назад от апреля 2026 = апрель 2025
    svc_12 = RetentionService(mock_db, retention_months=12)
    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)

    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        cutoff_12 = svc_12._get_cutoff_date()

    assert cutoff_12 == datetime(2025, 4, 1, 0, 0, 0, tzinfo=UTC)

    # 6 месяцев назад от апреля 2026 = октябрь 2025
    svc_6 = RetentionService(mock_db, retention_months=6)

    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        cutoff_6 = svc_6._get_cutoff_date()

    assert cutoff_6 == datetime(2025, 10, 1, 0, 0, 0, tzinfo=UTC)

    # 36 месяцев (3 года) назад от апреля 2026 = апрель 2023
    svc_36 = RetentionService(mock_db, retention_months=36)

    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        cutoff_36 = svc_36._get_cutoff_date()

    assert cutoff_36 == datetime(2023, 4, 1, 0, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------
# COR-06: изоляция цикла создания партиций
# ------------------------------------------------------------------


def test_create_future_partitions_isolated_per_month(mock_db):
    """COR-06: сбой создания ОДНОЙ партиции не прерывает остальные месяцы.

    До фикса общий try/except вокруг цикла обрывал создание на первом сбое,
    и последующие месяцы не создавались. Симулируем сбой на втором месяце
    (май 2026) и проверяем, что остальные месяцы всё равно создаются.
    """
    failing_date = date(2026, 5, 1)

    async def execute_side_effect(query, params):
        # params = ["sensor_telemetry", start_date]
        if params[1] == failing_date:
            raise RuntimeError("partition creation failed for May")

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)
    svc = RetentionService(mock_db)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Не должно поднимать исключение — сбой одного месяца изолирован.
        asyncio.run(svc._create_future_partitions())

    # Все LOOKAHEAD_MONTHS + 1 месяцев должны быть попытаны (включая после сбоя).
    attempted_dates = [c.args[1][1] for c in mock_db.execute.call_args_list]
    assert mock_db.execute.call_count == RetentionService.LOOKAHEAD_MONTHS + 1
    # Месяцы ПОСЛЕ сбойного мая должны присутствовать.
    assert date(2026, 6, 1) in attempted_dates
    assert date(2026, 10, 1) in attempted_dates


# ------------------------------------------------------------------
# Метрика сбоев создания партиций (scud_partition_create_failures_total)
# ------------------------------------------------------------------


def test_create_future_partitions_increments_metric_on_failure(mock_db):
    """Сбой одного месяца → inc_partition_create_failure вызывается ровно один раз
    с именем таблицы _PARTITIONED_TABLE ("sensor_telemetry").

    Проверяет, что метрика инкрементируется при каждом except-блоке,
    а не только при финальном summary.
    """
    from app.services.retention import _PARTITIONED_TABLE

    failing_date = date(2026, 5, 1)

    async def execute_side_effect(query, params):
        if params[1] == failing_date:
            raise RuntimeError("partition creation failed for May")

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)
    svc = RetentionService(mock_db)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt, \
         patch("app.services.retention.inc_partition_create_failure") as mock_inc:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._create_future_partitions())

    mock_inc.assert_called_once_with(_PARTITIONED_TABLE)


def test_create_future_partitions_no_metric_on_success(mock_db):
    """Все партиции создаются успешно → inc_partition_create_failure не вызывается."""
    mock_db.execute = AsyncMock(return_value=None)
    svc = RetentionService(mock_db)

    fixed_now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    with patch("app.services.retention.datetime") as mock_dt, \
         patch("app.services.retention.inc_partition_create_failure") as mock_inc:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        asyncio.run(svc._create_future_partitions())

    mock_inc.assert_not_called()
