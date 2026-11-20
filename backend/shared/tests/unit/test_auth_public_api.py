"""Unit-тесты публичного API пакета ``shared.auth`` после декомпозиции
``base_handler`` на модули ``json_handler`` / ``problem_details`` /
``user_auth_mixin``.

Гарантируем, что:
- три публичных имени по-прежнему импортируются из ``shared.auth``;
- иерархия классов сохранена (``ProblemDetailsHandler`` — наследник
  ``JSONHandler``);
- consumer-класс ``class H(ProblemDetailsHandler, UserAuthMixin)`` собирается
  и инстанцируется через реальный Tornado-запрос (MRO + cooperative
  ``__init__`` целы);
- backward-compat shim ``shared.auth.base_handler`` всё ещё отдаёт классы.
"""

from __future__ import annotations

import json

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import JSONHandler, ProblemDetailsHandler, UserAuthMixin


class _ConsumerHandler(ProblemDetailsHandler, UserAuthMixin):
    """Замешивает оба публичных компонента — собирается без MRO-конфликта."""

    # Снимок состояния, выставленного cooperative ``__init__``/``prepare``,
    # для проверки из теста (инстанс handler'а недоступен снаружи харнеса).
    last_current_user_id: int | None = -1

    async def get(self) -> None:
        type(self).last_current_user_id = self.current_user_id
        self.write_json({"ok": True})


def test_public_names_importable() -> None:
    # Сам импорт сверху не должен падать; здесь фиксируем, что это классы.
    assert isinstance(JSONHandler, type)
    assert isinstance(ProblemDetailsHandler, type)
    assert isinstance(UserAuthMixin, type)


def test_problem_details_is_json_handler_subclass() -> None:
    assert issubclass(ProblemDetailsHandler, JSONHandler)


def test_consumer_handler_mro_is_consistent() -> None:
    # MRO собирается без TypeError: оба компонента совместимы в одном классе.
    assert issubclass(_ConsumerHandler, JSONHandler)
    assert issubclass(_ConsumerHandler, UserAuthMixin)


def test_backward_compat_shim_still_exports() -> None:
    from shared.auth.base_handler import (
        JSONHandler as ShimJSON,
        ProblemDetailsHandler as ShimProblem,
        UserAuthMixin as ShimMixin,
    )

    # Shim реэкспортирует те же объекты, что и пакет — не копии.
    assert ShimJSON is JSONHandler
    assert ShimProblem is ProblemDetailsHandler
    assert ShimMixin is UserAuthMixin


class ConsumerHandlerConstructTest(AsyncHTTPTestCase):
    """Через реальный запрос проверяем, что consumer-handler конструируется
    и cooperative ``__init__`` из ``UserAuthMixin`` отрабатывает
    (``current_user_id`` инициализирован дефолтом ``None``).

    Порядок баз здесь — ``(ProblemDetailsHandler, UserAuthMixin)``, как в
    примере из задачи. При таком MRO ``UserAuthMixin.prepare`` НЕ попадает в
    цепочку (``RequestHandler.prepare`` найден раньше), поэтому auth не
    выполняется — сервисные ``BaseHandler`` намеренно используют обратный
    порядок ``(UserAuthMixin, ProblemDetailsHandler)`` (см.
    test_prepare_metrics_chain). Здесь проверяется только сам факт сборки
    и инициализации mixin-атрибута дефолтом."""

    def get_app(self) -> Application:
        return Application([(r"/x", _ConsumerHandler, {"db": None})])

    def setUp(self) -> None:
        super().setUp()
        _ConsumerHandler.last_current_user_id = -1

    def test_constructs_and_serves(self) -> None:
        resp = self.fetch(
            "/x", method="GET", headers={"X-Remote-User": "5"},
        )
        assert resp.code == 200, resp.body
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        # cooperative __init__ инициализировал атрибут дефолтом None; при данном
        # MRO-порядке auth-prepare не запускается, поэтому id остаётся None.
        assert _ConsumerHandler.last_current_user_id is None
