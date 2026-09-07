from __future__ import annotations

from copy import deepcopy

from app import planner

LIMIT = 50 * 1024 * 1024


def _format(
    format_id: str,
    *,
    url: str,
    vcodec: str | None,
    acodec: str | None,
    ext: str = "mp4",
    filesize: int | None = 10_000_000,
    height: int = 720,
    fps: int = 30,
    tbr: int = 1_000,
    **extra,
) -> dict:
    result = {
        "format_id": format_id,
        "url": url,
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
        "height": height,
        "fps": fps,
        "tbr": tbr,
    }
    if filesize is not None:
        result["filesize"] = filesize
    result.update(extra)
    return result


def test_h264_candidate_is_ranked_above_vp9_and_av1() -> None:
    meta = {
        "formats": [
            _format(
                "vp9",
                url="https://cdn.example/vp9.mp4",
                vcodec="vp09.00.51.08",
                acodec="mp4a.40.2",
                height=1080,
            ),
            _format(
                "av1",
                url="https://cdn.example/av1.mp4",
                vcodec="av01.0.08M.08",
                acodec="mp4a.40.2",
                height=2160,
            ),
            _format(
                "h264",
                url="https://cdn.example/h264.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                height=720,
            ),
        ]
    }

    candidates = planner.build_video_candidates(meta, LIMIT)

    assert [candidate.format_spec for candidate in candidates] == ["h264"]


def test_progressive_does_not_override_higher_quality_separate_h264() -> None:
    meta = {
        "formats": [
            _format(
                "progressive",
                url="https://cdn.example/progressive.mp4",
                vcodec="avc1.4d401e",
                acodec="mp4a.40.2",
                height=360,
                filesize=4_000_000,
            ),
            _format(
                "video",
                url="https://cdn.example/video.mp4",
                vcodec="avc1.640028",
                acodec="none",
                height=1080,
                filesize=20_000_000,
            ),
            _format(
                "audio",
                url="https://cdn.example/audio.m4a",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                height=0,
                filesize=2_000_000,
            ),
        ]
    }

    candidates = planner.build_video_candidates(meta, LIMIT)

    assert [candidate.format_spec for candidate in candidates] == ["video+audio", "progressive"]


def test_avc1_avc3_and_h264_are_recognized_as_h264() -> None:
    assert planner.is_h264_codec("avc1.640028")
    assert planner.is_h264_codec("avc3.4d401f")
    assert planner.is_h264_codec("h264")
    assert not planner.is_h264_codec("vp9")
    assert not planner.is_h264_codec(None)


def test_instagram_direct_mp4_without_codec_fields_is_lower_compatibility() -> None:
    meta = {
        "webpage_url": "https://www.instagram.com/reel/example/",
        "formats": [
            _format(
                "direct",
                url="https://scontent.example/direct.mp4",
                vcodec=None,
                acodec=None,
            ),
            _format(
                "known",
                url="https://scontent.example/known.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
            ),
        ],
    }

    candidates = planner.build_video_candidates(meta, LIMIT)

    assert [candidate.format_spec for candidate in candidates] == ["known", "direct"]
    assert candidates[0].compatibility > candidates[1].compatibility


def test_duplicate_instagram_direct_url_is_deduplicated() -> None:
    direct_url = "https://scontent.example/shared.mp4"
    meta = {
        "webpage_url": "https://www.instagram.com/reel/example/",
        "formats": [
            _format("one", url=direct_url, vcodec=None, acodec=None),
            _format("two", url=direct_url, vcodec=None, acodec=None),
        ],
    }

    candidates = planner.build_video_candidates(meta, LIMIT)

    assert len(candidates) == 1
    assert candidates[0].direct_urls == (direct_url,)


def test_duplicate_format_spec_is_deduplicated() -> None:
    meta = {
        "formats": [
            _format(
                "same",
                url="https://cdn.example/first.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
            ),
            _format(
                "same",
                url="https://cdn.example/second.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                height=360,
            ),
        ]
    }

    candidates = planner.build_video_candidates(meta, LIMIT)

    assert len(candidates) == 1
    assert candidates[0].format_spec == "same"


def test_candidate_with_unknown_size_is_not_eligible(monkeypatch) -> None:
    monkeypatch.setattr(planner, "probe_url_size_bytes", lambda _url: None)
    meta = {
        "formats": [
            _format(
                "unknown",
                url="https://cdn.example/unknown.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                filesize=None,
            )
        ]
    }

    assert planner.build_video_candidates(meta, LIMIT) == []


def test_range_proven_candidate_is_eligible(monkeypatch) -> None:
    monkeypatch.setattr(planner, "probe_url_size_bytes", lambda _url: 12_000_000)
    meta = {
        "formats": [
            _format(
                "proven",
                url="https://cdn.example/proven.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                filesize=None,
            )
        ]
    }

    candidates = planner.build_video_candidates(meta, LIMIT)

    assert len(candidates) == 1
    assert candidates[0].estimated_size == 12_000_000
    assert candidates[0].estimated_confident is True


def test_candidate_larger_than_limit_is_not_eligible() -> None:
    meta = {
        "formats": [
            _format(
                "large",
                url="https://cdn.example/large.mp4",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                filesize=LIMIT + 1,
            )
        ]
    }

    assert planner.build_video_candidates(meta, LIMIT) == []


def test_fragmented_filesize_approx_is_buffered_and_not_marked_exact() -> None:
    reported = 20_000_000
    meta = {
        "formats": [
            _format(
                "hls",
                url="https://cdn.example/playlist.m3u8",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                filesize=None,
                filesize_approx=reported,
                protocol="m3u8_native",
            )
        ]
    }

    candidate = planner.build_video_candidates(meta, LIMIT)[0]

    assert candidate.estimated_size > reported
    assert candidate.estimated_confident is False
    assert candidate.size_source == "fragmented-approximate"


def test_complete_fragment_sizes_are_treated_as_exact() -> None:
    meta = {
        "formats": [
            _format(
                "hls",
                url="https://cdn.example/playlist.m3u8",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                filesize=None,
                filesize_approx=1,
                protocol="m3u8_native",
                fragments=[{"filesize": 10_000_000}, {"filesize": 11_000_000}],
            )
        ]
    }

    candidate = planner.build_video_candidates(meta, LIMIT)[0]

    assert candidate.estimated_size == 21_000_000
    assert candidate.estimated_confident is True
    assert candidate.size_source == "fragments"


def test_stale_metadata_selection_is_removed_without_mutating_original() -> None:
    meta = {
        "title": "Example",
        "requested_formats": [{"format_id": "old"}],
        "requested_downloads": [{"format_id": "old"}],
        "format_id": "old",
        "format": "old format",
        "url": "https://cdn.example/old.mp4",
        "ext": "mp4",
        "vcodec": "vp9",
        "acodec": "opus",
        "height": 1080,
        "formats": [{"format_id": "selected"}],
    }
    original = deepcopy(meta)

    cleaned = planner.metadata_without_format_selection(meta)

    assert cleaned["title"] == "Example"
    assert cleaned["formats"] == [{"format_id": "selected"}]
    for key in planner.FORMAT_SELECTION_FIELDS:
        assert key not in cleaned
    assert meta == original


def test_unverified_youtube_fallback_is_not_present_in_download_source() -> None:
    source = __file__[: -len("tests/test_instagram_format_candidates.py")] + "main.py"
    with open(source, encoding="utf-8") as handle:
        assert "18/best[ext=mp4]/best" not in handle.read()
