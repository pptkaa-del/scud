"""Интеграционный round-trip тест reports_api.

Полный путь async-пайплайна:
1. сидируем `command_history` в scud_history (3 open+executed в окне + 1 close);
2. через reports_api ставим задачу `locker_open` → 202;
3. поллим GET /api/v1/report/{id} пока status == 'ready' (worker отработал);
4. GET .../download → 200, парсим .xlsx (zip), проверяем 3 инв.номера присутствуют,
   маркер не-open строки отсутствует;
5. DELETE /api/v1/report/{id} → 204;
6. GET .../download → 410 (файл удалён, задача expired).

Поллинг ограничен по времени — зависший worker валит тест, а не висит вечно.
"""

from __future__ import annotations

import io
import time
import uuid
import zipfile

from .conftest import insert_command

# Окно отчёта.
_DATE_FROM = "2026-09-10T00:00:00+00:00"
_DATE_TO = "2026-09-10T23:59:59+00:00"
_POLL_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.5


def _xlsx_text(content: bytes) -> str:
    """Собрать весь XML-текст из .xlsx (zip) для подстрочного поиска значений.

    pylightxl может писать строки как inline или через sharedStrings — конкатенация
    всех частей делает проверку устойчивой к выбору механизма.
    """
    text_parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.endswith(".xml"):
                text_parts.append(zf.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(text_parts)


def test_full_report_roundtrip(api_client):
    # Уникальные маркеры, чтобы не пересечься с данными из других тестов.
    tag = uuid.uuid4().hex[:8]
    inv_numbers = [f"INV-{tag}-A", f"INV-{tag}-B", f"INV-{tag}-C"]
    close_inv = f"INV-{tag}-CLOSE"

    matching = [
        ("2026-09-10T08:00:00+00:00", 11, inv_numbers[0], "Анна А."),
        ("2026-09-10T12:00:00+00:00", 12, inv_numbers[1], "Борис Б."),
        ("2026-09-10T20:00:00+00:00", 13, inv_numbers[2], "Вера В."),
    ]
    for executed_at, created_by, inv, full_name in matching:
        insert_command(
            command_type="open",
            status="executed",
            executed_at=executed_at,
            locker_inventory_number=inv,
            created_by=created_by,
            user_full_name=full_name,
        )
    # Не-open строка — должна быть отфильтрована history_api и не попасть в отчёт.
    insert_command(
        command_type="close",
        status="executed",
        executed_at="2026-09-10T13:00:00+00:00",
        locker_inventory_number=close_inv,
        created_by=99,
        user_full_name="Закрывающий З.",
    )

    # 1. Поставить задачу.
    resp = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
        },
    )
    assert resp.status_code == 202, resp.text
    report_id = resp.json()["id"]

    # 2. Поллить до ready (worker генерирует асинхронно).
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        poll = api_client.get(f"/api/v1/report/{report_id}")
        assert poll.status_code == 200, poll.text
        status = poll.json()["status"]
        if status == "ready":
            break
        assert status in ("pending", "running"), f"unexpected status: {status}"
        time.sleep(_POLL_INTERVAL_S)
    assert status == "ready", f"report not ready within {_POLL_TIMEOUT_S}s (last={status})"

    # 3. Скачать и проверить содержимое.
    dl = api_client.get(f"/api/v1/report/{report_id}/download")
    assert dl.status_code == 200, dl.text
    assert "spreadsheet" in dl.headers["Content-Type"]

    text = _xlsx_text(dl.content)
    for inv in inv_numbers:
        assert inv in text, f"{inv} missing from report xlsx"
    assert close_inv not in text, "non-open row leaked into report"

    # 4. Удалить.
    delete = api_client.delete(f"/api/v1/report/{report_id}")
    assert delete.status_code == 204, delete.text

    # 5. После удаления скачивание → 410.
    gone = api_client.get(f"/api/v1/report/{report_id}/download")
    assert gone.status_code == 410, gone.text


