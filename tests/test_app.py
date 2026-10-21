from __future__ import annotations

import base64
import io
import json
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import database
from app.main import app
from app import providers
from app.providers import ProviderError, _normalize_upcitemdb, provider_statuses, run_provider
from app.security import hash_password


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    monkeypatch.setattr(database, "DB_PATH", data_dir / "inventory.sqlite3")
    monkeypatch.setattr(database, "UPLOAD_DIR", data_dir / "uploads")
    monkeypatch.setattr(database, "INTAKE_UPLOAD_DIR", data_dir / "intake")
    monkeypatch.setattr("app.main.UPLOAD_DIR", data_dir / "uploads")
    monkeypatch.setattr("app.main.INTAKE_UPLOAD_DIR", data_dir / "intake")
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


def test_admin_can_download_database_and_all_photo_directories(client, admin_headers, tmp_path):
    item = create_item(client, admin_headers, is_published=False)
    database.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    database.INTAKE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (database.UPLOAD_DIR / "item-photo.jpg").write_bytes(b"item-photo")
    (database.INTAKE_UPLOAD_DIR / "staged-photo.jpg").write_bytes(b"staged-photo")

    response = client.get("/api/admin/backups/download", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "items-for-sale-backup-" in response.headers["content-disposition"]

    archive_path = tmp_path / "download.zip"
    archive_path.write_bytes(response.content)
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "inventory.sqlite3",
            "backup-info.json",
            "uploads/item-photo.jpg",
            "intake/staged-photo.jpg",
        }
        assert archive.read("uploads/item-photo.jpg") == b"item-photo"
        assert archive.read("intake/staged-photo.jpg") == b"staged-photo"
        backup_info = json.loads(archive.read("backup-info.json"))
        assert set(backup_info["photo_directories"]) == {"uploads/", "intake/"}
        database_path = tmp_path / "restored.sqlite3"
        database_path.write_bytes(archive.read("inventory.sqlite3"))

    with sqlite3.connect(database_path) as restored:
        row = restored.execute("SELECT id, name FROM items WHERE id = ?", (item["id"],)).fetchone()
    assert row == (item["id"], item["name"])


def test_server_backup_requires_mounted_configured_directory_and_replaces_zip(
    client, admin_headers, tmp_path, monkeypatch
):
    backup_status = client.get("/api/admin/backups/status", headers=admin_headers)
    assert backup_status.status_code == 200
    assert backup_status.json()["available"] is False
    unavailable = client.post("/api/admin/backups/server", headers=admin_headers)
    assert unavailable.status_code == 503
    assert "INVENTORY_BACKUP_DIR" in unavailable.json()["detail"]

    server_usb = tmp_path / "server-usb"
    server_usb.mkdir()
    existing_backup = server_usb / "items-for-sale-backup.zip"
    existing_backup.write_bytes(b"old backup")
    monkeypatch.setenv("INVENTORY_BACKUP_DIR", str(server_usb))

    available = client.get("/api/admin/backups/status", headers=admin_headers)
    assert available.json()["available"] is True
    saved = client.post("/api/admin/backups/server", headers=admin_headers)
    assert saved.status_code == 200
    assert saved.json()["filename"] == existing_backup.name

    with zipfile.ZipFile(existing_backup) as archive:
        assert "inventory.sqlite3" in archive.namelist()
        assert json.loads(archive.read("backup-info.json"))["database"] == "inventory.sqlite3"


def test_backup_endpoints_require_admin(client, admin_headers):
    assert client.get("/api/admin/backups/status").status_code == 401
    assert client.get("/api/admin/backups/download").status_code == 401
    assert client.post("/api/admin/backups/server").status_code == 401


