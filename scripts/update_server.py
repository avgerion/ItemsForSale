#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import dotenv_values

APP_DIR = Path("/opt/items-for-sale")
DATA_DIR = Path("/var/lib/items-for-sale")
VENV_PYTHON = APP_DIR / ".venv" / "bin" / "python"
UPDATE_HELPER = Path("/usr/local/lib/items-for-sale/update_server.py")
UPDATE_STATUS = Path("/run/items-for-sale-update-status.json")
SERVICE_NAME = "items-for-sale"
REMOTE_BRANCH = "origin/main"


class UpdateError(Exception):
    pass


def write_status(state: str, message: str, commit: str = "") -> None:
    UPDATE_STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = UPDATE_STATUS.with_name(f"{UPDATE_STATUS.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "state": state,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "commit": commit,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o644)
    os.replace(temporary, UPDATE_STATUS)


def run_command(
    label: str,
    *args: str,
    timeout: int = 120,
    cwd: Path = APP_DIR,
) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1", "PIP_NO_CACHE_DIR": "1"},
    )
    if result.returncode:
        raise UpdateError(f"{label} failed with exit code {result.returncode}")
    return result.stdout.strip()


def is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def configured_paths() -> tuple[list[Path], Path, bool]:
    config = dotenv_values(APP_DIR / ".env")
    data_dir = Path(config.get("INVENTORY_DATA_DIR") or DATA_DIR).expanduser().resolve()
    database_path = Path(config.get("INVENTORY_DB_PATH") or data_dir / "inventory.sqlite3").expanduser().resolve()
    upload_dir = Path(config.get("INVENTORY_UPLOAD_DIR") or data_dir / "uploads").expanduser().resolve()
    intake_dir = Path(config.get("INVENTORY_INTAKE_DIR") or data_dir / "intake").expanduser().resolve()
    backup_value = (config.get("INVENTORY_BACKUP_DIR") or "").strip()
    backup_dir = (
        Path(backup_value).expanduser().resolve()
        if backup_value
        else (data_dir / "system-update-backups").resolve()
    )
    roots = [path for path in (data_dir, database_path, upload_dir, intake_dir) if path.exists()]
    if not roots:
        raise UpdateError("No inventory data paths exist to back up")
    if backup_dir in roots:
        raise UpdateError("The update backup directory cannot be an inventory data path")
    if is_within(backup_dir, APP_DIR):
        raise UpdateError("The update backup directory must be outside the source tree")
    return roots, backup_dir, bool(backup_value)


def create_data_backup() -> Path:
    roots, backup_dir, configured_backup = configured_paths()
    if configured_backup:
        if not backup_dir.is_dir() or not os.path.ismount(backup_dir):
            raise UpdateError("The configured server backup directory is not mounted")
    else:
        backup_dir.mkdir(parents=True)
    if not backup_dir.is_dir():
        raise UpdateError("The configured update backup location is not a directory")

    backup_name = f"items-for-sale-before-update-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz"
    archive_path = backup_dir / backup_name
    unique_roots: list[Path] = []
    for path in sorted(roots, key=lambda value: len(value.parts)):
        if not any(is_within(path, parent) for parent in unique_roots):
            unique_roots.append(path)

    with tarfile.open(archive_path, "w:gz") as archive:
        for root in unique_roots:
            if root.is_dir():
                archive.add(root, arcname=str(root).lstrip("/"), recursive=False)
                for current, directories, filenames in os.walk(root, followlinks=False):
                    current_path = Path(current)
                    directories[:] = [
                        name
                        for name in directories
                        if not is_within((current_path / name).resolve(), backup_dir)
                    ]
                    for name in directories:
                        directory = current_path / name
                        archive.add(directory, arcname=str(directory).lstrip("/"), recursive=False)
                    for name in filenames:
                        file_path = current_path / name
                        archive.add(file_path, arcname=str(file_path).lstrip("/"), recursive=False)
            else:
                archive.add(root, arcname=str(root).lstrip("/"), recursive=False)
    os.chmod(archive_path, 0o600)
    return archive_path


def wait_for_health(timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8000/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(2)
    raise UpdateError("The application did not become healthy after restart")


def main() -> int:
    if os.geteuid() != 0:
        print("The system updater must run as root.", file=sys.stderr)
        return 1
    if not APP_DIR.is_dir() or not (APP_DIR / ".git").is_dir():
        print("The configured Git checkout is missing.", file=sys.stderr)
        return 1

    os.chdir(APP_DIR)
    stopped = False
    try:
        branch = run_command("Checking the current branch", "git", "branch", "--show-current")
        if branch != "main":
            raise UpdateError("The server checkout must be on the main branch")
        if run_command("Checking the working tree", "git", "status", "--porcelain"):
            raise UpdateError("The server checkout has local changes; refusing to overwrite them")

        print("Checking origin/main for updates.", flush=True)
        run_command("Fetching origin/main", "git", "fetch", "origin", "main", timeout=180)
        previous_commit = run_command("Reading the installed commit", "git", "rev-parse", "HEAD")
        remote_commit = run_command("Reading origin/main", "git", "rev-parse", REMOTE_BRANCH)
        if previous_commit == remote_commit:
            write_status("no_updates", "The server is already up to date.", previous_commit[:12])
            print("No update is available.", flush=True)
            return 0
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", previous_commit, remote_commit],
            cwd=APP_DIR,
            capture_output=True,
            check=False,
        )
        if ancestor.returncode != 0:
            raise UpdateError("The server branch diverges from origin/main; refusing a non-fast-forward update")

        write_status("updating", "Creating a data backup and installing the latest main commit.")
        run_command("Stopping the application", "/usr/bin/systemctl", "stop", SERVICE_NAME)
        stopped = True
        backup_path = create_data_backup()
        print(f"Created data backup {backup_path.name}.", flush=True)
        run_command("Updating source code", "git", "pull", "--ff-only", "origin", "main", timeout=300)
        shutil.copyfile(APP_DIR / "scripts" / "update_server.py", UPDATE_HELPER)
        os.chmod(UPDATE_HELPER, 0o755)
        run_command(
            "Installing application dependencies",
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "-r",
            str(APP_DIR / "requirements.txt"),
            timeout=1800,
        )
        run_command("Starting the application", "/usr/bin/systemctl", "start", SERVICE_NAME)
        stopped = False
        wait_for_health()
        installed_commit = run_command("Reading the installed commit", "git", "rev-parse", "HEAD")
        write_status(
            "updated",
            f"Updated to {installed_commit[:12]} and passed the health check. Backup: {backup_path.name}.",
            installed_commit[:12],
        )
        print("Update completed and the health check passed.", flush=True)
        return 0
    except (OSError, tarfile.TarError, subprocess.TimeoutExpired, UpdateError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr, flush=True)
        if stopped:
            try:
                run_command("Restarting the application after failure", "/usr/bin/systemctl", "start", SERVICE_NAME)
            except (OSError, subprocess.TimeoutExpired, UpdateError) as restart_error:
                print(f"Could not restart the application: {restart_error}", file=sys.stderr, flush=True)
        write_status("failed", "Update failed. Check the systemd journal and use the pre-update backup if needed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
