"""Unit-тесты для `ProblemDetailsHandler.get_int_path_arg` (SIMP-07).

Хелпер дедуплицирует повторяющийся блок «взять path/строковый аргумент,
`int()` его, при ValueError отдать 400 ProblemDetails». Проверяем:
- валидное целое → возвращается распарсенное значение, handler работает дальше;
- нецелое → 400 application/problem+json с предсказуемым detail (имя поля);
- ведущие/хвостовые пробелы и знак — поведение int() сохраняется.
"""

from __future__ import annotations

import json
import logging

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import ProblemDetailsHandler


class _IntArgHandler(ProblemDetailsHandler):
    async def get(self, raw: str) -> None:
        value = self.get_int_path_arg(raw, "widget_id")
        # До сюда выполнение доходит только при валидном int — при ошибке
        # хелпер сам пишет 400 и поднимает Finish.
        self.write_json({"value": value})


class GetIntPathArgTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application([
            (r"/widget/(.+)", _IntArgHandler, {"db": None}),
        ])

    def _fetch(self, path: str) -> tuple[int, str]:
        resp = self.fetch(path, method="GET")
        return resp.code, resp.body.decode("utf-8")

    def test_valid_int_returns_parsed_value(self) -> None:
        code, text = self._fetch("/widget/42")
        assert code == 200, text
        assert json.loads(text) == {"value": 42}

    def test_negative_int_is_parsed(self) -> None:
        code, text = self._fetch("/widget/-7")
        assert code == 200, text
        assert json.loads(text) == {"value": -7}

    def test_non_int_returns_400_problem_details(self) -> None:
        code, text = self._fetch("/widget/notanumber")
        assert code == 400, text
        problem = json.loads(text)
        assert problem["status"] == 400
        # Имя поля присутствует в detail — предсказуемое сообщение.
        assert "widget_id" in problem["detail"], problem["detail"]
        assert "integer" in problem["detail"], problem["detail"]

    def test_non_int_uses_problem_json_content_type(self) -> None:
        resp = self.fetch("/widget/oops", method="GET")
        assert resp.code == 400
        ctype = resp.headers.get("Content-Type", "")
        assert "application/problem+json" in ctype, ctype

    def test_raw_value_not_leaked_in_detail(self) -> None:
        # Сырое (потенциально вредоносное) значение не эхо-ится в detail —
        # detail формируется только из имени поля + generic-текста.
        code, text = self._fetch("/widget/%3Cscript%3E")
        assert code == 400, text
        problem = json.loads(text)
        assert "<script>" not in problem["detail"], problem["detail"]


logging.getLogger("shared.auth.problem_details").setLevel(logging.DEBUG)