def test_database_upgrade_adds_manufacturer_part_number_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "inventory.sqlite3")
    monkeypatch.setattr(database, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(database, "INTAKE_UPLOAD_DIR", tmp_path / "intake")
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.execute("CREATE TABLE items (id TEXT PRIMARY KEY, model TEXT, barcode TEXT)")
        connection.execute(
            "CREATE TABLE assisted_intakes (id TEXT PRIMARY KEY, barcode TEXT, created_at TEXT, expires_at INTEGER)"
        )

    database.initialize_database()

    with database.connect_db() as connection:
        for table in ("items", "assisted_intakes"):
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert "manufacturer_part_number" in columns


def test_admin_fails_closed_until_accounts_are_configured(client):
    assert client.get("/manage").status_code == 503
    assert client.get("/api/admin/items").status_code == 503


def test_admin_page_redirects_to_login_when_accounts_are_configured(client, admin_headers):
    response = client.get("/manage")
    assert response.status_code == 200
    assert response.url.path == "/login"
    assert "Sign in to manage your inventory" in response.text


def test_login_creates_session_and_logout_clears_it(client, admin_headers):
    client.get("/api/admin/items", headers=admin_headers)
    login_page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert csrf

    login_response = client.post(
        "/login",
        data={
            "username": "owner",
            "password": "correct horse battery staple 2026",
            "csrf_token": csrf.group(1),
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/manage"
    cookie = client.cookies.get("items_admin_session")
    assert cookie
    assert client.get("/api/admin/items").status_code == 200

    logout_response = client.post(
        "/logout",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert logout_response.status_code == 303
    assert client.cookies.get("items_admin_session") is None
    assert client.get("/api/admin/items").status_code == 401
    client.cookies.set("items_admin_session", cookie)
    assert client.get("/api/admin/items").status_code == 401


def test_login_rejects_invalid_credentials_and_bad_csrf(client, admin_headers):
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)
    assert csrf
    invalid_password = client.post(
        "/login",
        data={"username": "owner", "password": "wrong password", "csrf_token": csrf.group(1)},
    )
    assert invalid_password.status_code == 401
    assert "Username or password is incorrect" in invalid_password.text

    bad_csrf = client.post(
        "/login",
        data={"username": "owner", "password": "correct horse battery staple 2026", "csrf_token": "bad-token"},
    )
    assert bad_csrf.status_code == 403


def test_intake_stages_private_photos_without_running_a_provider(client, admin_headers):
    intake_response = client.post(
        "/api/admin/intakes",
        json={"barcode": "012345678905"},
        headers=admin_headers,
    )
    assert intake_response.status_code == 201, intake_response.text
    intake = intake_response.json()
    assert intake["barcode"] == "012345678905"
    assert len(intake["photos"]) == 0
    assert client.get(f"/api/admin/intakes/{intake['id']}").status_code == 401

    image_bytes = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(image_bytes, format="PNG")
    photo_response = client.post(
        f"/api/admin/intakes/{intake['id']}/photos",
        headers=admin_headers,
        data={"kind": "barcode"},
        files={"file": ("barcode.png", image_bytes.getvalue(), "image/png")},
    )
    assert photo_response.status_code == 201, photo_response.text
    photo = photo_response.json()
    assert photo["url"].startswith("/api/admin/intakes/")
    assert client.get(photo["url"]).status_code == 401
    assert client.get(photo["url"], headers=admin_headers).content == image_bytes.getvalue()


def test_intake_manual_identifiers_and_sequential_live_provider_runs(client, admin_headers, monkeypatch):
    intake_response = client.post(
        "/api/admin/intakes",
        json={"manufacturer_part_number": "MFR-123"},
        headers=admin_headers,
    )
    assert intake_response.status_code == 201, intake_response.text
    intake = intake_response.json()
    assert intake["manufacturer_part_number"] == "MFR-123"
    assert intake["barcode"] == ""

    corrected = client.patch(
        f"/api/admin/intakes/{intake['id']}",
        json={"barcode": "012345678905", "manufacturer_part_number": "MFR-456"},
        headers=admin_headers,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["barcode"] == "012345678905"
    assert corrected.json()["manufacturer_part_number"] == "MFR-456"

    barcode_only_update = client.patch(
        f"/api/admin/intakes/{intake['id']}",
        json={"barcode": "012345678905"},
        headers=admin_headers,
    )
    assert barcode_only_update.json()["manufacturer_part_number"] == "MFR-456"

    provider_calls = []

    def fake_run_provider(method, *, barcode, manufacturer_part_number, photos):
        provider_calls.append((method, barcode, manufacturer_part_number, photos))
        if method == "upcitemdb":
            return [{
                "source_name": "UPCitemdb",
                "source_type": "upcitemdb",
                "fields": {
                    "barcode": barcode,
                    "manufacturer_part_number": manufacturer_part_number,
                    "name": "Catalog product",
                },
                "price_evidence": [],
            }]
        return [{
            "source_name": "eBay Browse API (active listings)",
            "source_type": "ebay_active",
            "fields": {"name": "Active listing product"},
            "price_evidence": [],
        }]

    monkeypatch.setattr("app.main.run_provider", fake_run_provider)
    catalog = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        json={"method": "upcitemdb"},
        headers=admin_headers,
    )
    assert catalog.status_code == 200, catalog.text
    catalog_candidate = catalog.json()["candidates"][0]
    assert catalog_candidate["fields"]["barcode"] == "012345678905"
    assert catalog_candidate["fields"]["manufacturer_part_number"] == "MFR-456"

    ebay = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        json={"method": "ebay_active"},
        headers=admin_headers,
    )
    assert ebay.status_code == 200, ebay.text
    candidates = ebay.json()["candidates"]
    assert {candidate["source_type"] for candidate in candidates} == {
        "upcitemdb",
        "ebay_active",
    }
    assert all(candidate["is_fixture"] is False for candidate in candidates)
    assert provider_calls == [
        ("upcitemdb", "012345678905", "MFR-456", []),
        ("ebay_active", "012345678905", "MFR-456", []),
    ]

    response = client.post(
        f"/api/admin/intakes/{intake['id']}/create-draft",
        headers=admin_headers,
        json={
            "item": {
                "name": "Manual identifier test",
                "manufacturer_part_number": "MFR-456",
                "barcode": "012345678905",
                "category": "Lighting",
                "condition": "open_box",
                "asking_price": 12.50,
            },
            "selected_field_sources": {"manufacturer_part_number": catalog_candidate["id"]},
            "selected_evidence_ids": [],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["manufacturer_part_number"] == "MFR-456"
    public = client.get("/api/public/items").json()
    assert all("manufacturer_part_number" not in item for item in public)


def test_provider_registry_reports_configuration_without_secrets(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "private-test-key")
    monkeypatch.setenv("UPCDATABASE_API_KEY", "")
    monkeypatch.setenv("EBAY_CLIENT_ID", "")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "")
    providers = provider_statuses()
    provider_map = {provider["id"]: provider for provider in providers}
    assert provider_map["vision_gemini"]["configured"] is True
    assert provider_map["vision_openai"]["configured"] is False
    assert provider_map["vision_groqcloud"]["configured"] is True
    assert provider_map["upcitemdb"]["configured"] is True
    assert provider_map["upcdatabase"]["configured"] is False
    assert provider_map["ebay_active"]["configured"] is False
    assert "private-test-key" not in json.dumps(providers)


def test_groqcloud_intake_route_accepts_photos_and_limits_each_run(client, admin_headers, monkeypatch):
    intake = client.post("/api/admin/intakes", json={}, headers=admin_headers).json()
    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), color="purple").save(image_bytes, format="PNG")
    photo_response = client.post(
        f"/api/admin/intakes/{intake['id']}/photos",
        headers=admin_headers,
        data={"kind": "product"},
        files={"file": ("product.png", image_bytes.getvalue(), "image/png")},
    )
    assert photo_response.status_code == 201
    photo_id = photo_response.json()["id"]
    observed: dict[str, Any] = {}

    def fake_provider(method, *, barcode, manufacturer_part_number, photos):
        observed.update({"method": method, "photos": photos})
        return [{
            "source_name": "GroqCloud vision",
            "source_type": method,
            "fields": {"name": "Unverified GroqCloud suggestion"},
            "price_evidence": [],
        }]

    monkeypatch.setattr("app.main.run_provider", fake_provider)
    response = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        headers=admin_headers,
        json={"method": "vision_groqcloud", "photo_ids": [photo_id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["candidates"][0]["source_type"] == "vision_groqcloud"
    assert observed["method"] == "vision_groqcloud"
    assert observed["photos"][0][1] == image_bytes.getvalue()

    too_many_photos = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        headers=admin_headers,
        json={
            "method": "vision_groqcloud",
            "photo_ids": [photo_id, str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())],
        },
    )
    assert too_many_photos.status_code == 422
    assert "no more than 3 photos" in too_many_photos.json()["detail"]


def test_unconfigured_live_provider_is_attempted_and_reports_setup_error(client, admin_headers, monkeypatch):
    monkeypatch.delenv("UPCDATABASE_API_KEY", raising=False)
    status_response = client.get("/api/admin/intake-providers", headers=admin_headers)
    assert status_response.status_code == 200
    available = {provider["id"]: provider["configured"] for provider in status_response.json()}
    assert available["upcdatabase"] is False

    intake = client.post(
        "/api/admin/intakes",
        json={"barcode": "012345678905"},
        headers=admin_headers,
    ).json()
    response = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        headers=admin_headers,
        json={"method": "upcdatabase"},
    )
    assert response.status_code == 502
    assert "UPCDATABASE_API_KEY" in response.json()["detail"]
    assert client.get(f"/api/admin/intakes/{intake['id']}", headers=admin_headers).status_code == 200


