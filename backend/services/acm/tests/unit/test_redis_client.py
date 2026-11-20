"""Unit-тесты для shared.redis_client.

Тесты используют mock TCP-сервер на asyncio.start_server для проверки
RESP протокола: GET / SET / DEL / PING + reconnect + error handling.
"""
import asyncio
import unittest

from shared.redis_client import RedisClient, RedisConnectionError, RedisError


class MockRedisServer:
    """Mock TCP server, отвечающий заданными RESP-ответами в порядке очереди."""

    def __init__(self, responses: list[bytes]):
        self._responses = list(responses)
        self.received: list[bytes] = []
        self._server = None
        self.port = 0
        self._handler_task = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.received.append(data)
                if self._responses:
                    writer.write(self._responses.pop(0))
                    await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()


class TestRedisClient(unittest.TestCase):
    def test_get_returns_value(self):
        async def run():
            server = MockRedisServer([b"$5\r\nhello\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                result = await client.get("greeting")
                self.assertEqual(result, b"hello")
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_get_missing_key_returns_none(self):
        async def run():
            server = MockRedisServer([b"$-1\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                result = await client.get("missing")
                self.assertIsNone(result)
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_set_without_ex(self):
        async def run():
            server = MockRedisServer([b"+OK\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                await client.set("k", "v")
                expected = b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n"
                self.assertEqual(server.received[0], expected)
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_set_with_ex_encodes_correctly(self):
        async def run():
            server = MockRedisServer([b"+OK\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                await client.set("k", "v", ex=30)
                expected = b"*5\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n$2\r\nEX\r\n$2\r\n30\r\n"
                self.assertEqual(server.received[0], expected)
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_set_accepts_bytes_value(self):
        async def run():
            server = MockRedisServer([b"+OK\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                await client.set("k", b"\x00\x01binary")
                # Verify binary roundtrip in argument encoding
                self.assertIn(b"\x00\x01binary", server.received[0])
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_delete_returns_count(self):
        async def run():
            server = MockRedisServer([b":2\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                count = await client.delete("a", "b")
                self.assertEqual(count, 2)
                expected = b"*3\r\n$3\r\nDEL\r\n$1\r\na\r\n$1\r\nb\r\n"
                self.assertEqual(server.received[0], expected)
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_delete_no_keys_short_circuits(self):
        async def run():
            client = RedisClient("127.0.0.1", 65000)  # unreachable port
            # Should not connect at all
            count = await client.delete()
            self.assertEqual(count, 0)
        asyncio.run(run())

    def test_ping_returns_true(self):
        async def run():
            server = MockRedisServer([b"+PONG\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                self.assertTrue(await client.ping())
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_error_reply_raises_redis_error(self):
        async def run():
            server = MockRedisServer([b"-ERR something bad\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                with self.assertRaises(RedisError) as ctx:
                    await client.get("k")
                self.assertIn("something bad", str(ctx.exception))
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_connection_to_unreachable_raises(self):
        async def run():
            client = RedisClient("127.0.0.1", 1, connect_timeout=0.5)
            with self.assertRaises(RedisConnectionError):
                await client.get("k")
        asyncio.run(run())

    def test_from_url_parses_correctly(self):
        c1 = RedisClient.from_url("redis://localhost:6379/0")
        self.assertEqual(c1._host, "localhost")
        self.assertEqual(c1._port, 6379)
        self.assertIsNone(c1._password)

        c2 = RedisClient.from_url("redis://:secret@example.com")
        self.assertEqual(c2._host, "example.com")
        self.assertEqual(c2._port, 6379)
        self.assertEqual(c2._password, "secret")

        c3 = RedisClient.from_url("redis://redis:6380")
        self.assertEqual(c3._host, "redis")
        self.assertEqual(c3._port, 6380)

    def test_from_url_rejects_invalid_scheme(self):
        with self.assertRaises(ValueError):
            RedisClient.from_url("http://example.com")

    def test_stalled_command_times_out_and_does_not_hang(self):
        """COR-23: redis принимает TCP-соединение, но никогда не отвечает на
        команду. Без command_timeout корутина зависла бы навсегда под self._lock,
        заблокировав всех последующих вызывающих. С таймаутом — поднимается
        RedisError за <= command_timeout, соединение сбрасывается."""
        async def run():
            # Сервер, который принимает соединение, но НЕ отвечает.
            server = MockRedisServer([])  # пустой список ответов → молчит
            await server.start()
            try:
                client = RedisClient(
                    "127.0.0.1", server.port, command_timeout=0.3,
                )
                start = asyncio.get_event_loop().time()
                with self.assertRaises(RedisError):
                    await client.get("k")
                elapsed = asyncio.get_event_loop().time() - start
                # Не зависли: вернулись примерно за command_timeout (с запасом).
                self.assertLess(elapsed, 2.0)
                # Соединение сброшено — следующий вызов переподключается.
                self.assertIsNone(client._writer)
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())

    def test_command_timeout_resets_connection_for_next_caller(self):
        """COR-23: после таймаута битое соединение сброшено, и следующий вызов
        к здоровому серверу проходит (не заклинен на отравленном connection)."""
        async def run():
            # Первый сервер молчит → таймаут. Второй отвечает корректно.
            silent = MockRedisServer([])
            await silent.start()
            healthy = MockRedisServer([b"$5\r\nhello\r\n"])
            await healthy.start()
            try:
                client = RedisClient(
                    "127.0.0.1", silent.port, command_timeout=0.3,
                )
                with self.assertRaises(RedisError):
                    await client.get("k")
                # Перенаправляем клиента на здоровый сервер (имитация рестарта redis
                # на том же endpoint): reconnect должен дать рабочий ответ.
                client._port = healthy.port
                result = await client.get("k")
                self.assertEqual(result, b"hello")
                await client.close()
            finally:
                await silent.stop()
                await healthy.stop()
        asyncio.run(run())

    def test_set_then_get_roundtrip(self):
        async def run():
            server = MockRedisServer([b"+OK\r\n", b"$5\r\nworld\r\n"])
            await server.start()
            try:
                client = RedisClient("127.0.0.1", server.port)
                await client.set("hello", "world", ex=60)
                result = await client.get("hello")
                self.assertEqual(result, b"world")
                # Two separate commands sent
                self.assertEqual(len(server.received), 2)
                await client.close()
            finally:
                await server.stop()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
