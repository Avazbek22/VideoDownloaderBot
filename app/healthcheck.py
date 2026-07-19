from __future__ import annotations

import os
import time
from pathlib import Path


def main() -> int:
    output = Path(os.getenv("OUTPUT_FOLDER") or "/app/data/downloads")
    marker = output.parent / ".healthy"
    if not marker.is_file():
        return 1
    return 0 if time.time() - marker.stat().st_mtime < 900 else 1


if __name__ == "__main__":
    raise SystemExit(main())
