"""The managed log boundary must keep uvicorn's access formatter contract."""

import logging

from uvicorn.logging import AccessFormatter

from core.managed_security import install_managed_log_boundary, managed_logging


def test_managed_access_record_keeps_five_field_shape_without_leaking():
    install_managed_log_boundary()
    token = managed_logging.set(True)
    try:
        record = logging.getLogger("uvicorn.access").makeRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4", "GET", "/chat?token=private-value", "1.1", 200), None,
        )
    finally:
        managed_logging.reset(token)
    assert record.args == ("-", "-", "-", "1.1", "0")
    rendered = AccessFormatter(use_colors=False).format(record)
    assert "private-value" not in rendered and "/chat" not in rendered


def test_managed_error_record_stays_generic():
    install_managed_log_boundary()
    token = managed_logging.set(True)
    try:
        record = logging.getLogger("uvicorn.error").makeRecord(
            "uvicorn.error", logging.WARNING, __file__, 1, "private %s", ("value",), None,
        )
    finally:
        managed_logging.reset(token)
    assert record.getMessage() == "Managed execution diagnostic"
    assert "private" not in repr(record.__dict__)
