"""Minimal asyncio Redis client over RESP protocol.

Astra Linux main-repo не содержит python3-redis (только в extended).
Этот модуль реализует достаточный для проекта подмножество команд
поверх stdlib asyncio + socket: GET, SET (с EX), DEL, PING.

Один multiplexed connection на инстанс с auto-reconnect при разрывах.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Дефолтный таймаут на ОДИН обмен командой (write+read). Redis может принять
# TCP-соединение, но затем зависнуть (half-open сокет, перегруженный сервер) —
# без таймаута корутина встанет навсегда под self._lock, и все последующие
# вызывающие (ACM token-cache, user_api cache-invalidation) встанут в очередь
# за ней бесконечно (COR-23). Бюджет коротки́й: cache-операции должны быть
# суб-секундными; на превышении — RedisConnectionError, который caller'ы уже
# толерируют как cache-miss/handled-error (не зависание).
DEFAULT_COMMAND_TIMEOUT = 2.0


class RedisError(Exception):
    """Redis сервер вернул ошибку или нарушил протокол."""


class RedisConnectionError(RedisError):
    """Сетевой сбой при подключении к Redis."""


class RedisClient:
    """Asyncio Redis client поверх RESP. Один connection на инстанс."""

    def __init__(
        self,
        host: str,
        port: int = 6379,
        password: str | None = None,
        connect_timeout: float = 5.0,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisClient:
        u = urlparse(url)
        if u.scheme not in ("redis", "rediss"):
            raise ValueError(f"Unsupported Redis URL scheme: {u.scheme!r}")
        return cls(
            host=u.hostname or "localhost",
            port=u.port or 6379,
            password=u.password,
            **kwargs,
        )

    async def _connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._connect_timeout,
            )
        except (TimeoutError, OSError) as e:
            raise RedisConnectionError(f"Cannot connect to redis://{self._host}:{self._port}: {e}") from e
        if self._password:
            await self._write_command(b"AUTH", self._password.encode())
            reply = await self._read_reply()
            if reply != b"OK":
                raise RedisError(f"AUTH failed: {reply!r}")

    async def _ensure_connected(self) -> None:
        if self._writer is None or self._writer.is_closing():
            await self._connect()

    @staticmethod
    def _encode(*args: bytes) -> bytes:
        parts = [f"*{len(args)}\r\n".encode()]
        for arg in args:
            parts.append(f"${len(arg)}\r\n".encode())
            parts.append(arg)
            parts.append(b"\r\n")
        return b"".join(parts)

    async def _write_command(self, *args: bytes) -> None:
        assert self._writer is not None
        self._writer.write(self._encode(*args))
        await self._writer.drain()

    async def _read_reply(self) -> bytes | int | list[Any] | None:
        assert self._reader is not None
        line = await self._reader.readline()
        if not line:
            raise RedisConnectionError("Connection closed by server")
        prefix, body = line[:1], line[1:-2]
        if prefix == b"+":
            return body
        if prefix == b"-":
            raise RedisError(body.decode("utf-8", errors="replace"))
        if prefix == b":":
            return int(body)
        if prefix == b"$":
            length = int(body)
            if length == -1:
                return None
            data = await self._reader.readexactly(length + 2)
            return data[:-2]
        if prefix == b"*":
            count = int(body)
            if count == -1:
                return None
            return [await self._read_reply() for _ in range(count)]
        raise RedisError(f"Unknown RESP prefix: {prefix!r}")

    async def _execute(self, *args: bytes) -> bytes | int | list[Any] | None:
        async with self._lock:
            for attempt in range(2):
                try:
                    # Весь обмен (ensure-connect + write + read) под одним
                    # бюджетом времени: redis, принявший TCP но не отвечающий на
                    # команду, иначе подвесил бы корутину навсегда под self._lock
                    # и заблокировал всех последующих вызывающих (COR-23).
                    return await asyncio.wait_for(
                        self._exchange(*args),
                        timeout=self._command_timeout,
                    )
                except TimeoutError:
                    # Сбрасываем потенциально half-open соединение, чтобы
                    # следующий вызывающий переподключился, а не унаследовал
                    # зависший сокет.
                    await self._reset_connection()
                    if attempt == 0:
                        logger.warning(
                            "Redis command timed out after %.2fs, resetting connection",
                            self._command_timeout,
                        )
                        continue
                    raise RedisConnectionError(
                        f"Redis command timed out after {self._command_timeout}s",
                    ) from None
                except (ConnectionError, OSError, asyncio.IncompleteReadError, RedisConnectionError):
                    await self._reset_connection()
                    if attempt == 0:
                        continue
                    raise
            return None

    async def _exchange(self, *args: bytes) -> bytes | int | list[Any] | None:
        """Ensure-connect + write + read одной команды (под command_timeout)."""
        await self._ensure_connected()
        await self._write_command(*args)
        return await self._read_reply()

    async def _reset_connection(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._writer = None
        self._reader = None

    async def ping(self) -> bool:
        reply = await self._execute(b"PING")
        return reply == b"PONG"

    async def get(self, key: str) -> bytes | None:
        reply = await self._execute(b"GET", key.encode())
        if reply is None or isinstance(reply, bytes):
            return reply
        raise RedisError(f"GET expected bytes/nil, got {type(reply).__name__}")

    async def set(self, key: str, value: bytes | str, ex: int | None = None) -> None:
        val = value.encode() if isinstance(value, str) else value
        if ex is None:
            await self._execute(b"SET", key.encode(), val)
        else:
            await self._execute(b"SET", key.encode(), val, b"EX", str(ex).encode())

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        args = [b"DEL"] + [k.encode() for k in keys]
        reply = await self._execute(*args)
        if isinstance(reply, int):
            return reply
        if reply is None:
            return 0
        raise RedisError(f"DEL expected int, got {type(reply).__name__}")

    async def close(self) -> None:
        async with self._lock:
            await self._reset_connection()
