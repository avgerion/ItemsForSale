from __future__ import annotations

import html
import io
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from bokeh.embed import components
from bokeh.models import (
    BooleanFilter,
    CDSView,
    ColumnDataSource,
    CustomJS,
    DataTable,
    HTMLTemplateFormatter,
    Select,
    TableColumn,
    TextInput,
)
from bokeh.resources import CDN
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import HTMLResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import UPLOAD_DIR, connect_db, initialize_database
from .security import require_admin

Condition = Literal["new_sealed", "open_box", "used_good", "used_fair", "for_parts_untested"]
Availability = Literal["available", "reserved", "sold", "hidden"]
PhotoKind = Literal["product", "package", "barcode"]
CONDITION_LABELS = {
    "new_sealed": "New/sealed",
    "open_box": "Open box",
    "used_good": "Used/good",
    "used_fair": "Used/fair",
    "for_parts_untested": "For parts/untested",
}
AVAILABILITY_LABELS = {
    "available": "Available",
    "reserved": "Reserved",
    "sold": "Sold",
    "hidden": "Hidden",
}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ItemCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    brand: str = Field(default="", max_length=120)
    model: str = Field(default="", max_length=120)
    barcode: str = Field(default="", max_length=80)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=3000)
    condition: Condition
    quantity: int = Field(default=1, ge=1, le=100_000)
    reserved_quantity: int = Field(default=0, ge=0)
    sold_quantity: int = Field(default=0, ge=0)
    availability: Availability = "available"
    asking_price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    private_notes: str = Field(default="", max_length=5000)
    storage_location: str = Field(default="", max_length=500)
    is_published: bool = False

    @model_validator(mode="after")
    def validate_quantities(self) -> "ItemCreate":
        if self.reserved_quantity + self.sold_quantity > self.quantity:
            raise ValueError("reserved and sold quantities cannot exceed total quantity")
        if self.availability == "sold" and self.sold_quantity != self.quantity:
            raise ValueError("a sold listing must have all units marked sold")
        return self


class ItemUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    barcode: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=3000)
    condition: Condition | None = None
    quantity: int | None = Field(default=None, ge=1, le=100_000)
    reserved_quantity: int | None = Field(default=None, ge=0)
    sold_quantity: int | None = Field(default=None, ge=0)
    availability: Availability | None = None
    asking_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    private_notes: str | None = Field(default=None, max_length=5000)
    storage_location: str | None = Field(default=None, max_length=500)
    is_published: bool | None = None


class CategoryCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)


class SaleEventCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    starts_at: datetime
    ends_at: datetime
    address: str = Field(min_length=1, max_length=500)
    contact_phone: str = Field(default="", max_length=40)
    contact_email: str = Field(default="", max_length=254)

    @model_validator(mode="after")
    def validate_dates(self) -> "SaleEventCreate":
        if normalize_datetime(self.ends_at) <= normalize_datetime(self.starts_at):
            raise ValueError("sale event end must be later than its start")
        return self


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return normalize_datetime(value or utc_now()).isoformat()


def price_to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money_from_cents(value: int) -> float:
    return value / 100


