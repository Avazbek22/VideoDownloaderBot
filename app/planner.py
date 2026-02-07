import re
from urllib.parse import urlparse
from typing import Any, Dict, Optional, Tuple

import yt_dlp

from app.http_utils import requests_session_with_retries
from app.text_utils import fmt_bytes

AUDIO_HEADROOM_BYTES = 1_500_000


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return any(
        x in host
        for x in (
            "youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
        )
    )


def apply_youtube_runtime_opts(
    opts: Dict[str, Any],
    url: str,
    js_runtimes: Optional[str],
    remote_components: Optional[str],
) -> Dict[str, Any]:
    if not is_youtube_url(url):
        return opts
    if js_runtimes:
        parsed: Dict[str, Dict[str, str]] = {}
        for raw in str(js_runtimes).split(","):
            item = raw.strip()
            if not item:
                continue
            runtime, _, path = item.partition(":")
            runtime = runtime.strip().lower()
            if not runtime:
                continue
            conf: Dict[str, str] = {}
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


def probe_url_size_bytes(url: str, timeout_sec: int = 10) -> Optional[int]:
    """
    Try to get real content size without downloading the file:
    - Send GET with Range: bytes=0-0
    - Parse Content-Range: bytes 0-0/123456
    """
    if not url or not isinstance(url, str):
        return None

    s = requests_session_with_retries()
    try:
        resp = s.get(
            url,
            headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"},
            stream=True,
            timeout=(timeout_sec, timeout_sec),
            allow_redirects=True,
        )
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
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def get_video_meta(
    url: str,
    js_runtimes: Optional[str] = None,
    remote_components: Optional[str] = None,
) -> Dict[str, Any]:
    ydl_opts: Dict[str, Any] = {"quiet": True, "no_warnings": True, "noplaylist": True}
    ydl_opts = apply_youtube_runtime_opts(ydl_opts, url, js_runtimes, remote_components)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _duration_sec(meta: Dict[str, Any]) -> Optional[int]:
    dur = meta.get("duration")
    if isinstance(dur, (int, float)) and dur > 0:
        return int(dur)
    return None


def _format_size_bytes(fmt: Dict[str, Any], dur: Optional[int]) -> Tuple[Optional[int], bool]:
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


def _best_progressive_mp4(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


def _best_separate_mp4_m4a(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


def build_video_plan_no_squeeze(meta: Dict[str, Any]) -> Dict[str, Any]:
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


def apply_probe_if_needed(plan: Dict[str, Any]) -> Dict[str, Any]:
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


def build_audio_plan_mp3(meta: Dict[str, Any], limit_bytes: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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
