import logging
import os
from typing import Any

from app.text_utils import fmt_bytes

LOGGER = logging.getLogger(__name__)


def calc_download_progress(d: dict[str, Any], state: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """
    Returns (percent, downloaded_bytes, total_bytes).
    - Prefer fragment-based progress for HLS/DASH.
    - Never show 100% until status == "finished".
    """
    downloaded = d.get("downloaded_bytes")
    total = d.get("total_bytes")

    frag_count = d.get("fragment_count")
    frag_index = d.get("fragment_index")

    if isinstance(frag_count, int) and frag_count > 0 and isinstance(frag_index, int):
        idx = frag_index
        if idx < 0:
            idx = 0
        if idx > frag_count:
            idx = frag_count

        pct = int((idx * 100) / frag_count)
        if pct >= 100:
            pct = 99

        prev = state.get("pct", 0)
        if pct < prev:
            pct = prev
        state["pct"] = pct
        return pct, None, None

    if isinstance(total, int) and total > 0 and isinstance(downloaded, int) and downloaded >= 0:
        pct = int((downloaded * 100) / total)
        if pct >= 100:
            pct = 99

        prev = state.get("pct", 0)
        if pct < prev:
            pct = prev
        state["pct"] = pct
        return pct, downloaded, total

    # Estimate as last resort (not used for pre-check decisions)
    total_est = d.get("total_bytes_estimate")
    if isinstance(total_est, int) and total_est > 0 and isinstance(downloaded, int) and downloaded >= 0:
        pct = int((downloaded * 100) / total_est)
        if pct >= 100:
            pct = 99

        prev = state.get("pct", 0)
        if pct < prev:
            pct = prev
        state["pct"] = pct
        return pct, downloaded, total_est

    return None, downloaded if isinstance(downloaded, int) else None, None


def render_status(
    title: str,
    stage: str,
    pct: int | None,
    downloaded: int | None,
    total: int | None,
    queued_pos: int | None = None,
) -> str:
    if stage == "queued":
        line = "Status: ⏳ Queued"
        if queued_pos is not None:
            line += f" (#{queued_pos})"
        return f"{title}\n\n{line}"

    if stage == "downloading":
        line = "Status: ⬇️ Downloading..."
        if pct is not None:
            line += f" {pct}%"
        if isinstance(downloaded, int) and isinstance(total, int) and total > 0:
            line += f"\n{fmt_bytes(downloaded)} / {fmt_bytes(total)}"
        return f"{title}\n\n{line}"

    if stage == "sending_video":
        line = "Status: ⬆️ Sending video..."
        if pct is not None:
            line += f" {pct}%"
        return f"{title}\n\n{line}"

    if stage == "sending_document":
        line = "Status: ⬆️ Sending document..."
        if pct is not None:
            line += f" {pct}%"
        return f"{title}\n\n{line}"

    if stage == "sending_audio":
        line = "Status: ⬆️ Sending audio..."
        if pct is not None:
            line += f" {pct}%"
        return f"{title}\n\n{line}"

    if stage == "cancelled":
        return f"{title}\n\nStatus: ⛔ Cancelled"

    if stage == "error":
        return f"{title}\n\nStatus: ❌ Error"

    return f"{title}\n\nStatus: ✅ Done!"


def find_file_by_prefix(output_folder: str, prefix: str, prefer_ext: str | None = None) -> str | None:
    try:
        files: list[str] = []
        for filename in os.listdir(output_folder):
            if not filename.startswith(prefix):
                continue
            lower = filename.lower()
            if lower.endswith((".part", ".ytdl", ".tmp", ".temp")):
                continue
            suffix = lower[len(prefix) :]
            if suffix.startswith(".f") and suffix[2:].split(".", 1)[0].isdigit():
                continue
            if os.path.isfile(os.path.join(output_folder, filename)):
                files.append(filename)
        if not files:
            return None

        if prefer_ext:
            for fn in files:
                if fn.lower().endswith(prefer_ext.lower()):
                    fp = os.path.join(output_folder, fn)
                    if os.path.exists(fp):
                        return fp

        best = None
        best_mtime = -1
        for fn in files:
            fp = os.path.join(output_folder, fn)
            try:
                mtime = os.path.getmtime(fp)
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = fp
            except Exception:
                LOGGER.debug("could not stat download candidate path=%s", fp, exc_info=True)

        if best and os.path.exists(best):
            return best

    except Exception:
        LOGGER.exception("downloaded file discovery failed folder=%s prefix=%s", output_folder, prefix)
    return None


def find_downloaded_file(
    info: dict[str, Any],
    output_folder: str,
    fallback_prefix: str,
    prefer_ext: str | None = None,
) -> str | None:
    try:
        req = (info.get("requested_downloads") or [])[0]
        fp = req.get("filepath")
        if fp and os.path.exists(fp):
            return fp
    except Exception:
        LOGGER.debug("requested_downloads did not contain a usable filepath", exc_info=True)

    return find_file_by_prefix(output_folder, fallback_prefix, prefer_ext=prefer_ext)
