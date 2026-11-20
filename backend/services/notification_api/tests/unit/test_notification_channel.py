from shared.enums import NotificationChannel


def test_notification_channel_values():
    assert NotificationChannel.MAX == "max"
    assert NotificationChannel.SMS == "sms"
    assert {c.value for c in NotificationChannel} == {"max", "sms"}
