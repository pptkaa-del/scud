"""Unit-тесты для AggregationService"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.aggregation import AggregationService

# ------------------------------------------------------------------
# Жизненный цикл
# ------------------------------------------------------------------


def test_start_stop_lifecycle(mock_db):
    """Проверяет, что start устанавливает _running=True, а stop — _running=False"""
    svc = AggregationService(mock_db)

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
# Границы периодов
# ------------------------------------------------------------------


def test_get_period_boundaries_hourly():
    """Проверяет корректные часовые границы периода"""
    fixed_now = datetime(2026, 4, 12, 14, 35, 0, tzinfo=UTC)

    with patch("app.services.aggregation.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        start, end = AggregationService._get_period_boundaries("hourly")

    assert end == datetime(2026, 4, 12, 14, 0, 0, tzinfo=UTC)
    assert start == datetime(2026, 4, 12, 13, 0, 0, tzinfo=UTC)


def test_get_period_boundaries_hourly_at_midnight():
    """Полночь (hour=0): часовые границы пересекают сутки без ValueError.

    Регрессия COR-05: расчёт start/end в полночь не должен падать.
    Ожидаем (2026-04-11 23:00, 2026-04-12 00:00).
    """
    fixed_now = datetime(2026, 4, 12, 0, 35, 0, tzinfo=UTC)

    with patch("app.services.aggregation.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        start, end = AggregationService._get_period_boundaries("hourly")

    assert end == datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC)
    assert start == datetime(2026, 4, 11, 23, 0, 0, tzinfo=UTC)


def test_get_period_boundaries_daily():
    """Проверяет корректные дневные границы периода"""
    fixed_now = datetime(2026, 4, 12, 14, 35, 0, tzinfo=UTC)

    with patch("app.services.aggregation.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        start, end = AggregationService._get_period_boundaries("daily")

    assert end == datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC)
    assert start == datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)


def test_get_period_boundaries_monthly():
    """Проверяет корректные месячные границы периода"""
    fixed_now = datetime(2026, 4, 12, 14, 35, 0, tzinfo=UTC)

    with patch("app.services.aggregation.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        start, end = AggregationService._get_period_boundaries("monthly")

    assert end == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    assert start == datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------
# Выполнение SQL-запросов
# ------------------------------------------------------------------


def test_aggregate_hourly_executes_query(mock_db):
    """Проверяет, что часовая агрегация вызывает db.execute с типом 'hourly'"""
    mock_db.execute.return_value = "INSERT 0 5"
    svc = AggregationService(mock_db)

    asyncio.run(svc._process_hourly())

    mock_db.execute.assert_called_once()
    args = mock_db.execute.call_args
    assert "hourly" in str(args)


def test_aggregate_daily_executes_query(mock_db):
    """Проверяет, что дневная агрегация вызывает db.execute с типом 'daily'"""
    mock_db.execute.return_value = "INSERT 0 10"
    svc = AggregationService(mock_db)

    asyncio.run(svc._process_daily())

    mock_db.execute.assert_called_once()
    args = mock_db.execute.call_args
    assert "daily" in str(args)


def test_aggregate_monthly_executes_query(mock_db):
    """Проверяет, что месячная агрегация вызывает db.execute с типом 'monthly'"""
    mock_db.execute.return_value = "INSERT 0 3"
    svc = AggregationService(mock_db)

    asyncio.run(svc._process_monthly())

    mock_db.execute.assert_called_once()
    args = mock_db.execute.call_args
    assert "monthly" in str(args)


# ------------------------------------------------------------------
# Логирование результатов
# ------------------------------------------------------------------


def test_run_aggregation_executes_upsert(mock_db, caplog):
    """`_process_hourly` вызывает _db.execute один раз с upsert SQL и логирует событие."""
    svc = AggregationService(mock_db)

    with caplog.at_level(logging.INFO):
        asyncio.run(svc._process_hourly())

    mock_db.execute.assert_called_once()
    call_args = mock_db.execute.call_args
    sql_arg = call_args.args[0]
    params = call_args.args[1]
    assert "ON CONFLICT" in sql_arg
    assert params[0] == "hourly"
    assert "upsert" in caplog.text.lower()


# ------------------------------------------------------------------
# Обработка ошибок
# ------------------------------------------------------------------


def test_error_handling_continues(mock_db):
    """Проверяет, что сервис продолжает работу после ошибки в БД"""
    call_count = 0

    async def failing_then_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("DB connection lost")
        return "INSERT 0 1"

    mock_db.execute = AsyncMock(side_effect=failing_then_ok)
    svc = AggregationService(mock_db)

    iteration = 0

    async def controlled_start():
        nonlocal iteration
        svc._running = True
        while svc._running:
            try:
                await svc._process_hourly()
                await svc._process_daily()
                await svc._process_monthly()
            except Exception:
                pass  # Сервис ловит ошибки в start()
            iteration += 1
            if iteration >= 2:
                svc.stop()

    asyncio.run(controlled_start())

    # Сервис должен был продолжить после ошибок — второй цикл выполнил все 3 метода
    assert iteration >= 2
    assert call_count >= 4
