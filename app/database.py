from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR = Path(os.environ.get("INVENTORY_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
DB_PATH = Path(os.environ.get("INVENTORY_DB_PATH", DATA_DIR / "inventory.sqlite3")).resolve()
UPLOAD_DIR = Path(os.environ.get("INVENTORY_UPLOAD_DIR", DATA_DIR / "uploads")).resolve()

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
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
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    address TEXT NOT NULL,
    contact_phone TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (ends_at > starts_at)
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT OR IGNORE INTO categories(name) VALUES (?)",
            [("Lighting",), ("Tools",), ("Home",)],
        )
