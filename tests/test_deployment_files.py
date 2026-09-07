import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_production_files_are_tracked_sources() -> None:
    expected = [
        "Dockerfile",
        "docker-compose.yml",
        ".env-example",
        "scripts/docker-entrypoint.sh",
        "scripts/lib-production.sh",
        "scripts/deploy.sh",
        "scripts/update-ytdlp.sh",
        "scripts/systemd/videodownloaderbot-deploy.service",
        "scripts/systemd/videodownloaderbot-deploy.timer",
        "scripts/systemd/videodownloaderbot-yt-dlp-update.service",
        "scripts/systemd/videodownloaderbot-yt-dlp-update.timer",
    ]
    assert all((ROOT / path).is_file() for path in expected)


def test_docker_hardening_and_no_runtime_pip() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    updater = (ROOT / "scripts/update-ytdlp.sh").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "YTDLP_CACHEBUST" in dockerfile
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "OUTPUT_FOLDER: /app/data/downloads" in compose
    assert "pip install" not in updater
    assert "--pull" not in updater
    assert '"$health" == "healthy"' in (ROOT / "scripts/lib-production.sh").read_text(encoding="utf-8")
    assert "/tmp/videodownloaderbot.healthy" in (ROOT / "app/healthcheck.py").read_text(encoding="utf-8")


def test_main_is_import_safe() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import main, threading; assert main.bot is None; assert len(threading.enumerate()) == 1",
        ],
        cwd=ROOT,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0


def test_installer_preserves_existing_environment_and_uses_main() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in installer
    assert 'BRANCH="main"' in installer
    assert 'if [[ ! -f "$env_file" ]]' in installer
    assert 'chmod 600 "$env_file"' in installer
    assert "config core.fileMode false" in installer
    assert "pull --ff-only" in installer
    assert "status --porcelain --untracked-files=no" in installer
    assert "write_docker_files" not in installer
    assert "systemctl enable --now docker" in installer
    assert "docker info" in installer
    assert '"$health" == "healthy"' in installer
