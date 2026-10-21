from __future__ import annotations

import html
import hmac
import io
import json
import os
import sqlite3
import secrets
import shutil
import tempfile
import time
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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask

from .backups import configured_backup_directory, create_inventory_backup, save_server_backup
from .database import INTAKE_UPLOAD_DIR, UPLOAD_DIR, connect_db, initialize_database, purge_expired_intakes
from .providers import ProviderError, provider_statuses, run_provider
from .security import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    AdminLoginRequired,
    configured_users,
    create_session_token,
    hash_password,
    require_admin,
    revoke_session,
    verify_password,
)

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
    manufacturer_part_number: str = Field(default="", max_length=120)
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
    manufacturer_part_number: str | None = Field(default=None, max_length=120)
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


class IntakeCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    barcode: str = Field(default="", max_length=80)
    manufacturer_part_number: str = Field(default="", max_length=120)


class IntakeUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    barcode: str | None = Field(default=None, max_length=80)
    manufacturer_part_number: str | None = Field(default=None, max_length=120)


class IntakeLookup(BaseModel):
    method: Literal[
        "vision_gemini",
        "vision_openai",
        "vision_groqcloud",
        "barcode_local",
        "upcitemdb",
        "upcdatabase",
        "ebay_active",
    ]
    photo_ids: list[str] = Field(default_factory=list, max_length=8)


class IntakeFinalize(BaseModel):
    item: ItemCreate
    selected_field_sources: dict[str, str] = Field(default_factory=dict)
    selected_evidence_ids: list[str] = Field(default_factory=list)


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
                "manufacturer_part_number": row["manufacturer_part_number"],
                "private_notes": row["private_notes"],
                "storage_location": row["storage_location"],
            }
        )
    return record


def intake_record(connection: sqlite3.Connection, intake_id: str) -> dict:
    intake = connection.execute(
        """
        SELECT id, barcode, manufacturer_part_number, created_at, expires_at
        FROM assisted_intakes WHERE id = ? AND expires_at > ?
        """,
        (intake_id, int(time.time())),
    ).fetchone()
    if intake is None:
        raise HTTPException(status_code=404, detail="Intake not found or expired")
    photos = connection.execute(
        """
        SELECT id, kind, media_type, created_at
        FROM assisted_intake_photos WHERE intake_id = ? ORDER BY created_at
        """,
        (intake_id,),
    ).fetchall()
    candidates = connection.execute(
        """
        SELECT id, source_name, source_type, fields_json, evidence_json, retrieved_at, is_fixture
        FROM intake_candidates WHERE intake_id = ? ORDER BY source_name, id
        """,
        (intake_id,),
    ).fetchall()
    candidate_records = []
    for candidate in candidates:
        fields = json.loads(candidate["fields_json"])
        match_notes = fields.pop("_match_notes", "")
        candidate_records.append(
            {
                "id": candidate["id"],
                "source_name": candidate["source_name"],
                "source_type": candidate["source_type"],
                "fields": fields,
                "match_notes": match_notes,
                "price_evidence": json.loads(candidate["evidence_json"]),
                "retrieved_at": candidate["retrieved_at"],
                "is_fixture": bool(candidate["is_fixture"]),
            }
        )
    return {
        "id": intake["id"],
        "barcode": intake["barcode"],
        "manufacturer_part_number": intake["manufacturer_part_number"],
        "created_at": intake["created_at"],
        "expires_at": intake["expires_at"],
        "photos": [
            {
                "id": photo["id"],
                "kind": photo["kind"],
                "media_type": photo["media_type"],
                "created_at": photo["created_at"],
                "url": f"/api/admin/intakes/{intake_id}/photos/{photo['id']}",
            }
            for photo in photos
        ],
        "candidates": candidate_records,
    }


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


