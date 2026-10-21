from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import database


def configured_backup_directory() -> Path | None:
    configured = os.environ.get("INVENTORY_BACKUP_DIR", "").strip()
    if not configured:
        return None

    backup_dir = Path(configured).expanduser().resolve()
    source_directories = (
        database.DATA_DIR.resolve(),
        database.DB_PATH.parent.resolve(),
        database.UPLOAD_DIR.resolve(),
        database.INTAKE_UPLOAD_DIR.resolve(),
    )
    if any(backup_dir == source or backup_dir.is_relative_to(source) for source in source_directories):
        raise ValueError("The server backup directory must be outside the database and photo directories.")
    return backup_dir


def create_inventory_backup(archive_path: Path) -> None:
    if not database.DB_PATH.is_file():
        raise FileNotFoundError("The inventory database is not available.")
    if not database.UPLOAD_DIR.is_dir() or not database.INTAKE_UPLOAD_DIR.is_dir():
        raise FileNotFoundError("One or more inventory photo directories are not available.")

    snapshot_fd, snapshot_name = tempfile.mkstemp(
        prefix=".inventory-database-snapshot-",
        suffix=".sqlite3",
        dir=archive_path.parent,
    )
    os.close(snapshot_fd)
    snapshot_path = Path(snapshot_name)
    try:
        with closing(sqlite3.connect(f"{database.DB_PATH.as_uri()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot_path)) as snapshot:
                source.backup(snapshot)

        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(snapshot_path, "inventory.sqlite3")
            archive.writestr(
                "backup-info.json",
                json.dumps(
                    {
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "database": "inventory.sqlite3",
                        "photo_directories": ["uploads/", "intake/"],
                    },
                    indent=2,
                ),
            )
            _add_directory(archive, database.UPLOAD_DIR, "uploads")
            _add_directory(archive, database.INTAKE_UPLOAD_DIR, "intake")
    finally:
        snapshot_path.unlink(missing_ok=True)


def save_server_backup(backup_dir: Path) -> Path:
    if not backup_dir.is_dir():
        raise FileNotFoundError("The configured server backup directory is not mounted or available.")
    if not os.access(backup_dir, os.W_OK):
        raise PermissionError("The configured server backup directory is not writable.")

    destination = backup_dir / "items-for-sale-backup.zip"
    temp_fd, temp_name = tempfile.mkstemp(prefix=".items-for-sale-backup-", suffix=".zip", dir=backup_dir)
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        create_inventory_backup(temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination


def _add_directory(archive: zipfile.ZipFile, source: Path, archive_root: str) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        archive.write(path, f"{archive_root}/{path.relative_to(source).as_posix()}")
