from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_URL_LENGTH = 4096
MAX_REDIRECTS = 5
LOGGER = logging.getLogger(__name__)


class UnsafeUrlError(ValueError):
    pass


def domain_matches(hostname: str, domain: str) -> bool:
    host = hostname.rstrip(".").lower()
    expected = domain.rstrip(".").lower()
    return host == expected or host.endswith(f".{expected}")


def _is_global_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def validate_public_url(url: str) -> str:
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL_LENGTH:
        raise UnsafeUrlError("invalid URL length")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid URL") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("only HTTP(S) URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("credentials are not allowed")
    hostname = (parts.hostname or "").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("localhost is not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeUrlError("invalid port")

    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeUrlError("non-global IP is not allowed")
        return url

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
        records = socket.getaddrinfo(
            ascii_host,
            port or (443 if parts.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (UnicodeError, socket.gaierror, OSError) as exc:
        raise UnsafeUrlError("host cannot be resolved") from exc
    addresses = {record[4][0] for record in records if record and record[4]}
    if not addresses or not all(_is_global_ip(address) for address in addresses):
        raise UnsafeUrlError("host resolves to a non-global IP")
    return url


def validate_redirect(current_url: str, location: str) -> str:
    if not location:
        raise UnsafeUrlError("redirect has no Location")
    return validate_public_url(urljoin(current_url, location))


def safe_url_for_log(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or "invalid"
        return urlunsplit((parts.scheme, host, "", "", ""))[:256]
    except Exception:
        LOGGER.debug("failed to sanitize URL for logging", exc_info=True)
        return "<invalid-url>"


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def safe_error_for_log(error: BaseException) -> str:
    """Return a bounded one-line error without source paths, queries, or fragments."""
    summary = " ".join(str(error).split())
    return _URL_IN_TEXT.sub("<url-redacted>", summary)[:512]