def test_upcitemdb_normalizer_produces_sourced_fields_and_typed_evidence():
    candidates = _normalize_upcitemdb(
        {
            "code": "OK",
            "items": [
                {
                    "upc": "012345678905",
                    "title": "Synthetic product title",
                    "brand": "Sample Brand",
                    "model": "S-100",
                    "mpn": "S-100-B",
                    "category": "Tools",
                    "offers": [
                        {
                            "title": "Retail listing",
                            "domain": "shop.example",
                            "price": "45.50",
                            "currency": "USD",
                            "link": "https://shop.example/item",
                        },
                    ],
                },
            ],
        },
        "012345678905",
    )
    assert candidates[0]["fields"]["manufacturer_part_number"] == "S-100-B"
    assert candidates[0]["price_evidence"][0]["evidence_type"] == "retail_reference"
    assert candidates[0]["price_evidence"][0]["amount"] == "45.50"
    assert candidates[0]["price_evidence"][0]["product_url"] == "https://shop.example/item"
    with pytest.raises(ProviderError, match="no product"):
        _normalize_upcitemdb({"code": "OK", "items": []}, "012345678905")


def test_upcdatabase_adapter_uses_authorization_header_and_maps_mpn(monkeypatch):
    monkeypatch.setenv("UPCDATABASE_API_KEY", "private-test-token")
    captured: dict[str, Any] = {}

    def fake_request(url, *, method="GET", headers=None, body=None):
        captured.update({"url": url, "headers": headers})
        return {
            "success": True,
            "barcode": "012345678905",
            "title": "Sample catalog item",
            "brand": "Example",
            "mpn": "MFR-5",
            "category": "Home",
        }, {}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    results = run_provider(
        "upcdatabase",
        barcode="012345678905",
        manufacturer_part_number="MFR-5",
        photos=[],
    )
    assert captured["url"].endswith("/product/012345678905")
    assert captured["headers"] == {"Authorization": "private-test-token"}
    assert results[0]["fields"]["manufacturer_part_number"] == "MFR-5"
    assert results[0]["fields"]["name"] == "Sample catalog item"


