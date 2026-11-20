"""Разбор device-топиков: общая схема и per-id (ПИВ v2).

Прошивки разных ревизий публикуют либо в общий `stat`, либо в `stat/{id}`.
Бэкенд обязан принимать обе и использовать суффикс как проверку подлинности.
"""
from app.mqtt.relay_protocol import (
    TOPIC_PHOTOS,
    TOPIC_REP,
    TOPIC_STAT,
    parse_device_topic,
)


class TestSharedScheme:
    def test_stat_without_suffix(self):
        assert parse_device_topic("stat") == (TOPIC_STAT, None)

    def test_rep_without_suffix(self):
        assert parse_device_topic("rep") == (TOPIC_REP, None)

    def test_photos_without_suffix(self):
        assert parse_device_topic("photos") == (TOPIC_PHOTOS, None)


class TestPerIdScheme:
    def test_stat_with_id(self):
        assert parse_device_topic("stat/1001") == (TOPIC_STAT, 1001)

    def test_rep_with_id(self):
        assert parse_device_topic("rep/7") == (TOPIC_REP, 7)

    def test_photos_with_id(self):
        assert parse_device_topic("photos/42") == (TOPIC_PHOTOS, 42)


class TestRejected:
    def test_unknown_kind(self):
        assert parse_device_topic("telemetry/1") is None

    def test_command_topic_is_not_a_device_publication(self):
        # com/{id} сервер публикует сам; на приёме он ничего не значит.
        assert parse_device_topic("com/1001") is None

    def test_non_numeric_suffix(self):
        assert parse_device_topic("stat/abc") is None

    def test_leading_zero_suffix(self):
        # Протокол: id — целое без ведущих нулей.
        assert parse_device_topic("stat/007") is None

    def test_negative_suffix(self):
        assert parse_device_topic("stat/-1") is None

    def test_extra_level(self):
        assert parse_device_topic("stat/1001/extra") is None

    def test_empty_suffix(self):
        assert parse_device_topic("stat/") is None

    def test_sys_topic(self):
        assert parse_device_topic("$SYS/broker/uptime") is None
