"""Интеграционная IDOR-регрессия (SEC-06): owner-scoping отчётов.

Operator A создаёт отчёт; operator B не должен его видеть/скачивать/
переименовывать/удалять (→404), и в списке B чужая задача не появляется
(включая total). Администратор видит чужой отчёт и список всех.

Заголовки X-Remote-User / X-Remote-Role проставляются per-request — gateway
их инжектит после JWT-валидации, downstream доверяет (trust boundary).
"""

from __future__ import annotations

import uuid

_DATE_FROM = "2026-09-10T00:00:00+00:00"
_DATE_TO = "2026-09-10T23:59:59+00:00"

_OWNER_A = "5001"
_OWNER_B = "5002"
_ADMIN = "5003"

_HDR_A = {"X-Remote-User": _OWNER_A, "X-Remote-Role": "operator"}
_HDR_B = {"X-Remote-User": _OWNER_B, "X-Remote-Role": "operator"}
_HDR_ADMIN = {"X-Remote-User": _ADMIN, "X-Remote-Role": "administrator"}


def _create_report(api_client, headers: dict[str, str], name: str) -> str:
    resp = api_client.post(
        "/api/v1/reports",
        json={
            "report_type": "locker_open",
            "params": {"date_from": _DATE_FROM, "date_to": _DATE_TO},
            "name": name,
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


def test_operator_b_cannot_access_operator_a_report(api_client):
    """B не может GET/download/rename/delete отчёт A — все 404."""
    name = f"A-secret-{uuid.uuid4().hex[:8]}"
    report_id = _create_report(api_client, _HDR_A, name)
    try:
        # GET чужого → 404
        got = api_client.get(f"/api/v1/report/{report_id}", headers=_HDR_B)
        assert got.status_code == 404, got.text

        # download чужого → 404 (не 409/410 — само существование не раскрываем)
        dl = api_client.get(
            f"/api/v1/report/{report_id}/download", headers=_HDR_B
        )
        assert dl.status_code == 404, dl.text

        # rename чужого → 404
        patched = api_client.patch(
            f"/api/v1/report/{report_id}",
            json={"name": "hijacked"},
            headers=_HDR_B,
        )
        assert patched.status_code == 404, patched.text

        # delete чужого → 404
        deleted = api_client.delete(
            f"/api/v1/report/{report_id}", headers=_HDR_B
        )
        assert deleted.status_code == 404, deleted.text

        # Контроль: отчёт A не тронут — A всё ещё видит своё прежнее имя.
        a_view = api_client.get(f"/api/v1/report/{report_id}", headers=_HDR_A)
        assert a_view.status_code == 200, a_view.text
        assert a_view.json()["name"] == name
    finally:
        api_client.delete(f"/api/v1/report/{report_id}", headers=_HDR_A)


def test_operator_owner_accesses_own_report(api_client):
    """A имеет полный доступ к своему отчёту."""
    report_id = _create_report(
        api_client, _HDR_A, f"A-own-{uuid.uuid4().hex[:8]}"
    )
    try:
        got = api_client.get(f"/api/v1/report/{report_id}", headers=_HDR_A)
        assert got.status_code == 200, got.text

        new_name = "A-renamed"
        patched = api_client.patch(
            f"/api/v1/report/{report_id}",
            json={"name": new_name},
            headers=_HDR_A,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["name"] == new_name
    finally:
        api_client.delete(f"/api/v1/report/{report_id}", headers=_HDR_A)


def test_list_excludes_foreign_jobs_and_total(api_client):
    """Список B не содержит задачу A; total отражает только свои."""
    tag = uuid.uuid4().hex[:8]
    a_name = f"A-list-{tag}"
    b_name = f"B-list-{tag}"
    a_id = _create_report(api_client, _HDR_A, a_name)
    b_id = _create_report(api_client, _HDR_B, b_name)
    try:
        # B ищет по общему тегу — видит только свой отчёт.
        listed = api_client.get(f"/api/v1/reports?q={tag}", headers=_HDR_B)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        ids = {item["id"] for item in body["items"]}
        assert b_id in ids
        assert a_id not in ids
        # total не утекает чужие задачи: ровно одна (B) по этому тегу.
        assert body["total"] == 1
    finally:
        api_client.delete(f"/api/v1/report/{a_id}", headers=_HDR_A)
        api_client.delete(f"/api/v1/report/{b_id}", headers=_HDR_B)


def test_administrator_sees_foreign_report_and_list(api_client):
    """Администратор видит чужой отчёт и список всех (override owner-scope)."""
    tag = uuid.uuid4().hex[:8]
    a_name = f"A-admin-{tag}"
    b_name = f"B-admin-{tag}"
    a_id = _create_report(api_client, _HDR_A, a_name)
    b_id = _create_report(api_client, _HDR_B, b_name)
    try:
        # Админ читает чужой отчёт A.
        got = api_client.get(f"/api/v1/report/{a_id}", headers=_HDR_ADMIN)
        assert got.status_code == 200, got.text
        assert got.json()["name"] == a_name

        # Список админа по тегу содержит обе задачи.
        listed = api_client.get(
            f"/api/v1/reports?q={tag}", headers=_HDR_ADMIN
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        ids = {item["id"] for item in body["items"]}
        assert a_id in ids
        assert b_id in ids
        assert body["total"] == 2
    finally:
        api_client.delete(f"/api/v1/report/{a_id}", headers=_HDR_A)
        api_client.delete(f"/api/v1/report/{b_id}", headers=_HDR_B)