def test_gemini_adapter_sends_only_requested_photos_and_parses_json(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    image_bytes = io.BytesIO()
    Image.new("RGB", (12, 8), color="green").save(image_bytes, format="PNG")
    captured: dict[str, Any] = {}

    def fake_request(url, *, method="GET", headers=None, body=None):
        captured.update({"url": url, "headers": headers, "body": body})
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "name": "Unverified garden tool",
                                        "brand": "Sample Brand",
                                        "model": "",
                                        "manufacturer_part_number": "",
                                        "barcode": "",
                                        "category": "Tools",
                                        "description": "",
                                        "match_notes": "Check label before accepting.",
                                    }
                                )
                            },
                        ],
                    },
                },
            ],
        }, {}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    results = run_provider(
        "vision_gemini",
        barcode="",
        manufacturer_part_number="",
        photos=[("product", image_bytes.getvalue(), "image/png")],
    )
    assert captured["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert captured["headers"] == {"X-Goog-Api-Key": "private-test-key"}
    body = captured["body"]
    parts = body["contents"][0]["parts"]
    assert len(parts) == 2
    assert "inlineData" in parts[0]
    assert results[0]["fields"]["name"] == "Unverified garden tool"
    assert results[0]["match_notes"] == "Check label before accepting."


def test_groqcloud_adapter_sends_selected_photos_and_parses_json(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "private-test-key")
    monkeypatch.setenv("GROQ_VISION_MODEL", "test/vision-model")
    import groq

    image_bytes = io.BytesIO()
    Image.new("RGB", (12, 8), color="blue").save(image_bytes, format="PNG")
    captured: dict[str, Any] = {}

    class FakeGroq:
        def __init__(self, **kwargs):
            captured["client_options"] = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def create(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "name": "Unverified GroqCloud product",
                                    "brand": "",
                                    "model": "",
                                    "manufacturer_part_number": "",
                                    "barcode": "",
                                    "category": "Tools",
                                    "description": "",
                                    "match_notes": "Verify the label.",
                                }
                            )
                        )
                    )
                ]
            )

    monkeypatch.setattr(groq, "Groq", FakeGroq)
    results = run_provider(
        "vision_groqcloud",
        barcode="",
        manufacturer_part_number="",
        photos=[("product", image_bytes.getvalue(), "image/png")],
    )

    assert captured["client_options"] == {
        "api_key": "private-test-key",
        "timeout": providers._API_TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    body = captured["request"]
    assert body["model"] == "test/vision-model"
    assert body["response_format"] == {"type": "json_object"}
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert results[0]["source_name"] == "GroqCloud vision"
    assert results[0]["source_type"] == "vision_groqcloud"
    assert results[0]["fields"]["name"] == "Unverified GroqCloud product"
    assert results[0]["match_notes"] == "Verify the label."


def test_groqcloud_rejects_more_than_three_photos(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "private-test-key")
    photos = [("product", b"invalid-is-not-prepared", "image/png")] * 4

    with pytest.raises(ProviderError, match="no more than 3 photos"):
        run_provider(
            "vision_groqcloud",
            barcode="",
            manufacturer_part_number="",
            photos=photos,
        )


def test_provider_http_error_logs_structured_details_without_secrets(monkeypatch, caplog):
    secret = "gsk_test_sensitive_value"
    monkeypatch.setenv("GROQ_API_KEY", secret)
    error_payload = {
        "error": {
            "message": f"The model qwen/test-model is blocked at the project level. {secret}",
            "type": "permissions_error",
            "code": "model_permission_blocked_project",
        }
    }
    error = providers.urllib.error.HTTPError(
        "https://api.groq.com/openai/v1/chat/completions",
        403,
        "Forbidden",
        {},
        io.BytesIO(json.dumps(error_payload).encode("utf-8")),
    )

    def raise_http_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(providers.urllib.request, "urlopen", raise_http_error)

    with caplog.at_level("WARNING", logger="app.providers"):
        with pytest.raises(ProviderError, match="Provider access is forbidden"):
            providers._request_json("https://api.groq.com/openai/v1/chat/completions")

    assert "model_permission_blocked_project" in caplog.text
    assert "qwen/test-model" in caplog.text
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_groqcloud_sdk_error_logs_provider_details_without_secrets(monkeypatch, caplog):
    import groq
    import httpx

    secret = "gsk_test_sdk_sensitive_value"
    monkeypatch.setenv("GROQ_API_KEY", secret)
    error_payload = {
        "error": {
            "message": f"The model qwen/test-model is blocked at the project level. {secret}",
            "type": "permissions_error",
            "code": "model_permission_blocked_project",
        }
    }
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(403, request=request, headers=httpx.Headers(), json=error_payload)
    sdk_error = groq.APIStatusError("403 Forbidden", response=response, body=error_payload)

    class FailingGroq:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def create(self, **kwargs):
            raise sdk_error

    monkeypatch.setattr(groq, "Groq", FailingGroq)
    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), color="red").save(image_bytes, format="PNG")

    with caplog.at_level("WARNING", logger="app.providers"):
        with pytest.raises(ProviderError, match="Provider access is forbidden"):
            run_provider(
                "vision_groqcloud",
                barcode="",
                manufacturer_part_number="",
                photos=[("product", image_bytes.getvalue(), "image/png")],
            )

    assert "model_permission_blocked_project" in caplog.text
    assert "qwen/test-model" in caplog.text
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_provider_error_log_detail_keeps_non_json_cloudflare_reason():
    detail = providers._provider_error_log_detail(
        "<html><body>Cloudflare Error 1010: Browser signature rejected</body></html>"
    )

    assert "Cloudflare Error 1010" in detail
    assert "Browser signature rejected" in detail


