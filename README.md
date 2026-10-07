# Items for Sale

A small, manual-first inventory app with a FastAPI API, SQLite storage, and a Bokeh public catalog. Item photos and owner-managed garage-sale events are supported; product recognition and automated pricing are intentionally left for a later milestone.

## Requirements

- Python 3.11 or newer
- An internet connection for installing the packages

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure one or more owner/helper accounts before opening the private management area:

```powershell
python scripts\create_user.py
$env:INVENTORY_USERS = '{"owner":"PASTE_THE_GENERATED_HASH_HERE"}'
```

For additional helpers, generate a separate username/password hash and include each entry in the JSON object. Keep `INVENTORY_USERS` out of source control; for persistent local development, load it from a protected environment file or secret store. The app disables administration when no accounts are configured. Use unique strong passwords.

Set `PUBLIC_CATALOG_URL` to the public HTTPS catalog URL when deployed; contact messages include it when configured. Locally, the public page remains usable without this setting.

Start the app:

```powershell
python -m uvicorn app.main:app --reload
```

- Public catalog: <http://127.0.0.1:8000/>
- Private management: <http://127.0.0.1:8000/manage>
- API schema: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

The management page uses HTTP Basic authentication. In `APP_ENV=production`, admin routes reject non-HTTPS requests. Put the app behind a correctly configured HTTPS reverse proxy or tunnel before making it internet-accessible; do not expose the SQLite database, uploads directory, or development server directly. Configure trusted proxy handling so the application can reliably determine that requests use HTTPS.

## Owner workspace

The `/manage` workspace provides an inventory dashboard with listing search and category/status filters, item create/edit and publication controls, private storage notes, photo upload and visibility management, owner-managed categories, and garage-sale event scheduling. Product images can be explicitly published; package/barcode photos remain private. The dashboard and its API are protected by the configured owner/helper accounts.

## Data and backups

By default, SQLite data and uploads are written to `data/`. `INVENTORY_DATA_DIR`, `INVENTORY_DB_PATH`, and `INVENTORY_UPLOAD_DIR` can override the storage locations. Back up both the database and uploaded photos together, and periodically verify restoration. A second local drive does not provide off-site disaster recovery.

## Inventory behavior

- Categories are owner-managed and start with Lighting, Tools, and Home.
- Condition values are New/sealed, Open box, Used/good, Used/fair, and For parts/untested.
- Availability values are Available, Reserved, Sold, and Hidden.
- Reserved and sold quantities cannot exceed total quantity. Available quantity is derived.
- Public catalog responses are an explicit allowlist. Draft, sold, hidden, and depleted listings are not public.
- Product photos are private unless explicitly marked public. Package and barcode photos remain private.
- The exact address of a garage-sale event is public only while the event is active. Expired events are omitted.
- Text and email actions open the buyer's messaging app; the app does not send or store messages.

## Tests

```powershell
python -m pytest -q
```
