"""configure_logging идемпотентен (повторный вызов не множит handlers)."""
import logging

from shared.observability import configure_logging


def test_repeated_configure_logging_single_handler() -> None:
    configure_logging("svc_a")
    configure_logging("svc_a")
    configure_logging("svc_a")
    root = logging.getLogger()
    assert len(root.handlers) == 1, "configure_logging должен очищать прежние handlers"