def test_upcitemdb_adapter_uses_trial_and_preserves_provider_attribution(monkeypatch):
    monkeypatch.delenv("UPCITEMDB_USER_KEY", raising=False)
    captured: dict[str, object] = {}

    def fake_request(url, *, method="GET", headers=None, body=None):
        captured.update({"url": url, "headers": headers})
        return {
            "code": "OK",
            "items": [{"title": "Sample product", "upc": "012345678905", "brand": "Example"}],
        }, {}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    result = run_provider(
        "upcitemdb",
        barcode="012345678905",
        manufacturer_part_number="MPN-1",
        photos=[],
    )
    assert "/prod/trial/lookup?upc=012345678905" in captured["url"]
    assert captured["headers"] == {}
    assert result[0]["source_name"] == "UPCitemdb"
    assert result[0]["fields"]["name"] == "Sample product"


def test_ebay_active_adapter_uses_oauth_and_marks_evidence_as_active(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "test-client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(providers, "_ebay_token", None)
    observed: dict[str, object] = {}

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, limit):
            return json.dumps({"access_token": "mock-access-token", "expires_in": 3600}).encode()

    def fake_urlopen(request, timeout):
        observed["token_url"] = request.full_url
        observed["token_headers"] = request.headers
        observed["token_body"] = request.data
        return FakeResponse()

    def fake_request(url, *, method="GET", headers=None, body=None):
        observed.update({"search_url": url, "search_headers": headers})
        return {
            "itemSummaries": [
                {
                    "title": "Active listing sample",
                    "itemWebUrl": "https://www.ebay.com/itm/123",
                    "price": {"value": "25.95", "currency": "USD"},
                },
            ],
        }, {}

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers, "_request_json", fake_request)
    results = run_provider(
        "ebay_active",
        barcode="012345678905",
        manufacturer_part_number="MFR-1",
        photos=[],
    )
    assert observed["token_url"].endswith("/identity/v1/oauth2/token")
    assert "gtin=012345678905" in observed["search_url"]
    assert observed["search_headers"]["Authorization"] == "Bearer mock-access-token"
    assert results[0]["price_evidence"][0]["evidence_type"] == "active_listing"
    assert results[0]["price_evidence"][0]["amount"] == "25.95"


