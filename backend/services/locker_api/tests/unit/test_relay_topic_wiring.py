"""Обвязка вокруг двух схем топиков: подписки, приоритет rep, ACL."""
from app.mqtt.provisioning import acl_block
from app.mqtt.relay_protocol import device_subscriptions, is_photos_topic, is_rep_topic


class TestSubscriptions:
    def test_covers_shared_scheme(self):
        assert set(device_subscriptions()) >= {"stat", "rep", "photos"}

    def test_covers_per_id_scheme(self):
        assert set(device_subscriptions()) >= {"stat/+", "rep/+", "photos/+"}

    def test_uses_single_level_wildcard_only(self):
        # `stat/#` собирал бы произвольную вложенность.
        assert not any(t.endswith("#") for t in device_subscriptions())

    def test_has_no_duplicates(self):
        subs = device_subscriptions()
        assert len(subs) == len(set(subs))


class TestRepPriority:
    """rep освобождён от сброса в bulkhead — иначе теряются ответы на команды."""

    def test_shared_rep_is_priority(self):
        assert is_rep_topic("rep") is True

    def test_per_id_rep_is_priority(self):
        assert is_rep_topic("rep/1001") is True

    def test_stat_is_not_priority(self):
        assert is_rep_topic("stat") is False

    def test_per_id_stat_is_not_priority(self):
        assert is_rep_topic("stat/1001") is False


class TestPhotosLane:
    def test_shared_photos(self):
        assert is_photos_topic("photos") is True

    def test_per_id_photos(self):
        assert is_photos_topic("photos/123") is True

    def test_stat_and_rep_are_not_photos(self):
        assert is_photos_topic("stat") is False
        assert is_photos_topic("rep/1") is False

    def test_garbage_is_not_photos(self):
        assert is_photos_topic("photos/x") is False


class TestAcl:
    def test_grants_per_id_write_topics(self):
        block = acl_block(7)
        assert "topic write stat/7\n" in block
        assert "topic write rep/7\n" in block
        assert "topic write photos/7\n" in block

    def test_keeps_shared_write_for_older_firmware(self):
        block = acl_block(7)
        assert "topic write stat\n" in block
        assert "topic write rep\n" in block
        assert "topic write photos\n" in block

    def test_still_reads_only_own_command_topic(self):
        assert "topic read com/7\n" in acl_block(7)
