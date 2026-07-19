import logging

from app.logging_setup import RedactingFormatter


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