def test_live_vision_run_uses_only_selected_photo_and_retains_intake(
    client, admin_headers, monkeypatch
):
    intake = client.post(
        "/api/admin/intakes",
        json={"manufacturer_part_number": "MFR-456"},
        headers=admin_headers,
    ).json()
    photo_ids = []
    for color in ("red", "blue"):
        image_bytes = io.BytesIO()
        Image.new("RGB", (4, 4), color=color).save(image_bytes, format="PNG")
        response = client.post(
            f"/api/admin/intakes/{intake['id']}/photos",
            headers=admin_headers,
            data={"kind": "product"},
            files={"file": (f"{color}.png", image_bytes.getvalue(), "image/png")},
        )
        assert response.status_code == 201
        photo_ids.append(response.json()["id"])

    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    observed: dict[str, object] = {}

    def fake_provider(method, *, barcode, manufacturer_part_number, photos):
        observed["method"] = method
        observed["mpn"] = manufacturer_part_number
        observed["photos"] = photos
        return [{
            "source_name": "Mocked Gemini",
            "source_type": method,
            "fields": {"name": "Unverified suggestion"},
            "price_evidence": [],
            "match_notes": "Mock response",
        }]

    monkeypatch.setattr("app.main.run_provider", fake_provider)
    response = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        headers=admin_headers,
        json={"method": "vision_gemini", "photo_ids": [photo_ids[1]]},
    )
    assert response.status_code == 200, response.text
    assert observed["method"] == "vision_gemini"
    assert observed["mpn"] == "MFR-456"
    selected_photos = observed["photos"]
    assert len(selected_photos) == 1
    assert selected_photos[0][1] == client.get(
        f"/api/admin/intakes/{intake['id']}/photos/{photo_ids[1]}",
        headers=admin_headers,
    ).content
    assert response.json()["candidates"][0]["is_fixture"] is False
    assert response.json()["candidates"][0]["match_notes"] == "Mock response"
    assert client.get(f"/api/admin/intakes/{intake['id']}", headers=admin_headers).status_code == 200


