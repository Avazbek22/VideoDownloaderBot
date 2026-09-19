import logging
import math
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from app.http_utils import requests_session_with_retries
from app.models import VideoFormatCandidate
from app.text_utils import fmt_bytes
from app.url_security import MAX_REDIRECTS, domain_matches, validate_public_url, validate_redirect

AUDIO_HEADROOM_BYTES = 1_500_000
FRAGMENTED_SIZE_FACTOR = 1.25
FRAGMENTED_SIZE_HEADROOM_BYTES = 1024 * 1024
LOGGER = logging.getLogger(__name__)
H264_PREFIXES = ("avc1", "avc3", "h264")
ORIGINAL_AUDIO_LANGUAGE_FIELD = "_vdb_original_audio_language"
FORMAT_SELECTION_FIELDS = frozenset(
    {
        "requested_formats",
        "requested_downloads",
        "format_id",
        "format",
        "url",
        "manifest_url",
        "ext",
        "vcodec",
        "acodec",
        "width",
        "height",
        "resolution",
        "filesize",
        "filesize_approx",
        "tbr",
        "vbr",
        "abr",
        "protocol",
        "container",
        ORIGINAL_AUDIO_LANGUAGE_FIELD,
    }
)


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        LOGGER.debug("failed to parse URL while checking YouTube domain", exc_info=True)
        return False
    return any(
        domain_matches(host, x)
        for x in (
            "youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
        )
    )


def is_instagram_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        LOGGER.debug("failed to parse URL while checking Instagram domain", exc_info=True)
        return False
    return any(
        domain_matches(host, x)
        for x in (
            "instagram.com",
            "instagr.am",
        )
    )


def apply_youtube_runtime_opts(
    opts: dict[str, Any],
    url: str,
    js_runtimes: str | None,
    remote_components: str | None,
    player_client: str | None = None,
    youtube_language: str | None = None,
) -> dict[str, Any]:
    if not is_youtube_url(url):
        return opts
    # YouTube may expose an automatic dub as the regional/account default.
    # Put yt-dlp's language preference ahead of bitrate and codec whenever a
    # generic selector such as `bestaudio` still has to make the final choice.
    opts["format_sort"] = ["lang"]
    opts["format_sort_force"] = True
    if js_runtimes:
        parsed: dict[str, dict[str, str]] = {}
        for raw in str(js_runtimes).split(","):
            item = raw.strip()
            if not item:
                continue
            runtime, _, path = item.partition(":")
            runtime = runtime.strip().lower()
            if not runtime:
                continue
            conf: dict[str, str] = {}
            if path.strip():
                conf["path"] = path.strip()
            parsed[runtime] = conf
        if parsed:
            opts["js_runtimes"] = parsed
    if remote_components:
        comps = [x.strip() for x in str(remote_components).split(",") if x.strip()]
        if comps:
            opts["remote_components"] = comps
    youtube_args: dict[str, list[str]] = {}
    if player_client:
        youtube_args["player_client"] = [player_client]
    if request_language := youtube_request_language(youtube_language):
        youtube_args["lang"] = [request_language]
    if youtube_args:
        extractor_args = dict(opts.get("extractor_args") or {})
        existing_youtube_args = dict(extractor_args.get("youtube") or {})
        existing_youtube_args.update(youtube_args)
        extractor_args["youtube"] = existing_youtube_args
        opts["extractor_args"] = extractor_args
    return opts


def youtube_player_clients(raw: str | None) -> list[str | None]:
    """Return a stable yt-dlp client fallback chain; None means yt-dlp's default."""
    configured = [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]
    if not configured:
        configured = ["default", "android", "ios"]

    result: list[str | None] = []
    for item in configured:
        client = None if item in {"default", "web"} else item
        if client not in result:
            result.append(client)
    return result or [None, "android", "ios"]


def has_downloadable_video(meta: dict[str, Any]) -> bool:
    """Return whether metadata identifies a video, including codec-less Instagram MP4."""
    formats = [item for item in meta.get("formats", []) or [] if isinstance(item, dict)]
    for item in [meta, *formats]:
        vcodec = item.get("vcodec")
        if vcodec not in (None, "", "none"):
            return True
        if (
            str(item.get("ext") or "").lower() == "mp4"
            and not vcodec
            and not item.get("acodec")
            and isinstance(item.get("url"), str)
        ):
            return True
    entries = [item for item in meta.get("entries", []) or [] if isinstance(item, dict)]
    return any(has_downloadable_video(item) for item in entries)


