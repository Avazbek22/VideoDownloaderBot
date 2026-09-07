from __future__ import annotations

import errno
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


def test_instagram_fresh_extraction_rebuilds_candidates(tmp_path, monkeypatch) -> None:
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
            if self.options["format"] == "prechecked":
                raise RuntimeError("expired CDN URL")
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_text("selected", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

        def extract_info(self, _url, download):
            assert download is False
            return {
                "webpage_url": "https://www.instagram.com/reel/example/",
                "formats": [
                    {
                        "format_id": "fresh",
                        "ext": "mp4",
                        "vcodec": "avc1.64001f",
                        "acodec": "mp4a.40.2",
                        "filesize": 100,
                    }
                ],
            }

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)

    main._download_and_send(_job("https://www.instagram.com/reel/example/", (_candidate("prechecked"),)))

    download_options = [options for options in options_seen if "format" in options]
    assert [options["format"] for options in download_options] == ["prechecked", "fresh"]
    assert download_options[1]["concurrent_fragment_downloads"] == 1
    assert "impersonate" not in download_options[1]
    assert sent == ["selected"]


def test_youtube_fresh_extraction_rechecks_same_prechecked_format(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    options_seen: list[dict] = []
    process_attempt = 0

    class FakeYDL:
        def __init__(self, options):
            self.options = dict(options)
            options_seen.append(self.options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download):
            assert download is False
            return {
                "formats": [
                    {
                        "format_id": "prechecked",
                        "ext": "mp4",
                        "vcodec": "avc1.64001f",
                        "acodec": "mp4a.40.2",
                        "filesize": 100,
                    }
                ]
            }

        def process_ie_result(self, _metadata, download):
            nonlocal process_attempt
            assert download is True
            process_attempt += 1
            if process_attempt == 1:
                raise RuntimeError("expired CDN URL")
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_text("youtube-selected", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_validate_metadata_urls", lambda _metadata: None)

    main._download_and_send(_job("https://www.youtube.com/watch?v=example", (_candidate("prechecked"),)))

    assert process_attempt == 2
    download_options = [options for options in options_seen if "format" in options]
    assert all(options["format"] == "prechecked" for options in download_options)
    assert download_options[1]["concurrent_fragment_downloads"] == 1
    assert sent == ["youtube-selected"]


def test_youtube_fresh_extraction_uses_new_candidate_and_media_url(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    process_attempt = 0

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download):
            assert download is False
            return {
                "formats": [
                    {
                        "format_id": "different",
                        "ext": "mp4",
                        "vcodec": "avc1.64001f",
                        "acodec": "mp4a.40.2",
                        "filesize": 100,
                        "url": "https://fresh.example/video.mp4",
                    }
                ]
            }

        def process_ie_result(self, metadata, download):
            nonlocal process_attempt
            assert download is True
            process_attempt += 1
            if self.options["format"] == "prechecked":
                raise RuntimeError("expired CDN URL")
            assert metadata["formats"][0]["url"] == "https://fresh.example/video.mp4"
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_text("fresh-candidate", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "_validate_metadata_urls", lambda _metadata: None)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)

    main._download_and_send(_job("https://www.youtube.com/watch?v=example", (_candidate("prechecked"),)))

    assert process_attempt == 2
    assert sent == ["fresh-candidate"]


def test_youtube_403_uses_fresh_extraction_with_next_player_client(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    extraction_clients: list[str] = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download):
            assert download is False
            configured = self.options.get("extractor_args", {}).get("youtube", {}).get("player_client", [])
            client = configured[0] if configured else "default"
            extraction_clients.append(client)
            if client == "default":
                raise RuntimeError("HTTP Error 403: Forbidden")
            return {
                "formats": [
                    {
                        "format_id": "android-fresh",
                        "ext": "mp4",
                        "vcodec": "avc1.64001f",
                        "acodec": "mp4a.40.2",
                        "filesize": 100,
                    }
                ]
            }

        def process_ie_result(self, _metadata, download):
            assert download is True
            if self.options["format"] == "prechecked":
                raise RuntimeError("HTTP Error 403: Forbidden")
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_text("android-fresh", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "_validate_metadata_urls", lambda _metadata: None)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)

    main._download_and_send(_job("https://www.youtube.com/watch?v=example", (_candidate("prechecked"),)))

    assert extraction_clients == ["default", "android"]
    assert sent == ["android-fresh"]


