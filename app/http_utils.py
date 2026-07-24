import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def probe_session_with_retries() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def telegram_upload_session() -> requests.Session:
    """Uploads are never transparently retried: a POST may already have succeeded."""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0, redirect=0))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Backward-compatible name for planner imports in older forks.
requests_session_with_retries = probe_session_with_retries
