import logging

from app.logging_setup import RedactingFormatter
from app.url_security import safe_error_for_log, safe_url_for_log


def test_logging_redacts_url_query_and_bot_token() -> None:
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "failed https://example.com/video?secret=yes token=123456789:abcdefghijklmnopqrstuvwxyz",
        (),
        None,
    )
    rendered = formatter.format(record)
    assert "secret=yes" not in rendered
    assert "123456789:abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "<bot-token-redacted>" in rendered
    assert "/video" not in rendered


def test_safe_log_helpers_remove_source_url_identifiers() -> None:
    source = "https://youtu.be/private-video-id?token=secret"

    assert safe_url_for_log(source) == "https://youtu.be"
    summary = safe_error_for_log(RuntimeError(f"failed URL {source}"))
    assert "private-video-id" not in summary
    assert "secret" not in summary