def apply_instagram_stability_opts(
    opts: dict[str, Any],
    url: str,
    impersonate: str | None,
    retries: int | None,
    fragment_retries: int | None,
    socket_timeout: int | None,
) -> dict[str, Any]:
    if not is_instagram_url(url):
        return opts
    if impersonate:
        imp = str(impersonate).strip()
        if imp:
            try:
                opts["impersonate"] = ImpersonateTarget.from_str(imp)
            except Exception:
                # If parsing fails, keep working without forced impersonation.
                LOGGER.debug("invalid yt-dlp impersonation target=%s", imp, exc_info=True)
    if isinstance(retries, int) and retries > 0:
        opts["retries"] = retries
    if isinstance(fragment_retries, int) and fragment_retries > 0:
        opts["fragment_retries"] = fragment_retries
    if isinstance(socket_timeout, int) and socket_timeout > 0:
        opts["socket_timeout"] = socket_timeout
    return opts


def apply_generic_impersonation_opts(
    opts: dict[str, Any],
    url: str,
    impersonate: str | None,
) -> dict[str, Any]:
    if not impersonate or is_youtube_url(url) or is_instagram_url(url):
        return opts
    try:
        opts["impersonate"] = ImpersonateTarget.from_str(str(impersonate).strip())
    except Exception:
        LOGGER.debug("invalid generic yt-dlp impersonation target", exc_info=True)
    return opts


def probe_url_size_bytes(url: str, timeout_sec: int = 10) -> int | None:
    """
    Try to get real content size without downloading the file:
    - Send GET with Range: bytes=0-0
    - Parse Content-Range: bytes 0-0/123456
    """
    if not url or not isinstance(url, str):
        return None

    s = requests_session_with_retries()
    try:
        current_url = validate_public_url(url)
        resp = None
        for _ in range(MAX_REDIRECTS + 1):
            resp = s.get(
                current_url,
                headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"},
                stream=True,
                timeout=(timeout_sec, timeout_sec),
                allow_redirects=False,
            )
            if resp.is_redirect or resp.is_permanent_redirect:
                next_url = validate_redirect(current_url, resp.headers.get("Location", ""))
                resp.close()
                current_url = next_url
                continue
            break
        else:
            return None
        if resp is None or resp.is_redirect or resp.is_permanent_redirect:
            return None
        cr = resp.headers.get("Content-Range") or resp.headers.get("content-range")
        if cr:
            m = re.search(r"/(\d+)\s*$", cr.strip())
            if m:
                total = int(m.group(1))
                if total > 0:
                    return total

        # Some servers may return Content-Length for full response (rare with range).
        cl = resp.headers.get("Content-Length") or resp.headers.get("content-length")
        if cl and cl.isdigit():
            # With range it can be 1 byte; ignore tiny values.
            val = int(cl)
            if val > 1024 * 1024:
                return val

        return None
    except Exception:
        LOGGER.debug("size probe failed url=%s", url, exc_info=True)
        return None
    finally:
        s.close()


def get_video_meta(
    url: str,
    js_runtimes: str | None = None,
    remote_components: str | None = None,
    instagram_impersonate: str | None = None,
    instagram_retries: int | None = None,
    instagram_fragment_retries: int | None = None,
    instagram_socket_timeout: int | None = None,
    cookies_file: str | None = None,
    youtube_player_client: str | None = None,
    youtube_language: str | None = None,
    generic_impersonate: str | None = None,
) -> dict[str, Any]:
    ydl_opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    ydl_opts = apply_youtube_runtime_opts(
        ydl_opts,
        url,
        js_runtimes,
        remote_components,
        player_client=youtube_player_client,
        youtube_language=youtube_language,
    )
    ydl_opts = apply_instagram_stability_opts(
        ydl_opts,
        url,
        impersonate=instagram_impersonate,
        retries=instagram_retries,
        fragment_retries=instagram_fragment_retries,
        socket_timeout=instagram_socket_timeout,
    )
    ydl_opts = apply_generic_impersonation_opts(ydl_opts, url, generic_impersonate)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _duration_sec(meta: dict[str, Any]) -> int | None:
    dur = meta.get("duration")
    if isinstance(dur, (int, float)) and dur > 0:
        return int(dur)
    return None