def test_downloaded_bytes_hard_cap_aborts_attempt_and_cleans_partial_files(tmp_path, monkeypatch) -> None:
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

        def process_ie_result(self, _metadata, download):
            assert download is True
            selected = self.options["format"]
            formats_seen.append(selected)
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            contents_before_attempt.append(sorted(path.name for path in output.parent.iterdir()))
            partial = output.with_suffix(".part")
            partial.write_bytes(b"partial")
            if selected == "oversized-hls":
                self.options["progress_hooks"][0](
                    {
                        "status": "downloading",
                        # Some yt-dlp fragment/component callbacks do not use
                        # the final output-template prefix. The hard cap still
                        # has to run before UI-specific filename filtering.
                        "filename": str(partial.with_name("component.part")),
                        "downloaded_bytes": main.MAX_SEND_BYTES + 1,
                        "total_bytes_estimate": main.MAX_SEND_BYTES * 8,
                    }
                )
            output.write_text("fits", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)

    main._download_and_send(_job("https://example.com/video", (_candidate("oversized-hls"), _candidate("fits"))))

    assert formats_seen == ["oversized-hls", "fits"]
    assert contents_before_attempt == [[], []]
    assert sent == ["fits"]


def test_enospc_is_terminal_and_does_not_start_the_next_candidate(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    formats_seen: list[str] = []
    fresh_extractions = 0

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def process_ie_result(self, _metadata, download):
            assert download is True
            formats_seen.append(self.options["format"])
            partial = Path(self.options["outtmpl"].replace("%(ext)s", "part"))
            partial.write_bytes(b"partial")
            try:
                raise OSError(errno.ENOSPC, "No space left on device")
            except OSError as cause:
                raise RuntimeError("yt-dlp wrapped the filesystem failure") from cause

        def extract_info(self, _url, download):
            nonlocal fresh_extractions
            fresh_extractions += 1
            raise AssertionError("ENOSPC must stop before fresh extraction")

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)

    main._download_and_send(
        _job("https://www.youtube.com/watch?v=example", (_candidate("first"), _candidate("second")))
    )

    assert formats_seen == ["first"]
    assert fresh_extractions == 0
    assert sent == []
    assert not (main.SETTINGS.output_dir / "job").exists()


def test_generic_download_retries_with_fresh_impersonated_metadata(tmp_path, monkeypatch) -> None:
    sent = _prepare(tmp_path, monkeypatch)
    options_seen: list[dict] = []
    extraction_count = 0

    class FakeYDL:
        def __init__(self, options):
            self.options = dict(options)
            options_seen.append(self.options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download):
            nonlocal extraction_count
            assert download is False
            assert "impersonate" in self.options
            extraction_count += 1
            return {
                "formats": [
                    {
                        "format_id": "fresh",
                        "ext": "mp4",
                        "vcodec": "avc1.64001f",
                        "acodec": "mp4a.40.2",
                        "filesize": 100,
                    }
                ]
            }

        def process_ie_result(self, _metadata, download):
            assert download is True
            if "impersonate" not in self.options:
                raise RuntimeError("HTTP 403 anti-bot challenge")
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.write_text("impersonated", encoding="utf-8")
            return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(main, "_validate_metadata_urls", lambda _metadata: None)
    monkeypatch.setattr(main, "validate_media_file", lambda *_args, **_kwargs: None)

    main._download_and_send(_job("https://example.com/protected", (_candidate("stale"),)))

    download_options = [options for options in options_seen if "format" in options]
    assert "impersonate" not in download_options[0]
    assert "impersonate" in download_options[1]
    assert extraction_count == 1
    assert sent == ["impersonated"]
