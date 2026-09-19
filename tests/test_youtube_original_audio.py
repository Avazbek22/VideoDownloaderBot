from __future__ import annotations

from app import planner


def _format(
    format_id: str,
    *,
    vcodec: str,
    acodec: str,
    ext: str = "mp4",
    filesize: int = 5_000_000,
    height: int = 0,
    **extra,
) -> dict:
    return {
        "format_id": format_id,
        "url": f"https://cdn.example/{format_id}.{ext}",
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
        "filesize": filesize,
        "height": height,
        **extra,
    }


def test_video_candidates_prefer_original_audio_over_higher_bitrate_auto_dub() -> None:
    metadata = {
        "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
        "formats": [
            _format("video", vcodec="avc1.640028", acodec="none", filesize=30_000_000, height=1080),
            _format(
                "dubbed",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                language="en-US",
                language_preference=10,
                format_note="English (United States) (default)",
                abr=192,
            ),
            _format(
                "original",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                language="ru",
                language_preference=-1,
                format_note="Russian",
                abr=128,
            ),
        ],
    }

    candidates = planner.build_video_candidates(metadata, 50_000_000)

    assert [candidate.format_spec for candidate in candidates[:2]] == ["video+original", "video+dubbed"]


def test_original_progressive_audio_wins_over_higher_resolution_auto_dub() -> None:
    metadata = {
        "formats": [
            _format(
                "dubbed-720",
                vcodec="avc1.64001f",
                acodec="mp4a.40.2",
                filesize=25_000_000,
                height=720,
                format_note="English - auto-dubbed (default)",
            ),
            _format(
                "original-360",
                vcodec="avc1.42001e",
                acodec="mp4a.40.2",
                filesize=8_000_000,
                height=360,
                format_note="Russian - original",
            ),
        ]
    }

    candidates = planner.build_video_candidates(metadata, 50_000_000)

    assert candidates[0].format_spec == "original-360"


def test_video_uses_dub_only_when_original_cannot_fit() -> None:
    metadata = {
        "formats": [
            _format("video", vcodec="avc1.640028", acodec="none", filesize=43_000_000, height=1080),
            _format(
                "original-too-large",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                filesize=8_000_000,
                language="ru",
                language_preference=10,
            ),
            _format(
                "dubbed-fallback",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                filesize=4_000_000,
                language="en",
                format_note="English - dubbed-auto",
            ),
        ]
    }

    candidates = planner.build_video_candidates(metadata, 50_000_000)

    assert candidates[0].format_spec == "video+dubbed-fallback"


def test_mp3_plan_selects_original_source_instead_of_auto_dub() -> None:
    metadata = {
        "duration": 60,
        "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
        "formats": [
            _format(
                "dubbed",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                language="en-US",
                format_note="English - dubbed-auto (default)",
                abr=192,
            ),
            _format(
                "original",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                language="ru",
                abr=128,
            ),
        ],
    }

    plan, reason = planner.build_audio_plan_mp3(metadata, 50_000_000)
    plans, _ = planner.build_audio_plans_mp3(metadata, 50_000_000)

    assert reason is None
    assert plan is not None
    assert plan["format_spec"] == "original"
    assert [candidate["format_spec"] for candidate in plans] == ["original", "dubbed"]


def test_mp3_plan_prefers_audio_only_source_within_original_track() -> None:
    metadata = {
        "duration": 60,
        "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
        "formats": [
            _format(
                "progressive-original",
                vcodec="avc1.640028",
                acodec="mp4a.40.2",
                language="ru",
                quality=10,
                abr=192,
            ),
            _format(
                "audio-original",
                vcodec="none",
                acodec="mp4a.40.2",
                ext="m4a",
                language="ru",
                quality=-1,
                abr=128,
            ),
        ],
    }

    plans, reason = planner.build_audio_plans_mp3(metadata, 50_000_000)

    assert reason is None
    assert plans[0]["format_spec"] == "audio-original"


def test_mp3_plan_keeps_ytdlp_selector_when_audio_roles_are_unknown() -> None:
    metadata = {
        "duration": 60,
        "formats": [
            _format("audio", vcodec="none", acodec="mp4a.40.2", ext="m4a", abr=128),
        ],
    }

    plan, reason = planner.build_audio_plan_mp3(metadata, 50_000_000)

    assert reason is None
    assert plan is not None
    assert plan["format_spec"] == "bestaudio/best"


def test_original_language_hint_is_removed_before_download_processing() -> None:
    metadata = {"title": "Example"}
    planner.remember_original_audio_language(metadata, "ru-RU")

    assert planner.original_audio_language(metadata) == "ru-ru"
    assert planner.ORIGINAL_AUDIO_LANGUAGE_FIELD not in planner.metadata_without_format_selection(metadata)


def test_caption_marker_overrides_misleading_regional_language_preference() -> None:
    metadata = {
        "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
        "formats": [
            _format(
                "regional-default",
                vcodec="avc1.42001e",
                acodec="mp4a.40.2",
                language="en-US",
                language_preference=10,
            ),
            _format(
                "source",
                vcodec="avc1.42001e",
                acodec="mp4a.40.2",
                language="ru",
                language_preference=-1,
            ),
        ],
    }

    candidates = planner.build_video_candidates(metadata, 50_000_000)

    assert planner.original_audio_language(metadata) == "ru"
    assert candidates[0].format_spec == "source"


def test_youtube_request_language_uses_supported_locale_codes() -> None:
    assert planner.youtube_request_language("en-US") == "en"
    assert planner.youtube_request_language("en-gb") == "en-GB"
    assert planner.youtube_request_language("zh-Hans") == "zh-CN"
    assert planner.youtube_request_language("pt-BR") == "pt"
