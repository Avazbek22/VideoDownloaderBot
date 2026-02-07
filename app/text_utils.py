import re
from typing import Optional


def youtube_url_validation(url: str):
    youtube_regex = (
        r"(https?://)?(www\.)?"
        r"(youtube|youtu|youtube-nocookie)\.(com|be)/"
        r"(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})"
    )
    return re.match(youtube_regex, url)


def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(https?://\S+)", text.strip())
    if not m:
        return None
    url = m.group(1).strip()
    url = url.rstrip(").,]}>\"'")
    return url


def strip_hashtags(s: str) -> str:
    # Remove hashtag tokens like #tag, #слово
    s = re.sub(r"(?<!\w)#[\w\-\_]+", "", s, flags=re.UNICODE)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def sanitize_filename_base(title: str, max_len: int = 120) -> str:
    title = strip_hashtags(title)

    # Windows forbidden chars + control chars
    title = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", title)
    title = title.replace("\n", " ").replace("\r", " ").strip()

    # Windows hates trailing dots/spaces
    title = title.rstrip(". ").strip()

    if not title:
        title = "video"

    if len(title) > max_len:
        title = title[:max_len].rstrip()

    return title


def fmt_bytes(n: Optional[int]) -> str:
    if not isinstance(n, int) or n < 0:
        return "unknown"
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.1f} {units[i]}"