def test_provider_error_does_not_delete_staged_intake(client, admin_headers, monkeypatch):
    intake = client.post(
        "/api/admin/intakes",
        json={"barcode": "012345678905"},
        headers=admin_headers,
    ).json()
    monkeypatch.setenv("EBAY_CLIENT_ID", "test-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "test-secret")

    def failing_provider(*args, **kwargs):
        raise ProviderError("Provider rate limit reached. Try again later.")

    monkeypatch.setattr("app.main.run_provider", failing_provider)
    response = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        headers=admin_headers,
        json={"method": "ebay_active"},
    )
    assert response.status_code == 502
    assert "rate limit" in response.json()["detail"]
    still_open = client.get(f"/api/admin/intakes/{intake['id']}", headers=admin_headers)
    assert still_open.status_code == 200
    assert still_open.json()["candidates"] == []


def test_intake_creates_only_an_unpublished_draft_with_selected_evidence(client, admin_headers, monkeypatch):
    intake = client.post(
        "/api/admin/intakes",
        json={"barcode": "012345678905"},
        headers=admin_headers,
    ).json()
    def fake_run_provider(method, *, barcode, manufacturer_part_number, photos):
        assert method == "upcitemdb"
        return [
            {
                "source_name": "Google Gemini",
                "source_type": "vision_gemini",
                "fields": {"name": "Candidate product", "brand": "Provider brand"},
                "price_evidence": [],
            },
            {
                "source_name": "UPCitemdb",
                "source_type": "upcitemdb",
                "fields": {
                    "model": "Candidate model",
                    "barcode": barcode,
                    "manufacturer_part_number": manufacturer_part_number,
                },
                "price_evidence": [{
                    "id": str(uuid.uuid4()),
                    "evidence_type": "retail_reference",
                    "title": "Provider catalog reference",
                    "source_name": "UPCitemdb",
                    "product_url": "https://example.com/product",
                    "amount": "89.99",
                    "currency": "USD",
                }],
            },
        ]

    monkeypatch.setattr("app.main.run_provider", fake_run_provider)
    lookup_response = client.post(
        f"/api/admin/intakes/{intake['id']}/lookup",
        headers=admin_headers,
        json={"method": "upcitemdb"},
    )
    assert lookup_response.status_code == 200, lookup_response.text
    lookup = lookup_response.json()
    candidates = {candidate["source_type"]: candidate for candidate in lookup["candidates"]}
    vision = candidates["vision_gemini"]
    catalog = candidates["upcitemdb"]
    retail_evidence = next(
        evidence for evidence in catalog["price_evidence"]
        if evidence["evidence_type"] == "retail_reference"
    )
    image_bytes = io.BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(image_bytes, format="PNG")
    client.post(
        f"/api/admin/intakes/{intake['id']}/photos",
        headers=admin_headers,
        data={"kind": "product"},
        files={"file": ("product.png", image_bytes.getvalue(), "image/png")},
    )

    response = client.post(
        f"/api/admin/intakes/{intake['id']}/create-draft",
        headers=admin_headers,
        json={
            "item": {
                "name": "Owner-confirmed pendant",
                "brand": "My corrected brand",
                "model": "MY-MODEL",
                "barcode": "012345678905",
                "category": "Lighting",
                "description": "Owner-reviewed item description",
                "condition": "open_box",
                "quantity": 1,
                "asking_price": 25.50,
                "private_notes": "Keep package in the garage",
                "storage_location": "Garage / shelf C",
                "is_published": True,
            },
            "selected_field_sources": {"name": vision["id"], "model": catalog["id"]},
            "selected_evidence_ids": [retail_evidence["id"]],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["name"] == "Owner-confirmed pendant"
    assert created["brand"] == "My corrected brand"
    assert created["asking_price"] == 25.5
    assert created["is_published"] is False
    assert len(created["photos"]) == 1
    assert len(created["price_evidence"]) == 1
    assert created["price_evidence"][0]["evidence_type"] == "retail_reference"
    assert created["price_evidence"][0]["amount"] == 89.99
    assert client.get(f"/api/admin/intakes/{intake['id']}", headers=admin_headers).status_code == 404
    assert client.get(f"/api/admin/photos/{created['photos'][0]['id']}", headers=admin_headers).content == image_bytes.getvalue()
    assert created["id"] not in {item["id"] for item in client.get("/api/public/items").json()}

    client.patch(
        f"/api/admin/items/{created['id']}",
        json={"is_published": True},
        headers=admin_headers,
    )
    public_item = client.get("/api/public/items").json()[0]
    assert public_item["id"] == created["id"]
    assert "price_evidence" not in public_item
    assert "private_notes" not in public_item
    assert "storage_location" not in public_item


def test_intake_rejects_results_from_another_intake(client, admin_headers, monkeypatch):
    first = client.post("/api/admin/intakes", json={"barcode": "123"}, headers=admin_headers).json()
    second = client.post("/api/admin/intakes", json={"barcode": "456"}, headers=admin_headers).json()
    monkeypatch.setattr(
        "app.main.run_provider",
        lambda *args, **kwargs: [{
            "source_name": "UPCitemdb",
            "source_type": "upcitemdb",
            "fields": {"name": "Provider suggestion"},
            "price_evidence": [],
        }],
    )
    lookup = client.post(
        f"/api/admin/intakes/{first['id']}/lookup",
        headers=admin_headers,
        json={"method": "upcitemdb"},
    )
    assert lookup.status_code == 200, lookup.text
    candidate = lookup.json()["candidates"][0]
    response = client.post(
        f"/api/admin/intakes/{second['id']}/create-draft",
        headers=admin_headers,
        json={
            "item": {
                "name": "Incorrect candidate",
                "category": "Lighting",
                "condition": "open_box",
                "asking_price": 1,
            },
            "selected_field_sources": {"name": candidate["id"]},
        },
    )
    assert response.status_code == 422


def test_expired_intake_photos_are_removed_at_startup(client, admin_headers):
    intake = client.post(
        "/api/admin/intakes",
        json={"barcode": "123"},
        headers=admin_headers,
    ).json()
    image_bytes = io.BytesIO()
    Image.new("RGB", (4, 4), color="green").save(image_bytes, format="PNG")
    client.post(
        f"/api/admin/intakes/{intake['id']}/photos",
        headers=admin_headers,
        data={"kind": "package"},
        files={"file": ("package.png", image_bytes.getvalue(), "image/png")},
    )
    with database.connect_db() as connection:
        photo = connection.execute(
            "SELECT filename FROM assisted_intake_photos WHERE intake_id = ?",
            (intake["id"],),
        ).fetchone()
        connection.execute(
            "UPDATE assisted_intakes SET expires_at = 0 WHERE id = ?",
            (intake["id"],),
        )
    staged_path = database.INTAKE_UPLOAD_DIR / photo["filename"]
    assert staged_path.exists()

    database.initialize_database()

    assert client.get(f"/api/admin/intakes/{intake['id']}", headers=admin_headers).status_code == 404
    assert not staged_path.exists()


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
    assert 'data-view="intake"' in management_page.text
    assert 'id="intake-review-content"' in management_page.text
    assert 'id="intake-product-files"' in management_page.text
    assert 'id="intake-package-files"' in management_page.text
    assert 'id="intake-barcode-files"' in management_page.text
    assert 'id="intake-manufacturer-part-number"' in management_page.text
    assert 'id="manufacturer_part_number"' in management_page.text
    assert "Choose one method to run" in management_page.text
    assert 'api("/api/admin/intake-providers")' in management_page.text
    assert "Live providers" in management_page.text
    assert "Select for next local or vision run" in management_page.text
    assert "completed/sold-item history" in management_page.text
    assert 'id="photo-form"' in management_page.text
    assert 'id="event-form"' in management_page.text
    assert 'data-view="backups"' in management_page.text
    assert 'href="/api/admin/backups/download"' in management_page.text
    assert 'id="save-server-backup"' in management_page.text
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
