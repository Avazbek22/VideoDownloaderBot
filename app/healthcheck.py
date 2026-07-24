from __future__ import annotations

import os
import time
from pathlib import Path

HEALTH_MARKER = Path("/tmp/videodownloaderbot.healthy")
MAX_AGE_SECONDS = 120


def main() -> int:
    marker = Path(os.getenv("HEALTH_MARKER") or HEALTH_MARKER)
    if not marker.is_file():
        return 1
    return 0 if time.time() - marker.stat().st_mtime < MAX_AGE_SECONDS else 1


if __name__ == "__main__":
    raise SystemExit(main())
