from __future__ import annotations

from app import planner


def test_strict_size_planner_reports_confident_size() -> None:
    meta = {
        "duration": 60,
        "formats": [
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 720,
                "fps": 30,
                "tbr": 1000,
                "filesize": 40_000_000,
            }
        ],
    }
    plan = planner.build_video_plan_no_squeeze(meta)
    assert plan["format_spec"] == "18"
    assert plan["estimated_confident"] is True
    assert plan["estimated_size"] == 40_000_000


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status_code = status
        self.headers = headers
        self.is_redirect = status in {301, 302, 303, 307, 308}
        self.is_permanent_redirect = status in {301, 308}

    def close(self) -> None:
        pass


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append((url, kwargs))
        return next(self.responses)

    def close(self) -> None:
        pass


def test_size_probe_disables_redirects_and_validates_each_hop(monkeypatch) -> None:
    session = FakeSession(
        [
            FakeResponse(302, {"Location": "https://cdn.example/video"}),
            FakeResponse(206, {"Content-Range": "bytes 0-0/1234567"}),
        ]
    )
    monkeypatch.setattr(planner, "requests_session_with_retries", lambda: session)
    monkeypatch.setattr(planner, "validate_public_url", lambda url: url)
    monkeypatch.setattr(planner, "validate_redirect", lambda _current, location: location)
    assert planner.probe_url_size_bytes("https://example/video") == 1_234_567
    assert all(item[1]["allow_redirects"] is False for item in session.urls)