def _format_size_bytes(fmt: dict[str, Any], dur: int | None) -> tuple[int | None, bool]:
    """
    Returns (size_bytes, confident).
    confident=True when size comes from 'filesize', a complete fragment list,
    or a URL probe. Fragmented 'filesize_approx' values remain approximate.
    """
    fs = fmt.get("filesize")
    if isinstance(fs, int) and fs > 0:
        return fs, True

    fsa = fmt.get("filesize_approx")
    if isinstance(fsa, int) and fsa > 0:
        if _is_fragmented_format(fmt):
            return _fragmented_estimate(fsa), False
        return fsa, True

    # Bitrate estimation is NOT confident (we don't use it to block/allow).
    tbr = fmt.get("tbr")  # usually Kbps
    if dur and isinstance(tbr, (int, float)) and tbr > 0:
        est = int(dur * (float(tbr) * 1000.0 / 8.0))
        if est > 0:
            return est, False

    return None, False


def is_h264_codec(codec: Any) -> bool:
    return isinstance(codec, str) and codec.strip().lower().startswith(H264_PREFIXES)


def metadata_without_format_selection(meta: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(meta)
    for key in FORMAT_SELECTION_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def _positive_number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _format_has_audio(fmt: dict[str, Any]) -> bool:
    audio_codec = str(fmt.get("acodec") or "").strip().lower()
    if audio_codec:
        return audio_codec != "none"
    return str(fmt.get("ext") or "").lower() in {"aac", "m4a", "mp3", "mp4", "ogg", "opus", "webm"} and bool(
        fmt.get("url")
    )


def _normalized_language_code(value: Any) -> str:
    return str(value or "").strip().replace("_", "-").casefold()


def same_language(left: Any, right: Any) -> bool:
    left_code = _normalized_language_code(left)
    right_code = _normalized_language_code(right)
    if not left_code or not right_code:
        return False
    if left_code == right_code:
        return True
    left_parts = left_code.split("-")
    right_parts = right_code.split("-")
    return left_parts[0] == right_parts[0] and (len(left_parts) == 1 or len(right_parts) == 1)


def _audio_role_score(fmt: dict[str, Any], original_language: str | None = None) -> int:
    """Rank source audio above unlabelled, dubbed, and descriptive tracks."""
    note = " ".join(str(fmt.get(key) or "") for key in ("format_note", "format")).casefold()
    preference = _number(fmt.get("language_preference"))

    # yt-dlp normally assigns -10 to descriptive audio. A positive preference
    # alone is not authoritative: YouTube can elevate a regional default dub.
    if preference <= -10 or "descriptive" in note or "audio description" in note:
        return 0
    if "dubbed" in note or "auto-dub" in note or "autodub" in note:
        return 1
    language = _normalized_language_code(fmt.get("language"))
    if original_language and language:
        return 3 if same_language(language, original_language) else 2
    if preference >= 10 or "original" in note:
        return 3
    # Preserve quality-based ordering for sites that do not expose roles.
    return 2


def original_audio_language_candidates(meta: dict[str, Any]) -> list[str]:
    remembered = _normalized_language_code(meta.get(ORIGINAL_AUDIO_LANGUAGE_FIELD))
    if remembered:
        return [remembered]

    # yt-dlp's `*-orig` caption marker is derived from YouTube's source
    # captions and is more reliable than a regional audio preference.
    candidates: list[str] = []

    def add(language: Any) -> None:
        normalized = _normalized_language_code(language)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    automatic_captions = meta.get("automatic_captions")
    if isinstance(automatic_captions, dict):
        for language, tracks in automatic_captions.items():
            normalized = _normalized_language_code(language)
            if normalized.endswith("-orig"):
                add(normalized.removesuffix("-orig"))
                continue
            if not isinstance(tracks, list):
                continue
            if any(
                "original" in str(track.get("name") or "").casefold()
                for track in tracks
                if isinstance(track, dict)
            ):
                add(normalized)
    if candidates:
        return candidates

    formats = [item for item in meta.get("formats", []) or [] if isinstance(item, dict)]
    for fmt in formats:
        language = _normalized_language_code(fmt.get("language"))
        if language and _format_has_audio(fmt) and _audio_role_score(fmt) == 3:
            add(language)
    return candidates


def original_audio_language(meta: dict[str, Any]) -> str | None:
    """Return a source language only when metadata identifies it unambiguously."""
    candidates = original_audio_language_candidates(meta)
    return candidates[0] if len(candidates) == 1 else None


def youtube_request_language(language: str | None) -> str | None:
    """Map media-language tags to a safe YouTube UI language preference."""
    normalized = _normalized_language_code(language)
    if not normalized:
        return None
    special = {
        "en-in": "en-IN",
        "en-gb": "en-GB",
        "es-419": "es-419",
        "es-us": "es-US",
        "fr-ca": "fr-CA",
        "pt-pt": "pt-PT",
        "sr-latn": "sr-Latn",
        "zh-cn": "zh-CN",
        "zh-hans": "zh-CN",
        "zh-tw": "zh-TW",
        "zh-hant": "zh-TW",
        "zh-hk": "zh-HK",
        "yue": "zh-HK",
    }
    if normalized in special:
        return special[normalized]
    base_language = normalized.split("-", 1)[0]
    return {"he": "iw", "nb": "no", "nn": "no"}.get(base_language, base_language)


def remember_original_audio_language(meta: dict[str, Any], language: str | None) -> str | None:
    normalized = _normalized_language_code(language)
    if normalized:
        meta[ORIGINAL_AUDIO_LANGUAGE_FIELD] = normalized
        return normalized
    return None


def audio_languages(meta: dict[str, Any]) -> set[str]:
    formats = [item for item in meta.get("formats", []) or [] if isinstance(item, dict)]
    candidates = formats or [meta]
    return {
        language
        for fmt in candidates
        if _format_has_audio(fmt) and (language := _normalized_language_code(fmt.get("language")))
    }


def metadata_has_original_audio(
    meta: dict[str, Any],
    original_language: str | None,
    requested_language: str | None = None,
) -> bool:
    if not original_language:
        return True
    available_languages = audio_languages(meta)
    if any(same_language(language, original_language) for language in available_languages):
        return True
    # Some player clients omit per-format language tags. A response explicitly
    # requested in the source language is the strongest signal still available.
    return not available_languages and same_language(requested_language, original_language)


def _is_fragmented_format(fmt: dict[str, Any]) -> bool:
    protocol = str(fmt.get("protocol") or "").lower()
    return bool(
        fmt.get("manifest_url")
        or bool(fmt.get("fragments"))
        or any(marker in protocol for marker in ("m3u8", "dash", "http_dash_segments", "ism"))
    )


def _fragmented_estimate(value: int) -> int:
    return math.ceil(value * FRAGMENTED_SIZE_FACTOR) + FRAGMENTED_SIZE_HEADROOM_BYTES


def _complete_fragment_size(fmt: dict[str, Any]) -> int | None:
    fragments = fmt.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        return None
    total = 0
    for fragment in fragments:
        if not isinstance(fragment, dict):
            return None
        value = next(
            (
                fragment.get(field)
                for field in ("filesize", "size", "content_length")
                if isinstance(fragment.get(field), int) and fragment[field] > 0
            ),
            None,
        )
        if value is None:
            return None
        total += value
    return total or None


def _component_size(fmt: dict[str, Any]) -> tuple[int | None, bool, str]:
    exact = fmt.get("filesize")
    if isinstance(exact, int) and exact > 0:
        return exact, True, "filesize"

    fragment_total = _complete_fragment_size(fmt)
    if fragment_total is not None:
        return fragment_total, True, "fragments"

    approximate = fmt.get("filesize_approx")
    if isinstance(approximate, int) and approximate > 0:
        if _is_fragmented_format(fmt):
            return _fragmented_estimate(approximate), False, "fragmented-approximate"
        return approximate, True, "filesize-approximate"

    url = fmt.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")) and not _is_fragmented_format(fmt):
        size = probe_url_size_bytes(url)
        if isinstance(size, int) and size > 0:
            return size, True, "content-range"
    return None, False, "unknown"


def _direct_urls(*formats: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        value
        for value in (item.get("url") for item in formats)
        if isinstance(value, str) and value.startswith(("http://", "https://"))
    )


def build_video_candidates(
    meta: dict[str, Any],
    limit_bytes: int,
    *,
    original_language: str | None = None,
) -> list[VideoFormatCandidate]:
    """Return size-screened, Telegram-compatible candidates, best first."""
    original_language = original_language or original_audio_language(meta)
    formats = [item for item in meta.get("formats", []) or [] if isinstance(item, dict)]
    instagram = is_instagram_url(str(meta.get("webpage_url") or meta.get("original_url") or ""))
    raw: list[tuple[tuple[float, ...], VideoFormatCandidate]] = []

    for fmt in formats:
        if str(fmt.get("ext") or "").lower() != "mp4":
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        has_known_h264 = is_h264_codec(vcodec)
        is_unknown_instagram_direct = (
            instagram
            and not vcodec
            and not acodec
            and isinstance(fmt.get("url"), str)
            and fmt["url"].startswith(("http://", "https://"))
        )
        if not (has_known_h264 or is_unknown_instagram_direct):
            continue
        if has_known_h264 and acodec in (None, "", "none"):
            continue
        size, confident, size_source = _component_size(fmt)
        if size is None or size > limit_bytes:
            continue
        format_id = fmt.get("format_id")
        if format_id is None:
            continue
        compatibility = 2 if has_known_h264 else 1
        height = int(_positive_number(fmt.get("height")))
        fps = _positive_number(fmt.get("fps"))
        bitrate = _positive_number(fmt.get("tbr"))
        candidate = VideoFormatCandidate(
            format_spec=str(format_id),
            merge_output_format=None,
            estimated_size=size,
            estimated_confident=confident,
            quality_label=f"{height}p" if height else "mp4",
            compatibility=compatibility,
            direct_urls=_direct_urls(fmt),
            size_source=size_source,
        )
        raw.append(
            (
                (
                    _audio_role_score(fmt, original_language),
                    compatibility,
                    height,
                    fps,
                    bitrate,
                    _positive_number(fmt.get("abr")),
                ),
                candidate,
            )
        )

    audio_formats = [
        fmt
        for fmt in formats
        if str(fmt.get("ext") or "").lower() in {"m4a", "mp4"}
        and fmt.get("vcodec") == "none"
        and fmt.get("acodec") not in (None, "", "none")
        and fmt.get("format_id") is not None
    ]
    audio_formats.sort(
        key=lambda fmt: (
            _audio_role_score(fmt, original_language),
            _positive_number(fmt.get("abr")),
            _positive_number(fmt.get("tbr")),
        ),
        reverse=True,
    )
    proven_audio: list[tuple[dict[str, Any], int, bool, str]] = []
    for audio in audio_formats:
        audio_size, audio_confident, audio_source = _component_size(audio)
        if audio_size is not None:
            proven_audio.append((audio, audio_size, audio_confident, audio_source))

    for audio, audio_size, audio_confident, audio_source in proven_audio:
        for video in formats:
            if (
                str(video.get("ext") or "").lower() != "mp4"
                or not is_h264_codec(video.get("vcodec"))
                or video.get("acodec") != "none"
                or video.get("format_id") is None
            ):
                continue
            video_size, video_confident, video_source = _component_size(video)
            if video_size is None or video_size + audio_size > limit_bytes:
                continue
            height = int(_positive_number(video.get("height")))
            fps = _positive_number(video.get("fps"))
            bitrate = _positive_number(video.get("tbr"))
            candidate = VideoFormatCandidate(
                format_spec=f"{video['format_id']}+{audio['format_id']}",
                merge_output_format="mp4",
                estimated_size=video_size + audio_size,
                estimated_confident=video_confident and audio_confident,
                quality_label=f"{height}p" if height else "mp4",
                compatibility=2,
                direct_urls=_direct_urls(video, audio),
                size_source=(video_source if video_source == audio_source else f"{video_source}+{audio_source}"),
            )
            raw.append(
                (
                    (
                        _audio_role_score(audio, original_language),
                        2,
                        height,
                        fps,
                        bitrate,
                        _positive_number(audio.get("abr") or audio.get("tbr")),
                    ),
                    candidate,
                )
            )

    raw.sort(key=lambda item: item[0], reverse=True)
    result: list[VideoFormatCandidate] = []
    seen_specs: set[str] = set()
    seen_urls: set[tuple[str, ...]] = set()
    for _score, candidate in raw:
        url_key = tuple(sorted(candidate.direct_urls))
        if candidate.format_spec in seen_specs or (url_key and url_key in seen_urls):
            continue
        seen_specs.add(candidate.format_spec)
        if url_key:
            seen_urls.add(url_key)
        result.append(candidate)
    return result


def build_video_plan_no_squeeze(meta: dict[str, Any]) -> VideoFormatCandidate | None:
    candidates = build_video_candidates(meta, 2**63 - 1)
    return candidates[0] if candidates else None


def apply_probe_if_needed(plan: VideoFormatCandidate | None) -> VideoFormatCandidate | None:
    # Kept as a compatibility shim for callers from older releases.
    return plan


def build_audio_plans_mp3(
    meta: dict[str, Any],
    limit_bytes: int,
    *,
    original_language: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Return original-first audio sources at the highest safe MP3 bitrate.

    Audio is offered only when its converted size can be estimated confidently.
    """
    dur = _duration_sec(meta)
    if not dur:
        return [], "Cannot determine duration, so I can't reliably estimate MP3 size."

    # Choose the highest standard bitrate that fits (with headroom).
    output_bitrate: int | None = None
    candidates = [192, 160, 128, 112, 96, 80, 64, 48, 32]
    for br in candidates:
        est = int(dur * (br * 1000 / 8))
        if est + AUDIO_HEADROOM_BYTES <= limit_bytes:
            output_bitrate = br
            break

    if output_bitrate is None:
        return [], f"Audio is too long to fit into {fmt_bytes(limit_bytes)} even at low bitrate."

    original_language = original_language or original_audio_language(meta)
    formats = [item for item in meta.get("formats", []) or [] if isinstance(item, dict)]
    ranked: list[tuple[tuple[float, ...], str]] = []
    has_audio_role_metadata = False
    for fmt in formats:
        format_id = str(fmt.get("format_id") or "").strip()
        if not format_id or not _format_has_audio(fmt):
            continue
        role_score = _audio_role_score(fmt, original_language)
        has_audio_role_metadata = has_audio_role_metadata or role_score != 2
        audio_only = int(str(fmt.get("vcodec") or "").strip().lower() == "none")
        source_bitrate = _positive_number(fmt.get("abr"))
        if not source_bitrate and audio_only:
            source_bitrate = _positive_number(fmt.get("tbr"))
        codec_compatibility = int(str(fmt.get("acodec") or "").startswith("mp4a"))
        ranked.append(
            (
                (
                    role_score,
                    audio_only,
                    source_bitrate,
                    _number(fmt.get("quality")),
                    codec_compatibility,
                ),
                format_id,
            )
        )

    format_specs = ["bestaudio/best"]
    if has_audio_role_metadata and ranked:
        ranked.sort(key=lambda item: item[0], reverse=True)
        format_specs = []
        seen: set[str] = set()
        for _score, format_id in ranked:
            if format_id in seen:
                continue
            seen.add(format_id)
            format_specs.append(format_id)

    estimated_size = int(dur * (output_bitrate * 1000 / 8))
    return [
        {
            "format_spec": format_spec,
            "merge_output_format": None,
            "mp3_kbps": output_bitrate,
            "estimated_size": estimated_size,
            "estimated_confident": True,
            "quality_label": f"mp3 {output_bitrate}kbps",
        }
        for format_spec in format_specs
    ], None


def build_audio_plan_mp3(
    meta: dict[str, Any],
    limit_bytes: int,
    *,
    original_language: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the preferred MP3 plan while preserving the existing public API."""
    plans, reason = build_audio_plans_mp3(
        meta,
        limit_bytes,
        original_language=original_language,
    )
    return (plans[0] if plans else None), reason