def category_id(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(
        "SELECT id FROM categories WHERE name = ? COLLATE NOCASE", (name.strip(),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail="Choose a category from the managed category list")
    return int(row["id"])


def normalize_availability(
    current: str,
    quantity: int,
    reserved_quantity: int,
    sold_quantity: int,
) -> str:
    if current == "hidden":
        return "hidden"
    if sold_quantity == quantity:
        return "sold"
    if current == "sold":
        return "available"
    if reserved_quantity > 0:
        return "reserved"
    return "available"


def item_record(row: sqlite3.Row, *, include_private: bool) -> dict:
    record = {
        "id": row["id"],
        "name": row["name"],
        "brand": row["brand"],
        "model": row["model"],
        "category": row["category"],
        "description": row["description"],
        "condition": row["condition"],
        "condition_label": CONDITION_LABELS[row["condition"]],
        "quantity": row["quantity"],
        "reserved_quantity": row["reserved_quantity"],
        "sold_quantity": row["sold_quantity"],
        "available_quantity": row["quantity"] - row["reserved_quantity"] - row["sold_quantity"],
        "availability": row["availability"],
        "availability_label": AVAILABILITY_LABELS[row["availability"]],
        "asking_price": money_from_cents(row["asking_price_cents"]),
        "is_published": bool(row["is_published"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_private:
        record.update(
            {
                "barcode": row["barcode"],
                "private_notes": row["private_notes"],
                "storage_location": row["storage_location"],
            }
        )
    return record


def fetch_item(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT items.*, categories.name AS category
        FROM items
        JOIN categories ON categories.id = items.category_id
        WHERE items.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return row


def public_items(search: str = "", category: str = "") -> list[dict]:
    clauses = [
        "items.is_published = 1",
        "items.availability IN ('available', 'reserved')",
        "(items.quantity - items.reserved_quantity - items.sold_quantity) > 0",
    ]
    parameters: list[str] = []
    if search.strip():
        clauses.append(
            "(items.name LIKE ? COLLATE NOCASE OR items.brand LIKE ? COLLATE NOCASE "
            "OR items.model LIKE ? COLLATE NOCASE OR items.description LIKE ? COLLATE NOCASE)"
        )
        value = f"%{search.strip()}%"
        parameters.extend([value, value, value, value])
    if category.strip():
        clauses.append("categories.name = ? COLLATE NOCASE")
        parameters.append(category.strip())

    with connect_db() as connection:
        rows = connection.execute(
            f"""
            SELECT items.*, categories.name AS category
            FROM items
            JOIN categories ON categories.id = items.category_id
            WHERE {' AND '.join(clauses)}
            ORDER BY items.name COLLATE NOCASE
            """,
            parameters,
        ).fetchall()
        result = []
        for row in rows:
            listing = item_record(row, include_private=False)
            photos = connection.execute(
                """
                SELECT id FROM item_photos
                WHERE item_id = ? AND kind = 'product' AND is_public = 1
                ORDER BY created_at, id
                """,
                (row["id"],),
            ).fetchall()
            listing["photo_urls"] = [f"/media/{photo['id']}" for photo in photos]
            result.append(listing)
    return result


def public_events() -> list[dict]:
    now = utc_now()
    now_text = timestamp(now)
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM sale_events WHERE ends_at >= ? ORDER BY starts_at",
            (now_text,),
        ).fetchall()
    events = []
    for row in rows:
        active = row["starts_at"] <= now_text <= row["ends_at"]
        events.append(
            {
                "id": row["id"],
                "title": row["title"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "address": row["address"] if active else None,
                "is_active": active,
                "contact_phone": row["contact_phone"],
                "contact_email": row["contact_email"],
            }
        )
    return events


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Items for Sale",
    description="Private inventory management and a public garage-sale catalog.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    with connect_db() as connection:
        connection.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/public/items")
def list_public_items(
    q: Annotated[str, Query(max_length=200)] = "",
    category: Annotated[str, Query(max_length=80)] = "",
) -> list[dict]:
    return public_items(q, category)


@app.get("/api/public/categories")
def list_public_categories() -> list[str]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT categories.name
            FROM categories JOIN items ON items.category_id = categories.id
            WHERE items.is_published = 1
              AND items.availability IN ('available', 'reserved')
              AND (items.quantity - items.reserved_quantity - items.sold_quantity) > 0
            ORDER BY categories.name COLLATE NOCASE
            """
        ).fetchall()
    return [row["name"] for row in rows]


@app.get("/api/public/events")
def list_public_events() -> list[dict]:
    return public_events()


@app.get("/media/{photo_id}")
def public_photo(photo_id: str) -> Response:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT item_photos.filename, item_photos.media_type
            FROM item_photos JOIN items ON items.id = item_photos.item_id
            WHERE item_photos.id = ?
              AND item_photos.kind = 'product'
              AND item_photos.is_public = 1
              AND items.is_published = 1
              AND items.availability IN ('available', 'reserved')
              AND (items.quantity - items.reserved_quantity - items.sold_quantity) > 0
            """,
            (photo_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = UPLOAD_DIR / row["filename"]
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Photo file not found") from exc
    return Response(content, media_type=row["media_type"], headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/admin/photos/{photo_id}")
def private_photo(photo_id: str, _: Annotated[str, Depends(require_admin)]) -> Response:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT filename, media_type FROM item_photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    try:
        content = (UPLOAD_DIR / row["filename"]).read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Photo file not found") from exc
    return Response(content, media_type=row["media_type"], headers={"X-Content-Type-Options": "nosniff"})


@app.get("/", response_class=HTMLResponse)
def catalog_page() -> str:
    listings = public_items()
    events = public_events()
    categories = ["All categories", *sorted({item["category"] for item in listings}, key=str.casefold)]
    names = [item["name"] for item in listings]
    photo = [
        f'<img src="{html.escape(item["photo_urls"][0], quote=True)}" alt="" width="72" height="54" style="object-fit:cover;border-radius:6px">'
        if item["photo_urls"]
        else ""
        for item in listings
    ]
    contacts = []
    first_contact = next(
        (event for event in events if event["contact_phone"] or event["contact_email"]),
        None,
    )
    for item in listings:
        links = []
        catalog_url = os.environ.get("PUBLIC_CATALOG_URL", "").rstrip("/")
        message = f"Hi, I'm interested in {item['name']}."
        if catalog_url:
            message += f" Browse: {catalog_url}"
        if first_contact and first_contact["contact_phone"]:
            encoded_message = quote(message)
            phone = quote(first_contact["contact_phone"], safe="+")
            links.append(f'<a href="sms:{phone}?body={encoded_message}">Text</a>')
        if first_contact and first_contact["contact_email"]:
            subject = quote(f"Question about {item['name']}")
            body = quote(message)
            email = quote(first_contact["contact_email"], safe="@.+-_")
            links.append(f'<a href="mailto:{email}?subject={subject}&amp;body={body}">Email</a>')
        contacts.append(" · ".join(links) or "—")

    source = ColumnDataSource(
        data={
            "photo": photo,
            "name": names,
            "brand": [item["brand"] for item in listings],
            "model": [item["model"] for item in listings],
            "category": [item["category"] for item in listings],
            "condition": [item["condition_label"] for item in listings],
            "price": [f"${item['asking_price']:.2f}" for item in listings],
            "quantity": [str(item["available_quantity"]) for item in listings],
            "status": [item["availability_label"] for item in listings],
            "contact": contacts,
        }
    )
    filter_model = BooleanFilter(booleans=[True] * len(listings))
    view = CDSView(filter=filter_model)
    search = TextInput(title="Search items", placeholder="Search by name, brand, model, or description")
    category_filter = Select(title="Category", value="All categories", options=categories)
    availability_filter = Select(
        title="Availability",
        value="All statuses",
        options=["All statuses", "Available", "Reserved"],
    )
    callback = CustomJS(
        args={
            "source": source,
            "view": view,
            "search": search,
            "category": category_filter,
            "availability": availability_filter,
        },
        code="""
            const query = search.value.trim().toLowerCase();
            const selectedCategory = category.value;
            const selectedAvailability = availability.value;
            const rows = source.data;
            const filter = view.filter;
            filter.booleans = rows.name.map((name, index) => {
                const categoryMatches = selectedCategory === "All categories" || rows.category[index] === selectedCategory;
                const availabilityMatches = selectedAvailability === "All statuses" || rows.status[index] === selectedAvailability;
                const text = [name, rows.brand[index], rows.model[index], rows.category[index], rows.condition[index]].join(" ").toLowerCase();
                return categoryMatches && availabilityMatches && (!query || text.includes(query));
            });
            filter.change.emit();
        """,
    )
    search.js_on_change("value", callback)
    category_filter.js_on_change("value", callback)
    availability_filter.js_on_change("value", callback)
    table = DataTable(
        source=source,
        view=view,
        columns=[
            TableColumn(field="photo", title="", width=90, formatter=HTMLTemplateFormatter(template="<%= value %>")),
            TableColumn(field="name", title="Item", width=240),
            TableColumn(field="category", title="Category", width=120),
            TableColumn(field="condition", title="Condition", width=150),
            TableColumn(field="price", title="Price", width=100),
            TableColumn(field="quantity", title="Available", width=90),
            TableColumn(field="status", title="Status", width=110),
            TableColumn(field="contact", title="Contact", width=110, formatter=HTMLTemplateFormatter(template="<%= value %>")),
        ],
        sizing_mode="stretch_width",
        height=460,
        index_position=None,
        autosize_mode="fit_viewport",
    )
    script, div = components([search, category_filter, availability_filter, table])

    event_cards = []
    for event in events:
        start = html.escape(event["starts_at"][:16].replace("T", " "))
        end = html.escape(event["ends_at"][:16].replace("T", " "))
        address = f"<p><strong>Address:</strong> {html.escape(event['address'])}</p>" if event["address"] else ""
        event_contacts = []
        catalog_url = os.environ.get("PUBLIC_CATALOG_URL", "").rstrip("/")
        event_message = f"Hi, I'm interested in the garage sale: {event['title']}."
        if catalog_url:
            event_message += f" Browse the catalog: {catalog_url}"
        if event["contact_phone"]:
            event_contacts.append(
                f'<a href="sms:{quote(event["contact_phone"], safe="+")}?body={quote(event_message)}">Text about this sale</a>'
            )
        if event["contact_email"]:
            event_contacts.append(
                f'<a href="mailto:{quote(event["contact_email"], safe="@.+-_")}?subject={quote(event["title"])}&amp;body={quote(event_message)}">Email about this sale</a>'
            )
        contact_html = f"<p>{' · '.join(event_contacts)}</p>" if event_contacts else ""
        event_cards.append(
            f'<article class="event"><h3>{html.escape(event["title"])}</h3><p>{start} – {end} UTC</p>{address}{contact_html}</article>'
        )
    event_section = "".join(event_cards) or "<p>No upcoming garage-sale dates have been posted yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Items for Sale</title>
  {CDN.render()}
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; color: #1d2939; background: #f7f8fa; }}
    body {{ margin: 0; }}
    header {{ padding: 1.5rem max(1rem, calc((100vw - 1100px) / 2)); background: #173b57; color: white; }}
    main {{ max-width: 1100px; margin: auto; padding: 1rem; }}
    .events {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: .8rem; }}
    .event {{ padding: 1rem; border-radius: .6rem; background: white; box-shadow: 0 1px 4px #0001; }}
    @media (max-width: 650px) {{ main {{ padding: .75rem; }} }}
  </style>
</head>
<body>
  <header><h1>Items for Sale</h1><p>Browse available items and find an upcoming garage sale.</p></header>
  <main>
    <section><h2>Garage-sale dates</h2><div class="events">{event_section}</div></section>
    <section><h2>Browse inventory</h2>{div}</section>
  </main>
  {script}
</body>
</html>"""


@app.get("/manage", response_class=HTMLResponse)
def management_page(_: Annotated[str, Depends(require_admin)]) -> HTMLResponse:
    template = Path(__file__).resolve().parent / "templates" / "admin.html"
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.get("/api/admin/items")
def list_admin_items(_: Annotated[str, Depends(require_admin)]) -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT items.*, categories.name AS category
            FROM items JOIN categories ON categories.id = items.category_id
            ORDER BY items.updated_at DESC
            """
        ).fetchall()
        result = [item_record(row, include_private=True) for row in rows]
        for record in result:
            photos = connection.execute(
                """
                SELECT id, kind, is_public, media_type, created_at
                FROM item_photos WHERE item_id = ? ORDER BY created_at
                """,
                (record["id"],),
            ).fetchall()
            record["photos"] = [
                {
                    "id": photo["id"],
                    "kind": photo["kind"],
                    "is_public": bool(photo["is_public"]),
                    "media_type": photo["media_type"],
                    "created_at": photo["created_at"],
                }
                for photo in photos
            ]
    return result


@app.post("/api/admin/items", status_code=status.HTTP_201_CREATED)
def create_item(
    item: ItemCreate,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    now = timestamp()
    item_id = str(uuid.uuid4())
    with connect_db() as connection:
        cat_id = category_id(connection, item.category)
        availability = normalize_availability(
            item.availability, item.quantity, item.reserved_quantity, item.sold_quantity
        )
        connection.execute(
            """
            INSERT INTO items (
                id, name, brand, model, barcode, category_id, description, condition,
                quantity, reserved_quantity, sold_quantity, availability, asking_price_cents,
                private_notes, storage_location, is_published, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                item.name,
                item.brand,
                item.model,
                item.barcode,
                cat_id,
                item.description,
                item.condition,
                item.quantity,
                item.reserved_quantity,
                item.sold_quantity,
                availability,
                price_to_cents(item.asking_price),
                item.private_notes,
                item.storage_location,
                int(item.is_published),
                now,
                now,
            ),
        )
        row = fetch_item(connection, item_id)
    return item_record(row, include_private=True)


@app.patch("/api/admin/items/{item_id}")
def update_item(
    item_id: str,
    update: ItemUpdate,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    changes = update.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one field to update")
    with connect_db() as connection:
        existing = fetch_item(connection, item_id)
        merged = {
            "name": existing["name"],
            "brand": existing["brand"],
            "model": existing["model"],
            "barcode": existing["barcode"],
            "category": existing["category"],
            "description": existing["description"],
            "condition": existing["condition"],
            "quantity": existing["quantity"],
            "reserved_quantity": existing["reserved_quantity"],
            "sold_quantity": existing["sold_quantity"],
            "availability": existing["availability"],
            "asking_price_cents": existing["asking_price_cents"],
            "private_notes": existing["private_notes"],
            "storage_location": existing["storage_location"],
            "is_published": existing["is_published"],
        }
        for key, value in changes.items():
            if value is None:
                raise HTTPException(status_code=422, detail=f"{key} cannot be null")
            if key == "asking_price":
                merged["asking_price_cents"] = price_to_cents(value)
            else:
                merged[key] = value
        if merged["reserved_quantity"] + merged["sold_quantity"] > merged["quantity"]:
            raise HTTPException(status_code=422, detail="reserved and sold quantities cannot exceed total quantity")
        if merged["availability"] == "sold" and merged["sold_quantity"] != merged["quantity"]:
            raise HTTPException(status_code=422, detail="a sold listing must have all units marked sold")
        merged["availability"] = normalize_availability(
            merged["availability"],
            merged["quantity"],
            merged["reserved_quantity"],
            merged["sold_quantity"],
        )
        cat_id = category_id(connection, merged["category"])
        connection.execute(
            """
            UPDATE items SET
                name = ?, brand = ?, model = ?, barcode = ?, category_id = ?, description = ?,
                condition = ?, quantity = ?, reserved_quantity = ?, sold_quantity = ?,
                availability = ?, asking_price_cents = ?, private_notes = ?, storage_location = ?,
                is_published = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                merged["name"],
                merged["brand"],
                merged["model"],
                merged["barcode"],
                cat_id,
                merged["description"],
                merged["condition"],
                merged["quantity"],
                merged["reserved_quantity"],
                merged["sold_quantity"],
                merged["availability"],
                merged["asking_price_cents"],
                merged["private_notes"],
                merged["storage_location"],
                int(bool(merged["is_published"])),
                timestamp(),
                item_id,
            ),
        )
        row = fetch_item(connection, item_id)
    return item_record(row, include_private=True)


@app.get("/api/admin/categories")
def list_admin_categories(_: Annotated[str, Depends(require_admin)]) -> list[str]:
    with connect_db() as connection:
        rows = connection.execute("SELECT name FROM categories ORDER BY name COLLATE NOCASE").fetchall()
    return [row["name"] for row in rows]


@app.post("/api/admin/categories", status_code=status.HTTP_201_CREATED)
def create_category(
    category: CategoryCreate,
    _: Annotated[str, Depends(require_admin)],
) -> dict[str, str]:
    with connect_db() as connection:
        try:
            connection.execute("INSERT INTO categories(name) VALUES (?)", (category.name,))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Category already exists") from exc
    return {"name": category.name}


@app.get("/api/admin/events")
def list_admin_events(_: Annotated[str, Depends(require_admin)]) -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute("SELECT * FROM sale_events ORDER BY starts_at").fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "address": row["address"],
            "contact_phone": row["contact_phone"],
            "contact_email": row["contact_email"],
        }
        for row in rows
    ]


@app.post("/api/admin/events", status_code=status.HTTP_201_CREATED)
def create_sale_event(
    event: SaleEventCreate,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    event_id = str(uuid.uuid4())
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO sale_events
            (id, title, starts_at, ends_at, address, contact_phone, contact_email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event.title,
                timestamp(event.starts_at),
                timestamp(event.ends_at),
                event.address,
                event.contact_phone,
                event.contact_email,
                timestamp(),
            ),
        )
        row = connection.execute("SELECT * FROM sale_events WHERE id = ?", (event_id,)).fetchone()
    return {
        "id": row["id"],
        "title": row["title"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "address": row["address"],
        "contact_phone": row["contact_phone"],
        "contact_email": row["contact_email"],
    }


@app.delete("/api/admin/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sale_event(event_id: str, _: Annotated[str, Depends(require_admin)]) -> Response:
    with connect_db() as connection:
        result = connection.execute("DELETE FROM sale_events WHERE id = ?", (event_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Sale event not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.patch("/api/admin/photos/{photo_id}")
def update_photo_visibility(
    photo_id: str,
    is_public: bool,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    with connect_db() as connection:
        photo = connection.execute("SELECT kind FROM item_photos WHERE id = ?", (photo_id,)).fetchone()
        if photo is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        if is_public and photo["kind"] != "product":
            raise HTTPException(status_code=422, detail="Only product photos can be public")
        connection.execute("UPDATE item_photos SET is_public = ? WHERE id = ?", (int(is_public), photo_id))
    return {"id": photo_id, "is_public": is_public}


@app.post("/api/admin/items/{item_id}/photos", status_code=status.HTTP_201_CREATED)
async def upload_item_photo(
    item_id: str,
    _: Annotated[str, Depends(require_admin)],
    file: Annotated[UploadFile, File()],
    kind: Annotated[PhotoKind, Form()],
    is_public: Annotated[bool, Form()] = False,
) -> dict:
    if is_public and kind != "product":
        raise HTTPException(status_code=422, detail="Only product photos can be public")
    payload = await file.read(MAX_IMAGE_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=422, detail="The uploaded image is empty")
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the 10 MB limit")
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, or WebP image")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > 40_000_000:
                raise HTTPException(status_code=413, detail="Image dimensions exceed the supported limit")
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=415, detail="The upload is not a valid image") from exc

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    photo_id = str(uuid.uuid4())
    filename = f"{photo_id}{Path(file.filename or '').suffix.lower()[:8]}"
    destination = UPLOAD_DIR / filename
    with connect_db() as connection:
        fetch_item(connection, item_id)
        destination.write_bytes(payload)
        try:
            connection.execute(
                """
                INSERT INTO item_photos (id, item_id, kind, filename, media_type, is_public, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (photo_id, item_id, kind, filename, file.content_type, int(is_public), timestamp()),
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return {
        "id": photo_id,
        "item_id": item_id,
        "kind": kind,
        "is_public": is_public,
        "url": f"/media/{photo_id}" if is_public else None,
    }


@app.delete("/api/admin/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_photo(photo_id: str, _: Annotated[str, Depends(require_admin)]) -> Response:
    with connect_db() as connection:
        row = connection.execute("SELECT filename FROM item_photos WHERE id = ?", (photo_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        connection.execute("DELETE FROM item_photos WHERE id = ?", (photo_id,))
        (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

