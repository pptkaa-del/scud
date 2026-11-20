"""
Интеграционные тесты reports_api через Apache Gateway.

Все запросы идут через http://apache_gateway (порт 80) с cookie-аутентификацией.
Apache Lua вызывает ACM перед каждым запросом — тесты проверяют полный стек:
  client → Apache → ACM → reports_api

Покрывают:
- Ролевой контроль: worker не может обращаться к report-эндпоинтам (403)
- Ролевой контроль: operator может создавать/читать отчёты (202/200)
- Маршрутизацию ProxyPass: /api/v1/reports и /api/v1/report/

Важно: тесты НЕ ждут, пока отчёт перейдёт в статус `ready` — это требует
данных history_api и тайминга воркера (покрыто интеграцией reports_api).
Здесь проверяются только маршрутизация и ACM-авторизация.
"""
from conftest import GATEWAY_URL

# Валидное тело для report_type=locker_open (см. LockerOpenGenerator.validate_params)
_VALID_REPORT_BODY = {
    "report_type": "locker_open",
    "params": {
        "date_from": "2026-01-01T00:00:00",
        "date_to": "2026-02-01T00:00:00",
    },
}


class TestReportsACMAccess:
    """Ролевой контроль для report-эндпоинтов: floor — OPERATOR."""

    def test_worker_denied_create_report(self, worker_session):
        """Worker → POST /api/v1/reports → 403 от ACM."""
        session, _ = worker_session
        resp = session.post(f"{GATEWAY_URL}/api/v1/reports", json=_VALID_REPORT_BODY)
        assert resp.status_code == 403, (
            f"Worker should be denied create report, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["status"] == 403
        assert data["title"] == "Forbidden"

    def test_worker_denied_list_reports(self, worker_session):
        """Worker → GET /api/v1/reports → 403 от ACM."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/reports")
        assert resp.status_code == 403, (
            f"Worker should be denied list reports, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["status"] == 403
        assert data["title"] == "Forbidden"


class TestReportsRouting:
    """Маршрутизация и operator-доступ: POST→202, GET→200, GET by id→200."""

    def test_operator_create_and_list_report(self, operator_session):
        """Operator → POST /api/v1/reports → 202; GET /api/v1/reports → 200 с items."""
        session, _ = operator_session

        create = session.post(f"{GATEWAY_URL}/api/v1/reports", json=_VALID_REPORT_BODY)
        assert create.status_code == 202, (
            f"Operator should create report (202), got {create.status_code}: {create.text}"
        )
        created = create.json()
        report_id = created["id"]
        assert created["report_type"] == "locker_open"
        assert created["status"] == "pending"

        listing = session.get(f"{GATEWAY_URL}/api/v1/reports")
        assert listing.status_code == 200, (
            f"Operator should list reports (200), got {listing.status_code}: {listing.text}"
        )
        body = listing.json()
        assert isinstance(body["items"], list)
        assert any(item["id"] == report_id for item in body["items"]), (
            "Created report not present in listing"
        )

        # GET /api/v1/report/{id} — инстанс через ProxyPass /api/v1/report/
        detail = session.get(f"{GATEWAY_URL}/api/v1/report/{report_id}")
        assert detail.status_code == 200, (
            f"Operator should get report detail (200), got {detail.status_code}: {detail.text}"
        )
        assert detail.json()["id"] == report_id
