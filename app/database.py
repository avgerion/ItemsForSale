from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR = Path(os.environ.get("INVENTORY_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
DB_PATH = Path(os.environ.get("INVENTORY_DB_PATH", DATA_DIR / "inventory.sqlite3")).resolve()
UPLOAD_DIR = Path(os.environ.get("INVENTORY_UPLOAD_DIR", DATA_DIR / "uploads")).resolve()
INTAKE_UPLOAD_DIR = Path(os.environ.get("INVENTORY_INTAKE_DIR", DATA_DIR / "intake")).resolve()
DEFAULT_OWNER = os.environ.get("INVENTORY_DEFAULT_OWNER", "owner").strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_username TEXT NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE(owner_username, name)
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    owner_username TEXT NOT NULL,
    name TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    manufacturer_part_number TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    description TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL CHECK (condition IN ('new_sealed', 'open_box', 'used_good', 'used_fair', 'for_parts_untested')),
    quantity INTEGER NOT NULL CHECK (quantity >= 1),
    reserved_quantity INTEGER NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),
    sold_quantity INTEGER NOT NULL DEFAULT 0 CHECK (sold_quantity >= 0),
    availability TEXT NOT NULL CHECK (availability IN ('available', 'reserved', 'sold', 'hidden')),
    asking_price_cents INTEGER NOT NULL CHECK (asking_price_cents >= 0),
    private_notes TEXT NOT NULL DEFAULT '',
    storage_location TEXT NOT NULL DEFAULT '',
    is_published INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (reserved_quantity + sold_quantity <= quantity)
);

CREATE TABLE IF NOT EXISTS item_photos (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('product', 'package', 'barcode')),
    filename TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    is_public INTEGER NOT NULL DEFAULT 0 CHECK (is_public IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_events (
    id TEXT PRIMARY KEY,
    owner_username TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    address TEXT NOT NULL,
    contact_phone TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_id_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assisted_intakes (
    id TEXT PRIMARY KEY,
    owner_username TEXT NOT NULL,
    barcode TEXT NOT NULL DEFAULT '',
    manufacturer_part_number TEXT NOT NULL DEFAULT '',
    lowes_product_number TEXT NOT NULL DEFAULT '',
    home_depot_product_number TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assisted_intake_photos (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES assisted_intakes(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('product', 'package', 'barcode')),
    filename TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intake_candidates (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES assisted_intakes(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    is_fixture INTEGER NOT NULL DEFAULT 0 CHECK (is_fixture IN (0, 1))
);

CREATE TABLE IF NOT EXISTS item_price_evidence (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('retail_reference', 'active_listing', 'completed_sale')),
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    product_url TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_identification_sources (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_product_links (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    store TEXT NOT NULL CHECK (store IN ('home_depot', 'lowes', 'ebay')),
    identifier TEXT NOT NULL,
    product_url TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def connect_db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    if not DEFAULT_OWNER:
        raise RuntimeError("INVENTORY_DEFAULT_OWNER must be a configured username")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    INTAKE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as connection:
        connection.executescript(SCHEMA)
        for table_name in ("categories", "items", "sale_events", "assisted_intakes"):
            existing_columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if "owner_username" not in existing_columns:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                f"UPDATE {table_name} SET owner_username = ? WHERE owner_username = ''",
                (DEFAULT_OWNER,),
            )
        _rebuild_categories_with_workspace_uniqueness(connection)
        for table_name in ("items", "assisted_intakes"):
            existing_columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if "manufacturer_part_number" not in existing_columns:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN manufacturer_part_number TEXT NOT NULL DEFAULT ''"
                )
        intake_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(assisted_intakes)")
        }
        for column in ("lowes_product_number", "home_depot_product_number"):
            if column not in intake_columns:
                connection.execute(
                    f"ALTER TABLE assisted_intakes ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        raw_users = os.environ.get("INVENTORY_USERS", "")
        try:
            configured_users = json.loads(raw_users) if raw_users else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("INVENTORY_USERS must be valid JSON before the database can initialize") from exc
        if not isinstance(configured_users, dict) or any(
            not isinstance(username, str) for username in configured_users
        ):
            raise RuntimeError("INVENTORY_USERS must be a JSON object keyed by usernames")
        configured_usernames = list(configured_users)
        if configured_usernames and DEFAULT_OWNER not in configured_users:
            raise RuntimeError("INVENTORY_DEFAULT_OWNER must name an account in INVENTORY_USERS")
        owners = list(dict.fromkeys([DEFAULT_OWNER, *configured_usernames]))
        connection.executemany(
            "INSERT OR IGNORE INTO categories(owner_username, name) VALUES (?, ?)",
            [(owner, name) for owner in owners for name in ("Lighting", "Tools", "Home")],
        )
        item_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(items)")
        }
        if "updated_at" in item_columns:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_owner_updated ON items(owner_username, updated_at)"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_intakes_owner_expiry ON assisted_intakes(owner_username, expires_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_owner_start ON sale_events(owner_username, starts_at)"
        )
    purge_expired_intakes()


def _rebuild_categories_with_workspace_uniqueness(connection: sqlite3.Connection) -> None:
    for index in connection.execute("PRAGMA index_list(categories)").fetchall():
        if not index["unique"]:
            continue
        columns = [
            row["name"] for row in connection.execute(f"PRAGMA index_info({index['name']})").fetchall()
        ]
        if columns != ["name"]:
            continue

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            CREATE TABLE categories_replacement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_username TEXT NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                UNIQUE(owner_username, name)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO categories_replacement(id, owner_username, name)
            SELECT id, owner_username, name FROM categories
            """
        )
        connection.execute("DROP TABLE categories")
        connection.execute("ALTER TABLE categories_replacement RENAME TO categories")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        break


def purge_expired_intakes(now_epoch: int | None = None) -> None:
    current_time = int(time.time()) if now_epoch is None else now_epoch
    with connect_db() as connection:
        expired_photos = connection.execute(
            """
            SELECT assisted_intake_photos.filename
            FROM assisted_intake_photos
            JOIN assisted_intakes ON assisted_intakes.id = assisted_intake_photos.intake_id
            WHERE assisted_intakes.expires_at <= ?
            """,
            (current_time,),
        ).fetchall()
        connection.execute("DELETE FROM assisted_intakes WHERE expires_at <= ?", (current_time,))
    for row in expired_photos:
        (INTAKE_UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
