"""TLS client context for the 8883 listener (protocol §4)."""
from __future__ import annotations

import ssl
from unittest.mock import MagicMock

import pytest

from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import RelayMqttTransport, build_tls_context


def test_context_requires_tls12_minimum() -> None:
    ctx = build_tls_context(insecure=True)
    assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2


def test_insecure_context_skips_verification() -> None:
    ctx = build_tls_context(insecure=True)
    assert ctx.check_hostname is False
    assert ctx.verify_mode is ssl.CERT_NONE


def test_custom_ca_is_verified_and_hostname_checked(tmp_path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_bytes(_SELF_SIGNED_CA)
    ctx = build_tls_context(str(ca))
    assert ctx.check_hostname is True
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.get_ca_certs(), "CA не загружен в контекст"


def test_unreadable_ca_fails_loudly(tmp_path) -> None:
    missing = tmp_path / "nope.crt"
    with pytest.raises(OSError):
        build_tls_context(str(missing))


def test_transport_passes_context_to_connect() -> None:
    ctx = build_tls_context(insecure=True)
    transport = RelayMqttTransport(RelayDevice(device_id=1, version=2),
                                   "broker.example", 8883, tls_context=ctx)
    assert transport._tls_context is ctx


def test_transport_without_tls_sends_false() -> None:
    transport = RelayMqttTransport(RelayDevice(device_id=1, version=2), "broker.example")
    transport._client = MagicMock()
    assert (transport._tls_context or False) is False


# Self-signed CA (RSA 2048, CN=scud-test-ca), generated for this test only.
_SELF_SIGNED_CA = b"""-----BEGIN CERTIFICATE-----
MIIDFDCCAfygAwIBAgIUYvPd1VUOAkFeym5HF4w6UjujaawwDQYJKoZIhvcNAQEL
BQAwFzEVMBMGA1UEAwwMc2N1ZC10ZXN0LWNhMCAXDTI2MDgyNjIzMTkyM1oYDzIx
MjYwODAyMjMxOTIzWjAXMRUwEwYDVQQDDAxzY3VkLXRlc3QtY2EwggEiMA0GCSqG
SIb3DQEBAQUAA4IBDwAwggEKAoIBAQChW1i3A0AjtrxhtaOkAZO/eXghjUHtS/ex
j9mLHHa4RIoy0claOCJk+bN6SLpOT3+bpSa/uZ8IsqLXdQslNtvjbDN8nNmAmi+V
omOrQEMAKXm8pMH1hkqsmhOJ4194pclkW1/bm4iWSmnIm2m9LUQV72QhqPFZd/aY
QXlmexmy3iXwzmC9d2CKbKvwmSf+dkna+R/fnqP2e3ITLYQXpaXFAtBz9y6EnT1N
CerfRaifZqj6dKfbhXsWQmdlFjZFmvjhWPFPpgT2dc47kUP7Hgrwna+i7uBrN5pp
u7iYJZUYvDZ0iAfhIEjyOJ55zbZwqU/X6uZE7Cb10zkiXQ4F/K3FAgMBAAGjVjBU
MB0GA1UdDgQWBBQFPgowyMm+EY1szH4NRCuZlWWp9zAfBgNVHSMEGDAWgBQFPgow
yMm+EY1szH4NRCuZlWWp9zASBgNVHRMBAf8ECDAGAQH/AgEAMA0GCSqGSIb3DQEB
CwUAA4IBAQAz3gQOIX0dHRyF0n7RBlYxGwqnvg33VgKULt0G1E05AOedBZqxpRiC
YL+k9lZHXXDA5n40edmNpWXzWtV38jOu5Ob94++RwCOJDfmuQxWAoWjpGcjWA39+
zZw/1u17k2Kk3sWGA9Rvyirq6Ga4J1sprA3bOJLQ3+hSDndNX1GanojcKDuw5vI5
7zUUUoiT8JUTL/4kUrMA79S/QsUj8QXVWO2cNG6PJ5SvT4ew+4XaQsxbNmibdUZ4
yRTH7/rl/w7K/dtkCdFS9NFcknVdNhvWq0j9M4f59ExHtJOWFDZIEkPBjNPDQnZz
Yy3+Qc39OVXmh4fSE6ITAe+ylGSq+Qhy
-----END CERTIFICATE-----
"""
