"""
Сценарий: E2E-отчёт locker_open на релейном MQTT-двойнике.

Проверяет полный стек gateway → locker_api → релейный двойник (RelayRunner) →
history_api → reports_api:

1. Создаём 6 mqtt-шкафов и поднимаем по одному релейному двойнику на каждый.
2. Создаём 4 worker-пользователя, выдаём каждому доступ к своей группе шкафов
   и шлём от их имени по 4 команды open на каждый шкаф группы (6×4 = 24).
3. Дожидаемся, пока history_api зафиксирует все 24 executed open-события.
4. Заказываем отчёт locker_open за окно [now-1h, now+1h].
5. Поллим задачу до status=ready (генерацию выполняет встроенный воркер
   reports_api, REPORT_WORKER_INTERVAL_S=5).
6. Скачиваем .xlsx и проверяем содержимое: заголовки, ≥24 строк данных,
   ≥6 различных инвентарных номеров (включая наши 6), сквозную нумерацию
   № п/п (1..N), заполненность колонки ФИО (ключевая регрессия —
   ФИО резолвится из user_api по created_by) и разнообразие авторов
   (≥4 различных created_by / ФИО, все созданные ФИО присутствуют).

Зависимости:
- RelayRunner: релейный двойник, исполняющий команды через com/{relay_id}
- locker_api: приём команд, запись command_history через history_api
- history_api: command-history (/internal/api/v1/commands/history)
- reports_api: генерация .xlsx, резолв ФИО через user_api
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from io import BytesIO

from openpyxl import load_workbook

from utils.api_client import GatewayClient, unique_phone
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.wait import wait_for

logger = logging.getLogger(__name__)

# Масштаб выбран так, чтобы тик command-queue (.env.fast: 2с) на 24 командах
# укладывался в несколько итераций, а инварианты отчёта (разные инв. номера,
# разные ФИО) проверялись на нетривиальном наборе.
_LOCKER_COUNT = 6
_OPENS_PER_LOCKER = 4
_EXPECTED_OPENS = _LOCKER_COUNT * _OPENS_PER_LOCKER  # 24

# Несколько разных пользователей-исполнителей — для проверки резолва
# created_by → ФИО на нескольких людях.
_USER_COUNT = 4
_USER_PASSWORD = "workerpass_systest1"
_USER_NAME_PREFIX = "Оператор Тестовый"

_EXPECTED_HEADERS = [
    "№ п/п",
    "ID пользователя",
    "Инв. номер шкафа",
    "ФИО",
    "Время открытия",
]

_ONLINE_TIMEOUT_S = 60.0
_OPENS_TIMEOUT_S = 90.0
_REPORT_READY_TIMEOUT_S = 90.0

_XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class TestLockerOpenReport:
    """E2E-отчёт по открытиям шкафов на релейном двойнике (6 шкафов / 24 open)."""

    def test_locker_open_report_at_volume(
        self,
        gateway: GatewayClient,
        save_artifact,
    ):
        provisioned_locker_ids: list[str] = []
        report_id: str | None = None
        created_user_ids: list[int] = []
        user_clients: list[GatewayClient] = []

        try:
            with ExitStack() as relay_stack:
                # ── 1. Создать 6 релейных шкафов и поднять двойники ─────────
                locker_ids: list[str] = []
                inventory_numbers: set[str] = set()
                for _ in range(_LOCKER_COUNT):
                    relay_id = _random_relay_id()
                    locker = _create_mqtt_locker(gateway, relay_id)
                    provisioned_locker_ids.append(locker["id"])
                    locker_ids.append(locker["id"])
                    inventory_numbers.add(locker["inventory_number"])
                    runner = relay_stack.enter_context(RelayRunner(relay_id))
                    runner.publish_stat()
                logger.info(
                    "Created %d relay lockers, %d distinct inventory numbers",
                    len(locker_ids), len(inventory_numbers),
                )

                wait_for(
                    lambda: all(
                        gateway.get_connectivity(lid) == "online" for lid in locker_ids
                    ),
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"not all {_LOCKER_COUNT} lockers came online",
                )
                logger.info("All %d lockers are online", len(locker_ids))

                # ── 2a. Создать _USER_COUNT worker-пользователей и залогинить их ──
                window_start = datetime.now(timezone.utc) - timedelta(hours=1)
                grant_end = datetime.now(timezone.utc) + timedelta(days=1)

                created_full_names: list[str] = []
                for i in range(1, _USER_COUNT + 1):
                    full_name = f"{_USER_NAME_PREFIX} {i}"
                    phone = unique_phone()
                    user = gateway.create_user(
                        phone=phone,
                        full_name=full_name,
                        password=_USER_PASSWORD,
                        role="worker",
                    )
                    user_id = user["id"]
                    created_user_ids.append(user_id)
                    created_full_names.append(full_name)

                    # 2FA по умолчанию включена — снимаем до логина паролем.
                    gateway.disable_2fa(user_id)
                    client = GatewayClient.login(gateway._base, phone, _USER_PASSWORD)
                    user_clients.append(client)
                    logger.info(
                        "Created worker user: id=%s name=%s", user_id, full_name
                    )

                # ── 2b. Раздать 6 шкафов пользователям round-robin ──────────
                # Распределение по остатку индекса — каждый шкаф попадает ровно
                # одному пользователю, покрытие всех 6 сохраняется при любом
                # соотношении _LOCKER_COUNT / _USER_COUNT.
                partitions: list[list[str]] = [[] for _ in range(_USER_COUNT)]
                for idx, lid in enumerate(locker_ids):
                    partitions[idx % _USER_COUNT].append(lid)
                assert sum(len(p) for p in partitions) == _LOCKER_COUNT
                assert {lid for p in partitions for lid in p} == set(locker_ids)

                # ── 2c. Выдать каждому пользователю доступ к его группе шкафов ──
                # Доступ выдаём ДО первой команды воркера: ACM кэширует контекст
                # пользователя на 30 с и не инвалидирует его при выдаче доступа,
                # поэтому первый запрос воркера должен идти уже после grant.
                for user_id, part in zip(created_user_ids, partitions, strict=True):
                    resp = gateway._session.post(
                        f"{gateway._base}/api/v1/user/{user_id}/accesses",
                        json={
                            "start_at": window_start.isoformat(),
                            "end_at": grant_end.isoformat(),
                            "full_access": False,
                            "locker_ids": part,
                        },
                        timeout=30,
                    )
                    assert resp.status_code == 201, (
                        f"grant access failed for user {user_id}: "
                        f"{resp.status_code} {resp.text}"
                    )
                    logger.info(
                        "Granted access: user_id=%s lockers=%d", user_id, len(part)
                    )

                # ── 2d. Каждый пользователь шлёт open по своей группе (6×4 = 24) ──
                for client, part in zip(user_clients, partitions, strict=True):
                    for lid in part:
                        for _ in range(_OPENS_PER_LOCKER):
                            client.send_command(lid, "open")
                logger.info(
                    "Issued %d open commands across %d users (%d lockers × %d)",
                    _EXPECTED_OPENS, _USER_COUNT, _LOCKER_COUNT, _OPENS_PER_LOCKER,
                )

                # ── 3. Дождаться, пока history_api зафиксирует все 24 executed open ──
                # Считаем per-locker (а не глобально), чтобы не словить executed
                # open-события от других сценариев, выполнявшихся ранее.
                def executed_open_count() -> int:
                    return sum(
                        len(gateway.get_command_history(
                            locker_id=lid, command_type="open",
                            status="executed", limit=50,
                        ))
                        for lid in locker_ids
                    )

                wait_for(
                    lambda: executed_open_count() >= _EXPECTED_OPENS,
                    timeout=_OPENS_TIMEOUT_S,
                    interval=3,
                    message=(
                        f"history did not record {_EXPECTED_OPENS} executed open events"
                    ),
                )
                logger.info("History recorded >= %d executed open events", _EXPECTED_OPENS)

            # ── Релейные двойники больше не нужны — отчёт читает history_api ──

            # ── 4. Заказать отчёт locker_open ──────────────────────────────
            window_end = datetime.now(timezone.utc) + timedelta(hours=1)
            job = gateway.create_report(
                "locker_open",
                params={
                    "date_from": window_start.isoformat(),
                    "date_to": window_end.isoformat(),
                },
            )
            report_id = job["id"]
            assert job["status"] in ("pending", "running"), job
            logger.info("Created report job: id=%s status=%s", report_id, job["status"])

            # ── 5. Поллить до ready ────────────────────────────────────────
            def report_ready() -> bool:
                detail = gateway.get_report(report_id)
                status = detail["status"]
                assert status != "failed", f"report generation failed: {detail}"
                return status == "ready"

            wait_for(
                report_ready,
                timeout=_REPORT_READY_TIMEOUT_S,
                interval=3,
                message="report did not reach ready",
            )
            detail = gateway.get_report(report_id)
            row_count = detail.get("row_count")
            logger.info("Report ready: row_count=%s", row_count)
            assert row_count is not None and row_count >= _EXPECTED_OPENS, (
                f"report row_count {row_count} < {_EXPECTED_OPENS}"
            )

            # ── 6. Скачать .xlsx и проверить содержимое ────────────────────
            resp = gateway.download_report(report_id)
            ctype = resp.headers.get("Content-Type", "")
            assert _XLSX_CONTENT_TYPE in ctype, f"unexpected Content-Type: {ctype}"
            assert resp.content, "downloaded report is empty"

            # Опционально сохранить .xlsx на хост (SAVE_ARTIFACTS) для просмотра.
            save_artifact(f"locker_open_report_{report_id}.xlsx", resp.content)

            wb = load_workbook(BytesIO(resp.content))
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            assert len(rows) >= 2, "xlsx must have title + header rows"

            # Строка 1 — объединённый заголовок отчёта (название + период).
            assert rows[0][0] and "Отчёт по открытию" in str(rows[0][0]), (
                f"title row missing or unexpected: {rows[0][0]!r}"
            )

            # Строка 2 — шапка таблицы; данные начинаются с 3-й строки.
            header = list(rows[1])
            assert header == _EXPECTED_HEADERS, (
                f"header mismatch: {header} != {_EXPECTED_HEADERS}"
            )

            data_rows = rows[2:]
            assert len(data_rows) >= _EXPECTED_OPENS, (
                f"expected >= {_EXPECTED_OPENS} data rows, got {len(data_rows)}"
            )

            # № п/п — сквозная нумерация 1..N.
            seq = [r[0] for r in data_rows]
            assert seq == list(range(1, len(data_rows) + 1)), (
                "№ п/п column is not sequential 1..N"
            )

            # ФИО (колонка 4) непустое для всех строк данных — ключевая регрессия.
            empty_names = [i for i, r in enumerate(data_rows) if not (r[3] or "").strip()]
            assert not empty_names, (
                f"ФИО must be populated for all rows; empty at rows {empty_names[:5]}"
            )

            # ≥6 различных инвентарных номеров, и наши 6 присутствуют.
            report_inventory = {r[2] for r in data_rows if r[2]}
            assert len(report_inventory) >= _LOCKER_COUNT, (
                f"expected >= {_LOCKER_COUNT} distinct inventory numbers, "
                f"got {len(report_inventory)}"
            )
            missing = inventory_numbers - report_inventory
            assert not missing, (
                f"created inventory numbers absent from report: {sorted(missing)[:5]}"
            )

            # ≥_USER_COUNT различных авторов (created_by, колонка 1) и ФИО (колонка 3).
            # ID из xlsx приходит как int или str — приводим к str для надёжности.
            report_user_ids = {str(r[1]) for r in data_rows if r[1] is not None}
            report_full_names = {(r[3] or "").strip() for r in data_rows if r[3]}
            assert len(report_user_ids) >= _USER_COUNT, (
                f"expected >= {_USER_COUNT} distinct created_by ids, "
                f"got {len(report_user_ids)}"
            )
            assert len(report_full_names) >= _USER_COUNT, (
                f"expected >= {_USER_COUNT} distinct ФИО, "
                f"got {len(report_full_names)}"
            )

            # Все созданные ФИО присутствуют в отчёте.
            missing_names = set(created_full_names) - report_full_names
            assert not missing_names, (
                f"created ФИО absent from report: {sorted(missing_names)}"
            )

            logger.info(
                "Report verified: %d data rows, %d distinct inventory numbers, "
                "%d distinct created_by ids, %d distinct ФИО, all ФИО populated",
                len(data_rows), len(report_inventory),
                len(report_user_ids), len(report_full_names),
            )

        finally:
            # ── 7. Очистка: удалить отчёт, пользователей и все созданные шкафы ──
            if report_id:
                try:
                    gateway.delete_report(report_id)
                except Exception as exc:
                    logger.warning("delete report %s failed: %s", report_id, exc)

            for client in user_clients:
                try:
                    client.logout()
                except Exception as exc:
                    logger.warning("logout worker client failed: %s", exc)

            for user_id in created_user_ids:
                try:
                    gateway.delete_user(user_id)
                except Exception as exc:
                    logger.warning("delete user %s failed: %s", user_id, exc)

            for lid in provisioned_locker_ids:
                try:
                    gateway.delete_locker(lid)
                except Exception as exc:
                    logger.warning("delete backend locker %s failed: %s", lid, exc)