def test_create_with_name_then_rename(api_client):
    """name опционален при создании и меняется через PATCH /report/{id}."""
    resp = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
            "name": "Исходное имя",
        },
    )
    assert resp.status_code == 202, resp.text
    report_id = resp.json()["id"]
    assert resp.json()["name"] == "Исходное имя"

    # GET отражает имя.
    got = api_client.get(f"/api/v1/report/{report_id}")
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "Исходное имя"

    # PATCH меняет имя.
    patched = api_client.patch(
        f"/api/v1/report/{report_id}", json={"name": "Новое имя"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Новое имя"

    # GET отражает новое имя.
    got2 = api_client.get(f"/api/v1/report/{report_id}")
    assert got2.status_code == 200, got2.text
    assert got2.json()["name"] == "Новое имя"

    # Очистка.
    api_client.delete(f"/api/v1/report/{report_id}")


def test_create_without_name_then_set(api_client):
    """Без name при создании поле = null, затем задаётся через PATCH."""
    resp = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
        },
    )
    assert resp.status_code == 202, resp.text
    report_id = resp.json()["id"]
    assert resp.json()["name"] is None

    patched = api_client.patch(
        f"/api/v1/report/{report_id}", json={"name": "Заданное позже"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Заданное позже"

    api_client.delete(f"/api/v1/report/{report_id}")


def test_search_reports_by_name(api_client):
    """GET /api/v1/reports?q=... фильтрует по подстроке имени (без учёта регистра)."""
    tag = uuid.uuid4().hex[:8]
    alpha_name = f"Alpha-{tag}"
    beta_name = f"Beta-{tag}"

    alpha = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
            "name": alpha_name,
        },
    )
    assert alpha.status_code == 202, alpha.text
    alpha_id = alpha.json()["id"]

    beta = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
            "name": beta_name,
        },
    )
    assert beta.status_code == 202, beta.text
    beta_id = beta.json()["id"]

    try:
        # Точное совпадение подстроки → только Alpha.
        found = api_client.get(f"/api/v1/reports?q={alpha_name}")
        assert found.status_code == 200, found.text
        ids = {item["id"] for item in found.json()["items"]}
        assert alpha_id in ids
        assert beta_id not in ids

        # Регистронезависимость: lowercase 'alpha' тоже находит Alpha.
        ci = api_client.get(f"/api/v1/reports?q=alpha-{tag}")
        assert ci.status_code == 200, ci.text
        ci_ids = {item["id"] for item in ci.json()["items"]}
        assert alpha_id in ci_ids
        assert beta_id not in ci_ids
    finally:
        api_client.delete(f"/api/v1/report/{alpha_id}")
        api_client.delete(f"/api/v1/report/{beta_id}")


def test_create_rejects_arbitrary_params_400(api_client):
    """Произвольные ключи в params → 400, задача не ставится в очередь.

    Защита от инъекции произвольных данных в JSONB-колонку report_jobs.params:
    клиент не может протащить ничего сверх объявленного набора ключей генератора.
    """
    before = api_client.get("/api/v1/reports?limit=1")
    assert before.status_code == 200, before.text
    total_before = before.json()["total"]

    resp = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {
                "date_from": _DATE_FROM,
                "date_to": _DATE_TO,
                "evil": "x" * 100000,
            },
        },
    )
    assert resp.status_code == 400, resp.text
    assert "evil" in resp.json()["detail"]

    # Ни одна задача не должна быть создана из отклонённого запроса.
    after = api_client.get("/api/v1/reports?limit=1")
    assert after.status_code == 200, after.text
    assert after.json()["total"] == total_before


def test_patch_unknown_report_404(api_client):
    """PATCH несуществующего отчёта → 404."""
    resp = api_client.patch(
        f"/api/v1/report/{uuid.uuid4()}", json={"name": "x"}
    )
    assert resp.status_code == 404, resp.text


def test_patch_empty_name_400(api_client):
    """PATCH с пустым именем → 400 (валидация)."""
    resp = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
        },
    )
    assert resp.status_code == 202, resp.text
    report_id = resp.json()["id"]

    bad = api_client.patch(f"/api/v1/report/{report_id}", json={"name": "   "})
    assert bad.status_code == 400, bad.text

    api_client.delete(f"/api/v1/report/{report_id}")
