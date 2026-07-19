import logging
import re
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from app.http_utils import requests_session_with_retries
from app.text_utils import fmt_bytes
from app.url_security import MAX_REDIRECTS, domain_matches, validate_public_url, validate_redirect

AUDIO_HEADROOM_BYTES = 1_500_000
LOGGER = logging.getLogger(__name__)


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
) -> dict[str, Any]:
    if not is_youtube_url(url):
        return opts
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
    return opts


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
) -> dict[str, Any]:
    ydl_opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    ydl_opts = apply_youtube_runtime_opts(ydl_opts, url, js_runtimes, remote_components)
    ydl_opts = apply_instagram_stability_opts(
        ydl_opts,
        url,
        impersonate=instagram_impersonate,
        retries=instagram_retries,
        fragment_retries=instagram_fragment_retries,
        socket_timeout=instagram_socket_timeout,
    )
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
    confident=True when size comes from 'filesize' or 'filesize_approx' or URL probe.
    """
    fs = fmt.get("filesize")
    if isinstance(fs, int) and fs > 0:
        return fs, True

    fsa = fmt.get("filesize_approx")
    if isinstance(fsa, int) and fsa > 0:
        return fsa, True

    # Bitrate estimation is NOT confident (we don't use it to block/allow).
    tbr = fmt.get("tbr")  # usually Kbps
    if dur and isinstance(tbr, (int, float)) and tbr > 0:
        est = int(dur * (float(tbr) * 1000.0 / 8.0))
        if est > 0:
            return est, False

    return None, False


def _best_progressive_mp4(meta: dict[str, Any]) -> dict[str, Any] | None:
    """
    Pick the best progressive MP4 (video+audio in one file), no limit filtering.
    """
    dur = _duration_sec(meta)
    best = None
    best_key = None

    for f in meta.get("formats", []) or []:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue

        height = f.get("height") or 0
        fps = f.get("fps") or 0
        tbr = f.get("tbr") or 0

        key = (int(height), int(fps), float(tbr))
        if best is None or key > best_key:
            size, conf = _format_size_bytes(f, dur)
            best = {
                "kind": "progressive",
                "format_spec": str(f.get("format_id")),
                "merge_output_format": None,
                "estimated_size": size,
                "estimated_confident": bool(conf),
                "probe_urls": [f.get("url")] if f.get("url") else [],
                "quality_label": f"{height}p" if height else "mp4",
            }
            best_key = key

    return best


def _best_separate_mp4_m4a(meta: dict[str, Any]) -> dict[str, Any] | None:
    """
    Pick best mp4 video-only + best m4a audio-only, no limit filtering.
    """
    dur = _duration_sec(meta)

    best_v = None
    best_v_key = None
    best_a = None
    best_a_key = None

    for f in meta.get("formats", []) or []:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext")

        # video-only mp4
        if ext == "mp4" and vcodec != "none" and acodec == "none":
            height = f.get("height") or 0
            fps = f.get("fps") or 0
            tbr = f.get("tbr") or 0
            key = (int(height), int(fps), float(tbr))
            if best_v is None or key > best_v_key:
                size, conf = _format_size_bytes(f, dur)
                best_v = {"f": f, "size": size, "conf": conf}
                best_v_key = key

        # audio-only m4a (or mp4 audio-only)
        if vcodec == "none" and acodec != "none" and ext in ("m4a", "mp4"):
            abr = f.get("abr") or f.get("tbr") or 0
            key = float(abr)
            if best_a is None or key > best_a_key:
                size, conf = _format_size_bytes(f, dur)
                best_a = {"f": f, "size": size, "conf": conf}
                best_a_key = key

    if not best_v or not best_a:
        return None

    total_size = None
    confident = False
    if isinstance(best_v["size"], int) and isinstance(best_a["size"], int):
        total_size = int(best_v["size"]) + int(best_a["size"])
        confident = bool(best_v["conf"] and best_a["conf"])

    vf = best_v["f"]
    af = best_a["f"]
    height = vf.get("height") or 0

    urls = []
    if vf.get("url"):
        urls.append(vf.get("url"))
    if af.get("url"):
        urls.append(af.get("url"))

    return {
        "kind": "separate",
        "format_spec": f"{vf.get('format_id')}+{af.get('format_id')}",
        "merge_output_format": "mp4",
        "estimated_size": total_size,
        "estimated_confident": confident,
        "probe_urls": urls,
        "quality_label": f"{height}p" if height else "mp4",
    }


def build_video_plan_no_squeeze(meta: dict[str, Any]) -> dict[str, Any]:
    """
    Build a video plan without reducing quality.
    We will NOT download unless we can confidently prove size <= limit.
    """
    p = _best_progressive_mp4(meta)
    if p:
        return p

    p = _best_separate_mp4_m4a(meta)
    if p:
        return p

    # Fallback: let yt-dlp pick "best"; size will likely be unknown -> we will refuse by policy.
    return {
        "kind": "unknown",
        "format_spec": "best",
        "merge_output_format": None,
        "estimated_size": None,
        "estimated_confident": False,
        "probe_urls": [],
        "quality_label": "best",
    }


def apply_probe_if_needed(plan: dict[str, Any]) -> dict[str, Any]:
    """
    If plan does not have confident size, try probing direct URLs (Range request).
    If we can probe all URLs -> confident total.
    """
    if plan.get("estimated_confident") and isinstance(plan.get("estimated_size"), int):
        return plan

    urls = plan.get("probe_urls") or []
    urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]

    if not urls:
        return plan

    sizes = []
    for u in urls:
        sz = probe_url_size_bytes(u)
        if not isinstance(sz, int) or sz <= 0:
            return plan
        sizes.append(sz)

    total = int(sum(sizes))
    plan["estimated_size"] = total
    plan["estimated_confident"] = True
    return plan


def build_audio_plan_mp3(meta: dict[str, Any], limit_bytes: int) -> tuple[dict[str, Any] | None, str | None]:
    """
    Audio is allowed only if we can confidently keep MP3 under the limit.
    We compute it from duration and pick a bitrate that fits with headroom.
    """
    dur = _duration_sec(meta)
    if not dur:
        return None, "Cannot determine duration, so I can't reliably estimate MP3 size."

    # Choose the highest standard bitrate that fits (with headroom)
    candidates = [192, 160, 128, 112, 96, 80, 64, 48, 32]
    for br in candidates:
        est = int(dur * (br * 1000 / 8))
        if est + AUDIO_HEADROOM_BYTES <= limit_bytes:
            return {
                "format_spec": "bestaudio/best",
                "merge_output_format": None,
                "mp3_kbps": br,
                "estimated_size": est,
                "estimated_confident": True,
                "quality_label": f"mp3 {br}kbps",
            }, None

    return None, f"Audio is too long to fit into {fmt_bytes(limit_bytes)} even at low bitrate."
