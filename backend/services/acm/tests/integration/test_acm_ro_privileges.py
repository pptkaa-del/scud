"""
SEC-24: проверка least-privilege роли acm_ro в scud_users.

ACM — read-only клиент БД пользователей. Эти тесты подтверждают, что выделенная
LOGIN-роль acm_ro имеет ТОЛЬКО право SELECT:
- SELECT по существующей таблице успешен;
- любая запись (INSERT) и DDL (CREATE TABLE) падает с ошибкой недостатка прав
  (SQLSTATE 42501, insufficient_privilege).

Роль создаётся в init-scripts/init-user-db.sh при первом старте postgres_user.
"""
import psycopg2
import psycopg2.errors
import pytest

from .conftest import ACM_RO_DB

# SQLSTATE недостатка привилегий (insufficient_privilege).
INSUFFICIENT_PRIVILEGE = "42501"


def _acm_ro_conn():
    return psycopg2.connect(**ACM_RO_DB)


# autouse setup_test_data из conftest гарантирует, что таблицы scud_users
# существуют и заполнены к моменту этих тестов.


def test_acm_ro_can_select(setup_test_data):
    """acm_ro может читать существующую таблицу users."""
    conn = _acm_ro_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users LIMIT 1")
            # Достаточно того, что запрос не упал на правах; строки могут быть.
            cur.fetchall()
    finally:
        conn.close()


def test_acm_ro_cannot_insert(setup_test_data):
    """acm_ro НЕ может писать: INSERT падает с insufficient_privilege (42501)."""
    conn = _acm_ro_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                cur.execute(
                    """
                    INSERT INTO users (id, phone, full_name, state, role)
                    VALUES (999001, '+79009009001', 'SEC-24 RO Probe', 'active', 'worker')
                    """
                )
            assert exc_info.value.pgcode == INSUFFICIENT_PRIVILEGE
    finally:
        conn.rollback()
        conn.close()


def test_acm_ro_cannot_create_table(setup_test_data):
    """acm_ro НЕ может выполнять DDL: CREATE TABLE падает с 42501."""
    conn = _acm_ro_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                cur.execute("CREATE TABLE sec24_ro_probe (i INT)")
            assert exc_info.value.pgcode == INSUFFICIENT_PRIVILEGE
    finally:
        conn.rollback()
        conn.close()
