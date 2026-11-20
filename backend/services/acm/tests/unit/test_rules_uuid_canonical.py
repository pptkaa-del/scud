"""Регрессия SEC-02: канонизация locker-id в ACM-правилах.

Баг: worker без Допусков мог OPEN любой шкаф, передав не-канонический вид
UUID в пути (32-символьный без дефисов, в фигурных скобках). Узкий паттерн
``[0-9a-fA-F-]{36}`` не совпадал → is_locker_scoped возвращал None →
check_access ПРОПУСКАЛ проверку Допуска и проваливался в allow.

Фикс: is_locker_scoped захватывает сегмент широким паттерном и парсит через
uuid.UUID; непарсимый сегмент → deny (а не skip), парсимый → канонизируется
до lowercase-дефисного вида и сравнивается с (тоже канонизированным)
grant-списком.
"""
from app.rules import Role, check_access, is_locker_scoped

# Допуск worker'а есть на X, нет на Y.
LOCKER_X = "550e8400-e29b-41d4-a716-446655440000"
LOCKER_Y = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

# Не-канонические формы X (всё это Postgres uuid примет и резолвит в X).
X_DASHLESS = "550e8400e29b41d4a716446655440000"
X_BRACED = "{550e8400-e29b-41d4-a716-446655440000}"
X_UPPER = "550E8400-E29B-41D4-A716-446655440000"
Y_DASHLESS = "6ba7b8109dad11d180b400c04fd430c8"


def _check(path: str) -> bool:
    """Worker с Допуском только на X, без full_access."""
    allowed, _ = check_access(
        method="POST",
        path=path,
        role=Role.WORKER,
        locker_ids=[LOCKER_X],
        full_access=False,
    )
    return allowed


class TestIsLockerScopedCanonical:
    def test_canonical_dashed(self):
        assert is_locker_scoped(f"/api/v1/locker/{LOCKER_X}/commands") == LOCKER_X

    def test_dashless_canonicalized(self):
        assert is_locker_scoped(f"/api/v1/locker/{X_DASHLESS}/commands") == LOCKER_X

    def test_braced_canonicalized(self):
        assert is_locker_scoped(f"/api/v1/locker/{X_BRACED}/commands") == LOCKER_X

    def test_uppercase_canonicalized(self):
        assert is_locker_scoped(f"/api/v1/locker/{X_UPPER}/commands") == LOCKER_X

    def test_garbage_segment_returns_sentinel_not_none(self):
        # Непарсимый сегмент НЕ должен совпасть с None (что вело бы к skip);
        # должен быть отличимый от None deny-маркер.
        result = is_locker_scoped("/api/v1/locker/not-a-uuid/commands")
        assert result is not None
        # Маркер заведомо не равен канонизированному UUID.
        assert result != LOCKER_X

    def test_non_locker_path_is_none(self):
        assert is_locker_scoped("/api/v1/lockers") is None


class TestCheckAccessCanonicalGrant:
    def test_canonical_granted_allowed(self):
        assert _check(f"/api/v1/locker/{LOCKER_X}/commands") is True

    def test_dashless_granted_allowed(self):
        assert _check(f"/api/v1/locker/{X_DASHLESS}/commands") is True

    def test_braced_granted_allowed(self):
        assert _check(f"/api/v1/locker/{X_BRACED}/commands") is True

    def test_uppercase_granted_allowed(self):
        assert _check(f"/api/v1/locker/{X_UPPER}/commands") is True

    # --- ЭКСПЛОЙТ: не-granted шкаф через не-канонический UUID → DENY ---
    def test_canonical_not_granted_denied(self):
        assert _check(f"/api/v1/locker/{LOCKER_Y}/commands") is False

    def test_dashless_not_granted_denied(self):
        # KEY REGRESSION: без фикса dashless обходил grant-чек.
        assert _check(f"/api/v1/locker/{Y_DASHLESS}/commands") is False

    def test_garbage_segment_denied(self):
        assert _check("/api/v1/locker/not-a-uuid/commands") is False

    def test_get_dashless_not_granted_denied_idor(self):
        # Тот же трюк на GET-чтении (IDOR).
        allowed, _ = check_access(
            method="GET",
            path=f"/api/v1/locker/{Y_DASHLESS}",
            role=Role.WORKER,
            locker_ids=[LOCKER_X],
            full_access=False,
        )
        assert allowed is False

    def test_full_access_worker_allowed_anywhere(self):
        allowed, _ = check_access(
            method="POST",
            path=f"/api/v1/locker/{Y_DASHLESS}/commands",
            role=Role.WORKER,
            locker_ids=[],
            full_access=True,
        )
        assert allowed is True
