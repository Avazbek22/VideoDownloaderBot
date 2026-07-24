from __future__ import annotations

import threading
import time
from pathlib import Path

import main
from app.media_validation import MediaValidationError
from app.models import DownloadJob, VideoFormatCandidate
from app.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        token="123:test-token-value-abcdefghijklmnop",
        logs_chat_id=None,
        output_dir=tmp_path / "data" / "downloads",
        logs_dir=tmp_path / "logs",
        max_filesize=50 * 1024 * 1024,
        workers=1,
        max_queue=2,
        upload_workers=1,
        job_timeout_seconds=60,
        pending_ttl_seconds=30,
        concurrent_fragments=4,
        cookies_file=None,
        log_level="INFO",
    )


def _candidate(format_spec: str) -> VideoFormatCandidate:
    return VideoFormatCandidate(
        format_spec=format_spec,
        merge_output_format=None,
        estimated_size=100,
        estimated_confident=True,
        quality_label="720p",
        compatibility=2,
        direct_urls=(f"https://cdn.example/{format_spec}.mp4",),
    )


def _job(url: str, candidates: tuple[VideoFormatCandidate, ...]) -> DownloadJob:
    return DownloadJob(
        job_id="job",
        user_id=1,
        chat_id=2,
        reply_to_message_id=3,
        status_message_id=4,
        url=url,
        title="Title",
        mode="video",
        video_candidates=candidates,
        audio_plan=None,
        metadata={
            "title": "Title",
            "requested_formats": [{"format_id": "stale"}],
            "requested_downloads": [{"format_id": "stale"}],
            "format_id": "stale",
            "url": "https://cdn.example/stale.mp4",
            "formats": [],
        },
        deadline=time.monotonic() + 60,
    )


def _prepare(tmp_path, monkeypatch) -> list[str]:
    main.SETTINGS = _settings(tmp_path)
    main.MAX_SEND_BYTES = main.SETTINGS.max_filesize
    main.stop_event.clear()
    main.cancel_events["job"] = threading.Event()
    sent: list[str] = []
    monkeypatch.setattr(main, "_safe_edit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_safe_delete", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_notify_operator_critical", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "_send_via_bot_api_with_progress",
        lambda **kwargs: sent.append(Path(kwargs["file_path"]).read_text(encoding="utf-8")),
    )
    return sent


def test_invalid_candidate_is_removed_then_next_prechecked_candidate_is_used(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    formats_seen: list[str] = []
    contents_before_attempt: list[list[str]] = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, metadata, download):
            assert download is True
            assert "requested_formats" not in metadata
            assert "requested_downloads" not in metadata
            assert "format_id" not in metadata
            assert "url" not in metadata
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            contents_before_attempt.append(sorted(path.name for path in output.parent.iterdir()))
            selected = self.options["format"]
            formats_seen.append(selected)
            output.write_text(selected, encoding="utf-8")
            output.with_suffix(".part").write_text("partial", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    def validate(path, *_args, **_kwargs):
        if Path(path).read_text(encoding="utf-8") == "bad":
            raise MediaValidationError("audio-only")

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "validate_media_file", validate)

    main._download_and_send(_job("https://example.com/video", (_candidate("bad"), _candidate("good"))))

    assert formats_seen == ["bad", "good"]
    assert contents_before_attempt == [[], []]
    assert sent == ["good"]


def test_instagram_fresh_extraction_keeps_same_prechecked_format(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    options_seen: list[dict] = []

    class FakeYDL:
        def __init__(self, options):
            self.options = dict(options)
            options_seen.append(self.options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, _metadata, download):
            assert download is True
            raise RuntimeError("expired CDN URL")

        def extract_info(self, _url, download):
            assert download is True
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_text("selected", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)

    main._download_and_send(_job("https://www.instagram.com/reel/example/", (_candidate("prechecked"),)))

    assert [options["format"] for options in options_seen] == ["prechecked", "prechecked"]
    assert options_seen[1]["concurrent_fragment_downloads"] == 1
    assert "impersonate" not in options_seen[1]
    assert sent == ["selected"]
