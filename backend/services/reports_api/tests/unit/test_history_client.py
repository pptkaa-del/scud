import asyncio
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from tornado.httpclient import HTTPError

from app.services.history_client import HistoryReportClient, OpenEvent
from shared.http.errors import UpstreamUnavailableError

_BASE = "http://history_api:8002"
_FROM = datetime(2026, 1, 1, 0, 0, 0)
_TO = datetime(2026, 2, 1, 0, 0, 0)


def _resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.body = json.dumps(payload).encode()
    return resp


def _full_row() -> dict:
    """Полная строка command_history (16 полей) — как отдаёт generic-эндпойнт.

    OpenEvent имеет лишь 4 поля; остальные ключи dacite должен проигнорировать.
    """
    return {
        "command_id": "11111111-1111-1111-1111-111111111111",
        "locker_id": "22222222-2222-2222-2222-222222222222",
        "locker_inventory_number": "INV-1",
        "command_type": "open",
        "created_by": 7,
        "user_full_name": "Иванов",
        "created_at": "2026-01-05T09:59:00",
        "status": "executed",
        "executed_at": "2026-01-05T10:00:00",
        "parameters": {"foo": "bar"},
        "result": {"ok": True},
        "id": 42,
        "error_message": None,
        "source": "device",
        "updated_at": "2026-01-05T10:00:01",
        "retries": 0,
    }


def test_fetch_open_events_parses_items():
    payload = {
        "items": [_full_row()],
        "total": 1,
        "limit": 1000,
        "offset": 0,
    }
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(payload))
    client = HistoryReportClient(_BASE, http=http)

    events = asyncio.run(client.fetch_open_events(_FROM, _TO, 1000, 0))

    assert len(events) == 1
    ev = events[0]
    # Полная строка (16 ключей) распарсилась в 4-польный OpenEvent — лишние
    # ключи (command_id, status, parameters, ...) dacite проигнорировал.
    assert isinstance(ev, OpenEvent)
    assert ev.created_by == 7
    assert ev.locker_inventory_number == "INV-1"
    assert ev.user_full_name == "Иванов"
    assert ev.executed_at == datetime(2026, 1, 5, 10, 0, 0)

    # Запрос ушёл на generic-эндпойнт с нужными фильтрами.
    http.fetch.assert_called_once()
    url = http.fetch.call_args.args[0]
    assert "/internal/api/v1/commands/history?" in url
    query = parse_qs(urlsplit(url).query)
    assert query["command_type"] == ["open"]
    assert query["status"] == ["executed"]
    assert query["order"] == ["executed_at_asc"]
    assert query["executed_from"] == [_FROM.isoformat()]
    assert query["executed_to"] == [_TO.isoformat()]


def test_fetch_open_events_empty():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp({"items": [], "total": 0, "limit": 1000, "offset": 0}))
    client = HistoryReportClient(_BASE, http=http)

    events = asyncio.run(client.fetch_open_events(_FROM, _TO, 1000, 0))

    assert events == []


def test_fetch_open_events_http_error_raises_upstream():
    http = MagicMock()
    http.fetch = AsyncMock(side_effect=HTTPError(503, "boom"))
    client = HistoryReportClient(_BASE, http=http)

    with pytest.raises(UpstreamUnavailableError) as exc_info:
        asyncio.run(client.fetch_open_events(_FROM, _TO, 1000, 0))

    assert exc_info.value.upstream == "history_api"
    assert exc_info.value.status == 503
