from __future__ import annotations

import json
import os
import re
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


def create_inventory_backup(archive_path: Path, *, owner_username: str | None = None) -> None:
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
                if owner_username is not None:
                    _scope_snapshot(snapshot, owner_username)
                    item_photos = {
                        row[0] for row in snapshot.execute("SELECT filename FROM item_photos").fetchall()
                    }
                    intake_photos = {
                        row[0]
                        for row in snapshot.execute("SELECT filename FROM assisted_intake_photos").fetchall()
                    }
                else:
                    item_photos = None
                    intake_photos = None

        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(snapshot_path, "inventory.sqlite3")
            backup_info: dict[str, object] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": "inventory.sqlite3",
                "photo_directories": ["uploads/", "intake/"],
            }
            if owner_username is not None:
                backup_info["workspace"] = owner_username
            archive.writestr("backup-info.json", json.dumps(backup_info, indent=2))
            _add_directory(archive, database.UPLOAD_DIR, "uploads", item_photos)
            _add_directory(archive, database.INTAKE_UPLOAD_DIR, "intake", intake_photos)
    finally:
        snapshot_path.unlink(missing_ok=True)


def save_server_backup(backup_dir: Path, *, owner_username: str | None = None) -> Path:
    if not backup_dir.is_dir():
        raise FileNotFoundError("The configured server backup directory is not mounted or available.")
    if not os.access(backup_dir, os.W_OK):
        raise PermissionError("The configured server backup directory is not writable.")

    backup_label = f"-{_safe_owner_name(owner_username)}" if owner_username else ""
    destination = backup_dir / f"items-for-sale{backup_label}-backup.zip"
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".items-for-sale{backup_label}-backup-", suffix=".zip", dir=backup_dir)
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        create_inventory_backup(temp_path, owner_username=owner_username)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination


def _scope_snapshot(connection: sqlite3.Connection, owner_username: str) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    with connection:
        connection.execute("DELETE FROM admin_sessions")
        connection.execute(
            "DELETE FROM item_price_evidence WHERE item_id NOT IN (SELECT id FROM items WHERE owner_username = ?)",
            (owner_username,),
        )
        connection.execute(
            "DELETE FROM item_identification_sources WHERE item_id NOT IN (SELECT id FROM items WHERE owner_username = ?)",
            (owner_username,),
        )
        connection.execute(
            "DELETE FROM item_photos WHERE item_id NOT IN (SELECT id FROM items WHERE owner_username = ?)",
            (owner_username,),
        )
        connection.execute(
            "DELETE FROM intake_candidates WHERE intake_id NOT IN (SELECT id FROM assisted_intakes WHERE owner_username = ?)",
            (owner_username,),
        )
        connection.execute(
            "DELETE FROM assisted_intake_photos WHERE intake_id NOT IN (SELECT id FROM assisted_intakes WHERE owner_username = ?)",
            (owner_username,),
        )
        connection.execute("DELETE FROM items WHERE owner_username != ?", (owner_username,))
        connection.execute("DELETE FROM assisted_intakes WHERE owner_username != ?", (owner_username,))
        connection.execute("DELETE FROM sale_events WHERE owner_username != ?", (owner_username,))
        connection.execute(
            "DELETE FROM categories WHERE id NOT IN (SELECT category_id FROM items)"
        )
    connection.commit()


def _safe_owner_name(owner_username: str | None) -> str:
    if owner_username is None:
        return ""
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", owner_username).strip("-")
    return sanitized or "workspace"


def _add_directory(
    archive: zipfile.ZipFile,
    source: Path,
    archive_root: str,
    selected_filenames: set[str] | None = None,
) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if selected_filenames is not None and path.name not in selected_filenames:
            continue
        archive.write(path, f"{archive_root}/{path.relative_to(source).as_posix()}")
