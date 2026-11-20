"""Unit-тесты LockerUpdateDTO (dacite.from_dict, тело PUT /api/v1/locker/{uuid}).

Семантика partial-update: поле отсутствует в теле -> None -> repo.update не
трогает соответствующую колонку. Проверяем, что dacite воспроизводит именно
эту семантику (а не подставляет случайные значения), а также что типовая
валидация (WrongTypeError) реально работает для некорректных типов.
"""
import unittest

from dacite import Config, from_dict
from dacite.exceptions import WrongTypeError

from app.handlers.locker import LockerUpdateDTO

# Тот же Config, что get_json_body_as использует по умолчанию для входящих тел
# (strict=True -> неизвестные ключи отвергаются 400 на уровне handler'а).
_STRICT_CONFIG = Config(strict=True)


class LockerUpdateDTOTest(unittest.TestCase):
    def test_partial_body_sets_only_provided_fields(self) -> None:
        dto = from_dict(
            LockerUpdateDTO,
            {"inventory_number": "СО №42", "location_lat": 55.75},
            config=_STRICT_CONFIG,
        )

        self.assertEqual(dto.inventory_number, "СО №42")
        self.assertEqual(dto.location_lat, 55.75)
        self.assertIsNone(dto.location_address)
        self.assertIsNone(dto.location_lon)
        self.assertIsNone(dto.description)

    def test_empty_body_all_fields_none(self) -> None:
        dto = from_dict(LockerUpdateDTO, {}, config=_STRICT_CONFIG)

        self.assertIsNone(dto.inventory_number)
        self.assertIsNone(dto.location_address)
        self.assertIsNone(dto.location_lat)
        self.assertIsNone(dto.location_lon)
        self.assertIsNone(dto.description)

    def test_wrong_type_for_location_lat_raises(self) -> None:
        with self.assertRaises(WrongTypeError):
            from_dict(
                LockerUpdateDTO,
                {"location_lat": "not-a-number"},
                config=_STRICT_CONFIG,
            )


if __name__ == "__main__":
    unittest.main()
