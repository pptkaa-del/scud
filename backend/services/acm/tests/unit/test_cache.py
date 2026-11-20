"""
Unit-тесты для app.cache — Redis-backed кэш контекста пользователя.

Покрывают:
- set/get round-trip с правильной JSON-сериализацией
- get при отсутствии ключа → None
- invalidate → DEL
- защита от повреждённого JSON
- сохранение state/role/full_access/locker_ids
"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.cache import UserContext, UserContextCache
from shared.redis_client import RedisConnectionError, RedisError

LOCKER_UUID = "550e8400-e29b-41d4-a716-446655440000"


def make_ctx(
    user_id: int = 1,
    role: str = "worker",
    state: str = "active",
    full_access: bool = False,
) -> UserContext:
    return UserContext(
        user_id=user_id,
        role=role,
        state=state,
        full_access=full_access,
        locker_ids=(LOCKER_UUID,) if not full_access else (),
    )


def make_redis_mock() -> MagicMock:
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock(return_value=None)
    mock.delete = AsyncMock(return_value=0)
    return mock


class TestUserContextCache(unittest.TestCase):
    def test_set_calls_redis_set_with_json_payload(self):
        async def run():
            redis_mock = make_redis_mock()
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            await cache.set(make_ctx(user_id=1, role="operator"))
            redis_mock.set.assert_awaited_once()
            args, kwargs = redis_mock.set.await_args
            self.assertEqual(args[0], "user_ctx:1")
            payload = json.loads(args[1])
            self.assertEqual(payload["role"], "operator")
            self.assertEqual(payload["state"], "active")
            self.assertEqual(payload["full_access"], False)
            self.assertEqual(payload["locker_ids"], [LOCKER_UUID])
            self.assertEqual(kwargs.get("ex"), 30)
        asyncio.run(run())

    def test_get_returns_user_context(self):
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(
                return_value=b'{"role":"worker","state":"active","full_access":false,"locker_ids":["abc"]}'
            )
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            ctx = await cache.get(5)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.user_id, 5)
            self.assertEqual(ctx.role, "worker")
            self.assertEqual(ctx.state, "active")
            self.assertEqual(ctx.full_access, False)
            self.assertEqual(ctx.locker_ids, ("abc",))
            redis_mock.get.assert_awaited_once_with("user_ctx:5")
        asyncio.run(run())

    def test_get_missing_returns_none(self):
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(return_value=None)
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            self.assertIsNone(await cache.get(999))
        asyncio.run(run())

    def test_invalidate_calls_redis_delete(self):
        async def run():
            redis_mock = make_redis_mock()
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            await cache.invalidate(7)
            redis_mock.delete.assert_awaited_once_with("user_ctx:7")
        asyncio.run(run())

    def test_get_corrupt_json_returns_none(self):
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(return_value=b"not json")
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            self.assertIsNone(await cache.get(1))
        asyncio.run(run())

    def test_get_malformed_payload_returns_none(self):
        async def run():
            # Валидный JSON, но без обязательных ключей
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(return_value=b'{"role":"worker"}')
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            self.assertIsNone(await cache.get(1))
        asyncio.run(run())

    def test_set_then_get_roundtrip(self):
        async def run():
            stored: dict[str, bytes] = {}

            async def fake_set(key, val, ex=None):
                stored[key] = val.encode() if isinstance(val, str) else val

            async def fake_get(key):
                return stored.get(key)

            redis_mock = MagicMock()
            redis_mock.set = fake_set
            redis_mock.get = fake_get
            redis_mock.delete = AsyncMock(return_value=0)

            cache = UserContextCache(redis_mock, ttl_seconds=30)
            ctx = make_ctx(user_id=42, role="administrator", full_access=True)
            await cache.set(ctx)
            result = await cache.get(42)
            self.assertEqual(result, ctx)
        asyncio.run(run())

    def test_set_full_access_serializes_empty_locker_ids(self):
        async def run():
            redis_mock = make_redis_mock()
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            ctx = UserContext(
                user_id=10,
                role="operator",
                state="active",
                full_access=True,
                locker_ids=(),
            )
            await cache.set(ctx)
            args, _ = redis_mock.set.await_args
            payload = json.loads(args[1])
            self.assertTrue(payload["full_access"])
            self.assertEqual(payload["locker_ids"], [])
        asyncio.run(run())

    def test_state_field_is_preserved(self):
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(
                return_value=b'{"role":"worker","state":"deactivated","full_access":false,"locker_ids":[]}'
            )
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            ctx = await cache.get(7)
            self.assertEqual(ctx.state, "deactivated")
        asyncio.run(run())

    def test_ttl_passed_to_redis(self):
        async def run():
            redis_mock = make_redis_mock()
            cache = UserContextCache(redis_mock, ttl_seconds=120)
            await cache.set(make_ctx(user_id=1))
            _, kwargs = redis_mock.set.await_args
            self.assertEqual(kwargs.get("ex"), 120)
        asyncio.run(run())


class TestUserContextCacheSingleflight(unittest.TestCase):
    """get_or_load: в одно окно cache-miss только один loader идёт в БД."""

    def test_returns_cached_value_without_calling_loader(self):
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(
                return_value=b'{"role":"worker","state":"active","full_access":false,"locker_ids":[]}'
            )
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            calls = []

            async def loader(uid):
                calls.append(uid)
                raise AssertionError("loader must not be called on cache hit")

            ctx = await cache.get_or_load(7, loader)
            self.assertIsNotNone(ctx)
            self.assertEqual(calls, [])
        asyncio.run(run())

    def test_concurrent_misses_call_loader_once(self):
        async def run():
            stored: dict[str, bytes] = {}

            async def fake_set(key, val, ex=None):
                stored[key] = val.encode() if isinstance(val, str) else val

            async def fake_get(key):
                return stored.get(key)

            redis_mock = MagicMock()
            redis_mock.set = fake_set
            redis_mock.get = fake_get
            redis_mock.delete = AsyncMock(return_value=0)

            cache = UserContextCache(redis_mock, ttl_seconds=30)

            loader_calls = 0
            loader_proceed = asyncio.Event()

            async def slow_loader(uid):
                nonlocal loader_calls
                loader_calls += 1
                # Удерживаем loader, пока не наберём конкуренцию
                await loader_proceed.wait()
                return make_ctx(user_id=uid, role="worker")

            async def caller():
                return await cache.get_or_load(42, slow_loader)

            tasks = [asyncio.create_task(caller()) for _ in range(20)]
            # Дать всем 20 задачам начаться и упереться в singleflight
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            loader_proceed.set()
            results = await asyncio.gather(*tasks)

            self.assertEqual(loader_calls, 1, "loader должен вызваться один раз")
            for r in results:
                self.assertIsNotNone(r)
                self.assertEqual(r.user_id, 42)
        asyncio.run(run())

    def test_loader_returning_none_does_not_cache(self):
        async def run():
            redis_mock = make_redis_mock()
            cache = UserContextCache(redis_mock, ttl_seconds=30)

            async def loader(uid):
                return None

            ctx = await cache.get_or_load(99, loader)
            self.assertIsNone(ctx)
            redis_mock.set.assert_not_awaited()
        asyncio.run(run())

    def test_redis_get_error_falls_through_to_loader(self):
        """RedisConnectionError на GET → cache-miss: loader (БД) загружает, не raise."""
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(side_effect=RedisConnectionError("redis down"))
            cache = UserContextCache(redis_mock, ttl_seconds=30)

            loader_calls = 0

            async def loader(uid):
                nonlocal loader_calls
                loader_calls += 1
                return make_ctx(user_id=uid, role="operator")

            ctx = await cache.get_or_load(7, loader)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.user_id, 7)
            self.assertEqual(ctx.role, "operator")
            self.assertEqual(loader_calls, 1)
        asyncio.run(run())

    def test_redis_set_error_does_not_fail_request(self):
        """RedisError на SET (запись после загрузки из БД) → значение возвращается, не raise."""
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(return_value=None)
            redis_mock.set = AsyncMock(side_effect=RedisError("write failed"))
            cache = UserContextCache(redis_mock, ttl_seconds=30)

            async def loader(uid):
                return make_ctx(user_id=uid, role="worker")

            ctx = await cache.get_or_load(11, loader)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.user_id, 11)
            self.assertEqual(ctx.role, "worker")
        asyncio.run(run())

    def test_get_redis_error_returns_none(self):
        """Прямой get при RedisError → None (cache-miss), не raise."""
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.get = AsyncMock(side_effect=RedisConnectionError("redis down"))
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            self.assertIsNone(await cache.get(5))
        asyncio.run(run())

    def test_set_redis_error_swallowed(self):
        """Прямой set при RedisError → не raise."""
        async def run():
            redis_mock = make_redis_mock()
            redis_mock.set = AsyncMock(side_effect=RedisError("write failed"))
            cache = UserContextCache(redis_mock, ttl_seconds=30)
            await cache.set(make_ctx(user_id=1))  # не должно бросать
        asyncio.run(run())

    def test_loader_exception_propagates_and_cleans_pending(self):
        async def run():
            redis_mock = make_redis_mock()
            cache = UserContextCache(redis_mock, ttl_seconds=30)

            async def failing_loader(uid):
                raise RuntimeError("DB down")

            with self.assertRaises(RuntimeError):
                await cache.get_or_load(1, failing_loader)

            # Запись в _pending должна быть очищена — следующий вызов вновь идёт в loader
            self.assertNotIn(1, cache._pending)

            ok_calls = 0

            async def ok_loader(uid):
                nonlocal ok_calls
                ok_calls += 1
                return make_ctx(user_id=uid)

            await cache.get_or_load(1, ok_loader)
            self.assertEqual(ok_calls, 1)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
