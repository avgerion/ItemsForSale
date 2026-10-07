from __future__ import annotations

import base64
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import database
from app.main import app
from app.security import hash_password


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    monkeypatch.setattr(database, "DB_PATH", data_dir / "inventory.sqlite3")
    monkeypatch.setattr(database, "UPLOAD_DIR", data_dir / "uploads")
    monkeypatch.setattr("app.main.UPLOAD_DIR", data_dir / "uploads")
    monkeypatch.setenv("INVENTORY_USERS", "")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers(monkeypatch):
    password = "correct horse battery staple 2026"
    monkeypatch.setenv("INVENTORY_USERS", json.dumps({"owner": hash_password(password)}))
    token = base64.b64encode(f"owner:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def create_item(client: TestClient, headers: dict, **overrides) -> dict:
    item = {
        "name": "LED pendant light",
        "brand": "Example",
        "model": "L-100",
        "barcode": "123456789012",
        "category": "Lighting",
        "description": "Dimmable ceiling light",
        "condition": "open_box",
        "quantity": 3,
        "reserved_quantity": 0,
        "sold_quantity": 0,
        "availability": "available",
        "asking_price": 24.95,
        "private_notes": "Inspect wiring before sale",
        "storage_location": "Garage / shelf B / bin 4",
        "is_published": True,
    }
    item.update(overrides)
    response = client.post("/api/admin/items", json=item, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_admin_fails_closed_until_accounts_are_configured(client):
    assert client.get("/manage").status_code == 503
    assert client.get("/api/admin/items").status_code == 503


def test_public_catalog_returns_only_published_available_inventory(client, admin_headers):
    public_item = create_item(client, admin_headers)
    create_item(client, admin_headers, name="Draft lamp", is_published=False)
    create_item(client, admin_headers, name="Sold drill", category="Tools", availability="sold", sold_quantity=1, quantity=1)
    create_item(client, admin_headers, name="Hidden fan", category="Home", availability="hidden")

    response = client.get("/api/public/items")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == [public_item["name"]]
    listing = response.json()[0]
    assert listing["available_quantity"] == 3
    assert "private_notes" not in listing
    assert "storage_location" not in listing
    assert "barcode" not in listing
    assert "brand" in listing


def test_reserved_units_are_counted_and_filterable(client, admin_headers):
    item = create_item(client, admin_headers, quantity=5, reserved_quantity=2)

    public_listing = client.get("/api/public/items").json()[0]
    assert public_listing["id"] == item["id"]
    assert public_listing["availability"] == "reserved"
    assert public_listing["available_quantity"] == 3

    updated = client.patch(
        f"/api/admin/items/{item['id']}",
        json={"reserved_quantity": 4},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["available_quantity"] == 1


def test_invalid_total_reserved_and_sold_counts_are_rejected(client, admin_headers):
    response = client.post(
        "/api/admin/items",
        json={
            "name": "Over-reserved item",
            "category": "Lighting",
            "condition": "open_box",
            "quantity": 2,
            "reserved_quantity": 2,
            "sold_quantity": 1,
            "asking_price": 10,
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_public_categories_only_include_categories_with_visible_stock(client, admin_headers):
    create_item(client, admin_headers)
    create_item(client, admin_headers, name="Draft tool", category="Tools", is_published=False)
    assert client.get("/api/public/categories").json() == ["Lighting"]


def test_search_and_category_filters(client, admin_headers):
    create_item(client, admin_headers)
    create_item(client, admin_headers, name="Cordless drill", category="Tools", quantity=2, reserved_quantity=1)
    assert [row["name"] for row in client.get("/api/public/items", params={"q": "pendant"}).json()] == [
        "LED pendant light"
    ]
    assert [row["name"] for row in client.get("/api/public/items", params={"category": "Tools"}).json()] == [
        "Cordless drill"
    ]
    assert "Availability" in client.get("/").text


def test_sale_address_is_returned_only_during_event_window(client, admin_headers, monkeypatch):
    now = datetime.now(timezone.utc)
    payload = {
        "title": "Saturday garage sale",
        "starts_at": (now - timedelta(minutes=1)).isoformat(),
        "ends_at": (now + timedelta(minutes=30)).isoformat(),
        "address": "123 Main Street",
        "contact_phone": "+15551234567",
        "contact_email": "seller@example.test",
    }
    response = client.post("/api/admin/events", json=payload, headers=admin_headers)
    assert response.status_code == 201, response.text
    active = client.get("/api/public/events").json()[0]
    assert active["address"] == "123 Main Street"
    assert active["contact_email"] == "seller@example.test"
    public_page = client.get("/")
    assert "123 Main Street" in public_page.text
    assert "sms:" in public_page.text
    assert "mailto:" in public_page.text

    from app import main

    future = datetime.fromisoformat(response.json()["ends_at"]) + timedelta(days=1)
    monkeypatch.setattr(main, "utc_now", lambda: future)
    assert client.get("/api/public/events").json() == []
    assert "123 Main Street" not in client.get("/").text


def test_photo_routes_enforce_public_and_private_access(client, admin_headers):
    item = create_item(client, admin_headers)
    photo_id = str(uuid.uuid4())
    filename = f"{photo_id}.png"
    (database.UPLOAD_DIR / filename).write_bytes(b"png")
    with database.connect_db() as connection:
        connection.execute(
            """
            INSERT INTO item_photos(id, item_id, kind, filename, media_type, is_public, created_at)
            VALUES (?, ?, 'product', ?, 'image/png', 1, ?)
            """,
            (photo_id, item["id"], filename, datetime.now(timezone.utc).isoformat()),
        )
    assert client.get(f"/media/{photo_id}").status_code == 200
    assert client.get(f"/api/admin/photos/{photo_id}").status_code == 401
    assert client.get(f"/api/admin/photos/{photo_id}", headers=admin_headers).status_code == 200
    assert client.get(f"/media/{photo_id}").headers["x-content-type-options"] == "nosniff"

    client.patch(
        f"/api/admin/items/{item['id']}",
        json={"is_published": False},
        headers=admin_headers,
    )
    assert client.get(f"/media/{photo_id}").status_code == 404

def test_bokeh_catalog_page_and_protected_management_page(client, admin_headers):
    create_item(client, admin_headers)
    public_page = client.get("/")
    assert public_page.status_code == 200
    assert "LED pendant light" in public_page.text
    assert "Search items" in public_page.text
    management_page = client.get("/manage", headers=admin_headers)
    assert management_page.status_code == 200
    assert "Stockroom" in management_page.text
    assert 'data-view="overview"' in management_page.text
    assert 'data-view="inventory"' in management_page.text
    assert 'id="photo-form"' in management_page.text
    assert 'id="event-form"' in management_page.text
    assert "@media (max-width: 700px)" in management_page.text


def test_product_photo_upload_validates_kind_visibility_and_content(client, admin_headers):
    item = create_item(client, admin_headers)
    image_bytes = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(image_bytes, format="PNG")
    response = client.post(
        f"/api/admin/items/{item['id']}/photos",
        headers=admin_headers,
        data={"kind": "product", "is_public": "true"},
        files={"file": ("light.png", image_bytes.getvalue(), "image/png")},
    )
    assert response.status_code == 201, response.text
    photo = response.json()
    assert photo["is_public"] is True
    assert client.get(photo["url"]).status_code == 200
    assert client.get(f"/api/admin/items", headers=admin_headers).json()[0]["photos"][0]["id"] == photo["id"]
    unpublished_photo = client.patch(
        f"/api/admin/photos/{photo['id']}?is_public=false",
        headers=admin_headers,
    )
    assert unpublished_photo.status_code == 200
    assert client.get(photo["url"]).status_code == 404

    forbidden = client.post(
        f"/api/admin/items/{item['id']}/photos",
        headers=admin_headers,
        data={"kind": "barcode", "is_public": "true"},
        files={"file": ("barcode.png", image_bytes.getvalue(), "image/png")},
    )
    assert forbidden.status_code == 422


def test_production_admin_requires_https(client, admin_headers, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    response = client.get("/api/admin/items", headers=admin_headers)
    assert response.status_code == 403
