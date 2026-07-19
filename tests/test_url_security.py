from __future__ import annotations

import socket

import pytest

from app.url_security import UnsafeUrlError, domain_matches, validate_public_url, validate_redirect


def _dns(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://169.254.169.254/x",
        "http://[::1]/x",
        "http://[fc00::1]/x",
        "https://user:pass@example.com/x",
        "https://example.com/" + "x" * 5000,
    ],
)
def test_rejects_ssrf_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


def test_dns_must_be_globally_routable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: _dns("192.168.1.5"))
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://example.com/video")


def test_redirect_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: _dns("93.184.216.34"))
    assert validate_redirect("https://example.com/a", "/video") == "https://example.com/video"
    with pytest.raises(UnsafeUrlError):
        validate_redirect("https://example.com/a", "http://127.0.0.1/private")


def test_domain_matching_has_label_boundary() -> None:
    assert domain_matches("www.youtube.com", "youtube.com")
    assert domain_matches("youtube.com", "youtube.com")
    assert not domain_matches("evil-youtube.com", "youtube.com")