def insert_inventory_item(
    connection: sqlite3.Connection,
    item: ItemCreate,
    *,
    is_published: bool | None = None,
) -> tuple[str, dict]:
    now = timestamp()
    item_id = str(uuid.uuid4())
    cat_id = category_id(connection, item.category)
    availability = normalize_availability(
        item.availability, item.quantity, item.reserved_quantity, item.sold_quantity
    )
    connection.execute(
        """
        INSERT INTO items (
            id, name, brand, model, manufacturer_part_number, barcode, category_id, description, condition,
            quantity, reserved_quantity, sold_quantity, availability, asking_price_cents,
            private_notes, storage_location, is_published, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            item.name,
            item.brand,
            item.model,
            item.manufacturer_part_number,
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
            int(item.is_published if is_published is None else is_published),
            now,
            now,
        ),
    )
    return item_id, item_record(fetch_item(connection, item_id), include_private=True)


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


@app.exception_handler(AdminLoginRequired)
async def redirect_to_login(_: Request, __: AdminLoginRequired) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


def login_form_response(*, error: str = "", response_status: int = status.HTTP_200_OK) -> HTMLResponse:
    token = secrets.token_urlsafe(24)
    template = (Path(__file__).resolve().parent / "templates" / "login.html").read_text(encoding="utf-8")
    rendered = template.replace("__CSRF_TOKEN__", html.escape(token, quote=True))
    rendered = rendered.replace("__ERROR__", f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "")
    response = HTMLResponse(rendered, status_code=response_status, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        "items_login_csrf",
        token,
        max_age=600,
        path="/login",
        secure=os.environ.get("APP_ENV", "development").lower() == "production",
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return login_form_response()


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: Annotated[str, Form(min_length=1, max_length=120)],
    password: Annotated[str, Form(min_length=1, max_length=1024)],
    csrf_token: Annotated[str, Form(min_length=1, max_length=120)],
) -> Response:
    csrf_cookie = request.cookies.get("items_login_csrf", "")
    if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_token):
        return login_form_response(error="Your sign-in form expired. Please try again.", response_status=403)

    users = configured_users()
    if not users:
        return login_form_response(
            error="Administration is disabled until an administrator account is configured.",
            response_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    encoded_password = users.get(username, "")
    if not encoded_password or not verify_password(password, encoded_password):
        dummy_hash = hash_password("invalid-user-placeholder", salt=b"items-for-sale")
        verify_password(password, dummy_hash)
        return login_form_response(error="Username or password is incorrect.", response_status=401)

    token = create_session_token(username)
    response = RedirectResponse(url="/manage", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("items_login_csrf", path="/login", samesite="strict")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=os.environ.get("APP_ENV", "development").lower() == "production",
        httponly=True,
        samesite="strict",
    )
    return response


@app.post("/logout")
def logout(request: Request, _: Annotated[str, Depends(require_admin)]) -> Response:
    revoke_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=os.environ.get("APP_ENV", "development").lower() == "production",
        httponly=True,
        samesite="strict",
    )
    return response


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


@app.get("/api/admin/intakes")
def list_intakes(_: Annotated[str, Depends(require_admin)]) -> list[dict]:
    purge_expired_intakes()
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT id FROM assisted_intakes WHERE expires_at > ? ORDER BY created_at DESC",
            (int(time.time()),),
        ).fetchall()
        return [intake_record(connection, row["id"]) for row in rows]


@app.get("/api/admin/intake-providers")
def list_intake_providers(_: Annotated[str, Depends(require_admin)]) -> list[dict[str, str | bool]]:
    return provider_statuses()


@app.get("/api/admin/backups/status")
def backup_status(_: Annotated[str, Depends(require_admin)]) -> dict[str, str | bool]:
    try:
        backup_dir = configured_backup_directory()
    except ValueError:
        return {
            "configured": True,
            "available": False,
            "detail": "The configured backup directory must be outside the database and photo directories.",
        }
    if backup_dir is None:
        return {
            "configured": False,
            "available": False,
            "detail": "Set INVENTORY_BACKUP_DIR to enable backups to a USB drive attached to this server.",
        }
    if not backup_dir.is_dir():
        return {
            "configured": True,
            "available": False,
            "detail": "The configured server backup directory is not mounted or available.",
        }
    if not os.access(backup_dir, os.W_OK):
        return {
            "configured": True,
            "available": False,
            "detail": "The configured server backup directory is not writable by the app.",
        }
    return {
        "configured": True,
        "available": True,
        "detail": "Server backup directory is mounted and writable.",
    }


def _remove_temporary_backup(path: str) -> None:
    Path(path).unlink(missing_ok=True)


@app.get("/api/admin/backups/download")
def download_backup(_: Annotated[str, Depends(require_admin)]) -> FileResponse:
    file_descriptor, filename = tempfile.mkstemp(prefix="items-for-sale-backup-", suffix=".zip")
    os.close(file_descriptor)
    try:
        create_inventory_backup(Path(filename))
    except (OSError, sqlite3.Error) as exc:
        _remove_temporary_backup(filename)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not create backup: {exc}",
        ) from exc
    download_name = f"items-for-sale-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.zip"
    return FileResponse(
        filename,
        media_type="application/zip",
        filename=download_name,
        background=BackgroundTask(_remove_temporary_backup, filename),
    )


@app.post("/api/admin/backups/server")
def create_server_backup(_: Annotated[str, Depends(require_admin)]) -> dict[str, str]:
    try:
        backup_dir = configured_backup_directory()
        if backup_dir is None:
            raise ValueError("Set INVENTORY_BACKUP_DIR before saving a backup to the server.")
        destination = save_server_backup(backup_dir)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not save server backup: {exc}",
        ) from exc
    return {"filename": destination.name}


@app.post("/api/admin/intakes", status_code=status.HTTP_201_CREATED)
def create_intake(
    intake: IntakeCreate,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    purge_expired_intakes()
    intake_id = str(uuid.uuid4())
    created = utc_now()
    expires = int(time.time()) + 48 * 60 * 60
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO assisted_intakes(id, barcode, manufacturer_part_number, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                intake_id,
                intake.barcode,
                intake.manufacturer_part_number,
                timestamp(created),
                expires,
            ),
        )
        return intake_record(connection, intake_id)


@app.get("/api/admin/intakes/{intake_id}")
def get_intake(
    intake_id: str,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    purge_expired_intakes()
    with connect_db() as connection:
        return intake_record(connection, intake_id)


@app.patch("/api/admin/intakes/{intake_id}")
def update_intake(
    intake_id: str,
    update: IntakeUpdate,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    purge_expired_intakes()
    changes = update.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide a barcode or manufacturer part number to update")
    with connect_db() as connection:
        existing = intake_record(connection, intake_id)
        barcode = changes.get("barcode", existing["barcode"])
        manufacturer_part_number = changes.get(
            "manufacturer_part_number", existing["manufacturer_part_number"]
        )
        if barcode is None or manufacturer_part_number is None:
            raise HTTPException(status_code=422, detail="Intake identifiers cannot be null")
        connection.execute(
            """
            UPDATE assisted_intakes
            SET barcode = ?, manufacturer_part_number = ?
            WHERE id = ?
            """,
            (barcode, manufacturer_part_number, intake_id),
        )
        return intake_record(connection, intake_id)


@app.delete("/api/admin/intakes/{intake_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_intake(
    intake_id: str,
    _: Annotated[str, Depends(require_admin)],
) -> Response:
    purge_expired_intakes()
    with connect_db() as connection:
        photos = connection.execute(
            "SELECT filename FROM assisted_intake_photos WHERE intake_id = ?",
            (intake_id,),
        ).fetchall()
        result = connection.execute("DELETE FROM assisted_intakes WHERE id = ?", (intake_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Intake not found")
    for photo in photos:
        (INTAKE_UPLOAD_DIR / photo["filename"]).unlink(missing_ok=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/admin/intakes/{intake_id}/photos", status_code=status.HTTP_201_CREATED)
async def upload_intake_photo(
    intake_id: str,
    _: Annotated[str, Depends(require_admin)],
    file: Annotated[UploadFile, File()],
    kind: Annotated[PhotoKind, Form()],
) -> dict:
    purge_expired_intakes()
    payload = await read_valid_image(file)
    INTAKE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    photo_id = str(uuid.uuid4())
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[file.content_type]
    filename = f"{photo_id}{extension}"
    destination = INTAKE_UPLOAD_DIR / filename
    with connect_db() as connection:
        intake_record(connection, intake_id)
        destination.write_bytes(payload)
        try:
            connection.execute(
                """
                INSERT INTO assisted_intake_photos(id, intake_id, kind, filename, media_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (photo_id, intake_id, kind, filename, file.content_type, timestamp()),
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return {
        "id": photo_id,
        "intake_id": intake_id,
        "kind": kind,
        "media_type": file.content_type,
        "url": f"/api/admin/intakes/{intake_id}/photos/{photo_id}",
    }


@app.get("/api/admin/intakes/{intake_id}/photos/{photo_id}")
def get_intake_photo(
    intake_id: str,
    photo_id: str,
    _: Annotated[str, Depends(require_admin)],
) -> Response:
    purge_expired_intakes()
    with connect_db() as connection:
        photo = connection.execute(
            """
            SELECT filename, media_type FROM assisted_intake_photos
            WHERE id = ? AND intake_id = ?
            """,
            (photo_id, intake_id),
        ).fetchone()
        if photo is None:
            raise HTTPException(status_code=404, detail="Intake photo not found")
    try:
        content = (INTAKE_UPLOAD_DIR / photo["filename"]).read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Intake photo file not found") from exc
    return Response(content, media_type=photo["media_type"], headers={"X-Content-Type-Options": "nosniff"})


@app.post("/api/admin/intakes/{intake_id}/lookup")
def lookup_intake(
    intake_id: str,
    _: Annotated[str, Depends(require_admin)],
    request: IntakeLookup,
) -> dict:
    purge_expired_intakes()
    retrieved_at = timestamp()
    with connect_db() as connection:
        intake = intake_record(connection, intake_id)
        has_identifier = bool(intake["barcode"] or intake["manufacturer_part_number"])
        if request.photo_ids and request.method not in {
            "vision_gemini",
            "vision_openai",
            "vision_groqcloud",
            "barcode_local",
        }:
            raise HTTPException(status_code=422, detail="This provider method does not accept photos.")
        if request.method in {"vision_gemini", "vision_openai", "vision_groqcloud"} and not request.photo_ids:
            raise HTTPException(status_code=422, detail="Select at least one photo before a vision run")
        if request.method == "vision_groqcloud" and len(request.photo_ids) > 3:
            raise HTTPException(status_code=422, detail="Select no more than 3 photos for a GroqCloud vision run")
        if request.method in {"upcitemdb", "upcdatabase", "ebay_active"} and not has_identifier:
            raise HTTPException(
                status_code=422,
                detail="Enter a barcode or manufacturer part number before this lookup",
            )

        selected_photos: list[tuple[str, bytes, str]] = []
        if request.photo_ids:
            if len(set(request.photo_ids)) != len(request.photo_ids):
                raise HTTPException(status_code=422, detail="A photo can only be selected once per provider run")
            placeholders = ",".join("?" for _ in request.photo_ids)
            photo_rows = connection.execute(
                f"""
                SELECT id, kind, filename, media_type
                FROM assisted_intake_photos
                WHERE intake_id = ? AND id IN ({placeholders})
                """,
                [intake_id, *request.photo_ids],
            ).fetchall()
            if len(photo_rows) != len(request.photo_ids):
                raise HTTPException(status_code=422, detail="A selected photo does not belong to this intake")
            for photo in photo_rows:
                try:
                    photo_bytes = (INTAKE_UPLOAD_DIR / photo["filename"]).read_bytes()
                except FileNotFoundError as exc:
                    raise HTTPException(status_code=404, detail="A selected intake photo file is missing") from exc
                selected_photos.append((photo["kind"], photo_bytes, photo["media_type"]))

        try:
            generated_candidates = run_provider(
                request.method,
                barcode=intake["barcode"],
                manufacturer_part_number=intake["manufacturer_part_number"],
                photos=selected_photos,
            )
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        for candidate in generated_candidates:
            candidate["id"] = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO intake_candidates(
                    id, intake_id, source_name, source_type, fields_json, evidence_json, retrieved_at, is_fixture
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate["id"],
                    intake_id,
                    candidate["source_name"],
                    candidate["source_type"],
                    json.dumps(
                        {
                            **candidate["fields"],
                            **({"_match_notes": candidate["match_notes"]} if candidate.get("match_notes") else {}),
                        }
                    ),
                    json.dumps(candidate["price_evidence"]),
                    retrieved_at,
                    0,
                ),
            )
        return intake_record(connection, intake_id)


@app.post("/api/admin/intakes/{intake_id}/create-draft", status_code=status.HTTP_201_CREATED)
def create_draft_from_intake(
    intake_id: str,
    request: IntakeFinalize,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    purge_expired_intakes()
    allowed_fields = {
        "name",
        "brand",
        "model",
        "manufacturer_part_number",
        "barcode",
        "category",
        "description",
    }
    if not set(request.selected_field_sources).issubset(allowed_fields):
        raise HTTPException(status_code=422, detail="Unsupported field in selected sources")
    if len(set(request.selected_evidence_ids)) != len(request.selected_evidence_ids):
        raise HTTPException(status_code=422, detail="Price evidence may only be selected once")

    copied_files: list[Path] = []
    staged_files: list[Path] = []
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with connect_db() as connection:
            intake_record(connection, intake_id)
            candidate_ids = set(request.selected_field_sources.values())
            selected_candidates: dict[str, sqlite3.Row] = {}
            for candidate_id in candidate_ids:
                candidate = connection.execute(
                    """
                    SELECT id, source_name, source_type, fields_json, evidence_json, retrieved_at
                    FROM intake_candidates WHERE id = ? AND intake_id = ?
                    """,
                    (candidate_id, intake_id),
                ).fetchone()
                if candidate is None:
                    raise HTTPException(status_code=422, detail="Selected result does not belong to this intake")
                selected_candidates[candidate_id] = candidate

            for field_name, candidate_id in request.selected_field_sources.items():
                fields = json.loads(selected_candidates[candidate_id]["fields_json"])
                if field_name not in fields:
                    raise HTTPException(status_code=422, detail=f"Selected result does not contain {field_name}")

            evidence_by_id: dict[str, tuple[sqlite3.Row, dict]] = {}
            all_candidates = connection.execute(
                """
                SELECT id, source_name, source_type, fields_json, evidence_json, retrieved_at
                FROM intake_candidates WHERE intake_id = ?
                """,
                (intake_id,),
            ).fetchall()
            for candidate in all_candidates:
                for evidence in json.loads(candidate["evidence_json"]):
                    evidence_by_id[evidence["id"]] = (candidate, evidence)
            for evidence_id in request.selected_evidence_ids:
                if evidence_id not in evidence_by_id:
                    raise HTTPException(status_code=422, detail="Selected price evidence does not belong to this intake")

            item_id, _ = insert_inventory_item(connection, request.item, is_published=False)
            for field_name, candidate_id in request.selected_field_sources.items():
                candidate = selected_candidates[candidate_id]
                source_value = json.loads(candidate["fields_json"])[field_name]
                connection.execute(
                    """
                    INSERT INTO item_identification_sources(
                        id, item_id, field_name, source_name, source_type, source_value, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        item_id,
                        field_name,
                        candidate["source_name"],
                        candidate["source_type"],
                        str(source_value),
                        candidate["retrieved_at"],
                    ),
                )

            for evidence_id in request.selected_evidence_ids:
                candidate, evidence = evidence_by_id[evidence_id]
                amount = Decimal(evidence["amount"])
                connection.execute(
                    """
                    INSERT INTO item_price_evidence(
                        id, item_id, evidence_type, title, source_name, product_url,
                        amount_cents, currency, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        item_id,
                        evidence["evidence_type"],
                        evidence["title"],
                        candidate["source_name"],
                        evidence.get("product_url", ""),
                        price_to_cents(amount),
                        evidence.get("currency", "USD"),
                        candidate["retrieved_at"],
                    ),
                )

            staged_photos = connection.execute(
                "SELECT id, kind, filename, media_type FROM assisted_intake_photos WHERE intake_id = ?",
                (intake_id,),
            ).fetchall()
            for photo in staged_photos:
                source = INTAKE_UPLOAD_DIR / photo["filename"]
                if not source.is_file():
                    raise HTTPException(status_code=404, detail="A staged intake photo is missing")
                suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[photo["media_type"]]
                filename = f"{photo['id']}{suffix}"
                destination = UPLOAD_DIR / filename
                shutil.copyfile(source, destination)
                copied_files.append(destination)
                staged_files.append(source)
                connection.execute(
                    """
                    INSERT INTO item_photos(id, item_id, kind, filename, media_type, is_public, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (photo["id"], item_id, photo["kind"], filename, photo["media_type"], timestamp()),
                )
            connection.execute("DELETE FROM assisted_intakes WHERE id = ?", (intake_id,))
    except Exception:
        for path in copied_files:
            path.unlink(missing_ok=True)
        raise

    for path in staged_files:
        path.unlink(missing_ok=True)
    with connect_db() as connection:
        item_result = item_record(fetch_item(connection, item_id), include_private=True)
        item_result["photos"] = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "is_public": bool(row["is_public"]),
                "media_type": row["media_type"],
                "created_at": row["created_at"],
            }
            for row in connection.execute(
                "SELECT id, kind, is_public, media_type, created_at FROM item_photos WHERE item_id = ?",
                (item_id,),
            ).fetchall()
        ]
        item_result["price_evidence"] = [
            {
                "id": row["id"],
                "evidence_type": row["evidence_type"],
                "title": row["title"],
                "source_name": row["source_name"],
                "product_url": row["product_url"],
                "amount": money_from_cents(row["amount_cents"]),
                "currency": row["currency"],
                "retrieved_at": row["retrieved_at"],
            }
            for row in connection.execute(
                "SELECT * FROM item_price_evidence WHERE item_id = ?",
                (item_id,),
            ).fetchall()
        ]
    return item_result


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
            evidence = connection.execute(
                "SELECT * FROM item_price_evidence WHERE item_id = ? ORDER BY retrieved_at DESC",
                (record["id"],),
            ).fetchall()
            record["price_evidence"] = [
                {
                    "id": row["id"],
                    "evidence_type": row["evidence_type"],
                    "title": row["title"],
                    "source_name": row["source_name"],
                    "product_url": row["product_url"],
                    "amount": money_from_cents(row["amount_cents"]),
                    "currency": row["currency"],
                    "retrieved_at": row["retrieved_at"],
                }
                for row in evidence
            ]
            identification = connection.execute(
                """
                SELECT field_name, source_name, source_value, retrieved_at
                FROM item_identification_sources WHERE item_id = ?
                ORDER BY field_name
                """,
                (record["id"],),
            ).fetchall()
            record["identification_sources"] = [
                {
                    "field_name": row["field_name"],
                    "source_name": row["source_name"],
                    "source_value": row["source_value"],
                    "retrieved_at": row["retrieved_at"],
                }
                for row in identification
            ]
    return result


@app.post("/api/admin/items", status_code=status.HTTP_201_CREATED)
def create_item(
    item: ItemCreate,
    _: Annotated[str, Depends(require_admin)],
) -> dict:
    with connect_db() as connection:
        _, record = insert_inventory_item(connection, item)
        return record


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
            "manufacturer_part_number": existing["manufacturer_part_number"],
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
                name = ?, brand = ?, model = ?, manufacturer_part_number = ?, barcode = ?, category_id = ?, description = ?,
                condition = ?, quantity = ?, reserved_quantity = ?, sold_quantity = ?,
                availability = ?, asking_price_cents = ?, private_notes = ?, storage_location = ?,
                is_published = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                merged["name"],
                merged["brand"],
                merged["model"],
                merged["manufacturer_part_number"],
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


async def read_valid_image(file: UploadFile) -> bytes:
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
    return payload


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
    payload = await read_valid_image(file)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    photo_id = str(uuid.uuid4())
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[file.content_type]
    filename = f"{photo_id}{extension}"
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
