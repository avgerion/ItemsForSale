# Items for Sale

A small, manual-first inventory app with a FastAPI API, SQLite storage, and a Bokeh public catalog. Item photos and owner-managed garage-sale events are supported; product recognition and automated pricing are intentionally left for a later milestone.

## Requirements

- Python 3.11 or newer
- An internet connection for installing the packages

## Run the server locally (Windows)

Open PowerShell in the project folder:

```powershell
Set-Location "C:\Users\avger\Offline\Documents\ItemsForSale"
if (-not (Test-Path .venv)) { py -m venv .venv }
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Generate an owner password hash:

```powershell
python -m scripts.create_user
```

Run this module command from the project folder, not as `python scripts\create_user.py`; module execution keeps the project root available so it can import the `app` package.

Copy the printed JSON object into the `INVENTORY_USERS` line in `.env`, for example:

```dotenv
INVENTORY_USERS={"owner":"PASTE_THE_GENERATED_HASH_HERE"}
```

Do not commit `.env`. To add helpers, generate a hash for each account and include every username/hash pair in the same JSON object. The server must be restarted after changing `.env`.

The owner workspace uses a sign-in page and an HTTP-only, server-side session cookie, so it works in browsers that do not show HTTP Basic Auth prompts. Sessions expire after 12 hours and are revoked when you sign out. Only a hash of each random session token is stored in the SQLite database.

Set `PUBLIC_CATALOG_URL` to the public HTTPS catalog URL when deployed; contact messages include it when configured. Locally, the public page remains usable without this setting.

Start the development server in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Keep that PowerShell window open while using the app. The server runs in the foreground; closing the window or pressing **Ctrl+C** stops it. `127.0.0.1` is loopback, so these URLs work only on the same computer and only while the server is running.

- Public catalog: <http://127.0.0.1:8000/>
- Private management: <http://127.0.0.1:8000/manage>
- API schema: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

Check that the server is responding from a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

It should return `status: ok`. If the connection is refused, confirm the first PowerShell window is still running and check its startup error output. If port 8000 is already in use, stop the other server or choose another port, then use that same port in the URLs.

To check that the `.env` account list is being read, use this PowerShell here-string (it prints usernames only, never password hashes):

```powershell
@'
import json
from dotenv import dotenv_values

users = json.loads(dotenv_values(".env").get("INVENTORY_USERS") or "{}")
print("Configured users:", ", ".join(users) if users else "NONE")
'@ | python -
```

The `--reload` option is for local development. Do not use the development server as an internet-facing production host.

The sign-in form protects the management page and admin API; HTTP Basic credentials remain supported for API clients. In `APP_ENV=production`, admin routes reject non-HTTPS requests. Put the app behind a correctly configured HTTPS reverse proxy or tunnel before making it internet-accessible; do not expose the SQLite database, uploads directory, or development server directly. Configure trusted proxy handling so the application can reliably determine that requests use HTTPS.

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
