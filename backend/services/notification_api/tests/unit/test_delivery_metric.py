def test_record_otp_delivery_never_raises_and_increments():
    from shared.metrics import otp_delivery_total, record_otp_delivery

    before = otp_delivery_total.labels(service="notification_api", channel="sms",
                                       outcome="sent")._value.get()
    record_otp_delivery("sms", "sent")
    after = otp_delivery_total.labels(service="notification_api", channel="sms",
                                      outcome="sent")._value.get()
    assert after == before + 1
    record_otp_delivery(None, None)  # type: ignore[arg-type]  # must not raise
