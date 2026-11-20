"""Unit-тесты для общего валидатора параметров отчёта.

`reject_unknown_params` — защита от инъекции произвольных данных в JSONB-колонку
`report_jobs.params`: запрос отчёта не должен протаскивать ключи сверх того
набора, что объявил генератор.
"""

import pytest

from app.reports.params import reject_unknown_params


def test_allowed_keys_pass():
    # Только разрешённые ключи (или их подмножество) — без исключения.
    reject_unknown_params(
        {"date_from": "x", "date_to": "y"}, {"date_from", "date_to"}
    )


def test_subset_of_allowed_passes():
    reject_unknown_params({"date_from": "x"}, {"date_from", "date_to"})


def test_empty_params_pass():
    reject_unknown_params({}, {"date_from", "date_to"})


def test_single_unknown_key_raises_and_names_it():
    with pytest.raises(ValueError, match="evil"):
        reject_unknown_params(
            {"date_from": "x", "evil": 1}, {"date_from", "date_to"}
        )


def test_key_matching_is_case_sensitive():
    # Точное совпадение: 'Date_From' — это НЕ 'date_from', значит неизвестный
    # ключ. Защита от будущей «удобной» нормализации регистра, открывающей обход.
    with pytest.raises(ValueError, match="Date_From"):
        reject_unknown_params({"Date_From": "x"}, {"date_from", "date_to"})


def test_multiple_unknown_keys_sorted_in_message():
    with pytest.raises(ValueError) as exc:
        reject_unknown_params(
            {"zeta": 1, "alpha": 2, "date_from": "x"}, {"date_from", "date_to"}
        )
    # Детерминированный порядок в сообщении для стабильных тестов/логов.
    assert "alpha" in str(exc.value)
    assert "zeta" in str(exc.value)
    assert str(exc.value).index("alpha") < str(exc.value).index("zeta")
