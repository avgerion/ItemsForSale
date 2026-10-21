# Items for Sale

A small inventory app with a FastAPI API, SQLite storage, and a Bokeh public catalog. Item photos, owner-managed garage-sale events, and a private assisted-intake review flow are supported.

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

In VS Code, use **Terminal → Run Task → Start Inventory Server** to start the same app. The app serves the FastAPI routes and Bokeh pages from one server; no separate frontend server is needed. **Check Inventory Server Health**, **Run Inventory Tests**, and **Project Status and Next Steps** are available from the same task menu.

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

### Hosting publicly and HTTPS

An HTTPS **reverse proxy** (for example, Caddy or Nginx) is a web server placed in front of this app. The public DNS name points to your network's public IP; the router forwards HTTPS traffic to the proxy, which obtains/uses a TLS certificate, encrypts the browser-to-server connection, and forwards requests to Uvicorn over a private local connection. Only the proxy should accept public web traffic; keep Uvicorn, the SQLite database, and photo directories inaccessible from the internet. A changing home IP may require dynamic DNS, and carrier-grade NAT may prevent router port-forwarding.

A **secure tunnel** uses a client program on your server to make an outbound connection to a tunnel provider. The provider gives you a public HTTPS address and forwards requests through that connection to your app, so you generally do not open inbound router ports. This can work around carrier-grade NAT, but traffic passes through the tunnel provider, so review its account access controls, logging, privacy, reliability, and terms. Configure the tunnel to reach only the local app and enable any available access restrictions.

Both approaches provide public HTTPS; neither automatically secures the app, installs it as a reliable service, or backs up your data. In `APP_ENV=production`, admin routes reject requests the app sees as non-HTTPS, and auth cookies are marked `Secure`. Configure the proxy/tunnel and Uvicorn's trusted forwarded-protocol handling so the app sees the original HTTPS scheme. Do not trust forwarded headers from arbitrary clients. Verify the public catalog and admin sign-in over HTTPS before setting `APP_ENV=production`, then verify administration still works; never expose the SQLite database, uploads directory, or Uvicorn port directly.

The sign-in form protects the management page and admin API; HTTP Basic credentials remain supported for API clients. Use a strong, unique owner password, keep `.env` private, and restrict administration with the proxy/tunnel access controls where practical. A Raspberry Pi 3 can be a small, low-traffic host, but its limited memory and older CPU make dependency installation, local OCR, image processing, and concurrent traffic slower. Use reliable power and storage, preferably a USB SSD or high-endurance card, and test unattended restarts and backups before relying on it.

## Owner workspace

The `/manage` workspace provides an inventory dashboard with listing search and category/status filters, item create/edit and publication controls, private storage notes, photo upload and visibility management, owner-managed categories, garage-sale event scheduling, and assisted intake. Product images can be explicitly published; package/barcode photos remain private. The dashboard and its API are protected by the configured owner/helper accounts.

### Assisted intake and provider integrations

Use **Assisted intake** to stage product, package, or barcode photos and manually enter/correct a UPC or manufacturer part number (MPN) before creating an inventory item. MPN is separate from Model. Staged photos stay on the server, are only available to signed-in admins, and expire after 48 hours. Discarding an intake deletes its staged photos.

Choose exactly one live method per run; results are appended so you can compare sources in sequence. Methods are selectable when their required input is present. Configure any required credentials or local software in `.env`; unconfigured methods report what setup is needed when run. For a live vision request, explicitly check the photos to send; unchecked photos are not included in the request. Local barcode/OCR processing runs on the server. Catalog and eBay requests use only entered identifiers and do not transmit photos.

Configure credentials in the local `.env` file using the names in `.env.example`; never put keys in browser code, source control, or a public page. Restart the server after changing provider settings.

- **Gemini vision:** `GEMINI_API_KEY`; optionally set `GEMINI_MODEL`.
- **OpenAI vision:** `OPENAI_API_KEY`; optionally set `OPENAI_VISION_MODEL`. This is a separately selected comparison, not an automatic second call.
- **GroqCloud vision:** `GROQ_API_KEY`; optionally set `GROQ_VISION_MODEL` (defaults to `qwen/qwen3.8-27b`). Requests use the official Groq Python SDK installed through `requirements.txt`. This is a separately selected comparison and accepts up to three photos per run.
- **UPCitemdb:** Uses its limited public trial endpoint if `UPCITEMDB_USER_KEY` is blank; paid keys can be configured with `UPCITEMDB_KEY_TYPE`. UPC/EAN is required; MPN-only lookup is not supported.
- **UPCDatabase.org:** Requires `UPCDATABASE_API_KEY`; UPC/EAN is required.
- **eBay active listings:** Requires eBay Buy/Browse API production credentials `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`; optionally set `EBAY_MARKETPLACE_ID`. Results are current listing asking prices, not completed sales.
- **Local barcode/OCR:** Installs the Python bindings through `requirements.txt`; also install the Tesseract OCR engine and set `TESSERACT_CMD` to its executable if it is not on `PATH`. No image is sent outside the server by this method.

eBay's official Browse API search returns active listings. Completed/sold-item search is not implemented because official Marketplace Insights access is restricted; the app does not scrape eBay pages or substitute active asking prices for sale prices. Provider failures, no matches, rate limits, and missing credentials are reported without deleting the intake or earlier results.

GroqCloud API error details are written to the server log in a bounded, redacted form to help diagnose provider-side failures.

Vision images are resized before transmission. A run accepts at most four selected photos and a total original upload size of 15 MB. Product-identification and OCR values remain suggestions; verify them manually. Select suggested fields and optional price evidence to carry them into the item form. **Create private draft** transfers staged photos as private photos, stores selected price evidence separately from the owner's asking price, and always creates an unpublished item.

## Data and backups

By default, SQLite data and uploads are written to `data/`. `INVENTORY_DATA_DIR`, `INVENTORY_DB_PATH`, and `INVENTORY_UPLOAD_DIR` can override the storage locations. Back up both the database and uploaded photos together, and periodically verify restoration. A second local drive does not provide off-site disaster recovery.

The signed-in admin workspace has a **Backups** page:

- **Download backup ZIP** creates a consistent SQLite snapshot plus the permanent item photos and private staged intake photos. Your browser downloads the archive to the computer you are using; use the browser's download settings to save it to a USB drive attached to that computer. No `.env` file, credentials, or API keys are included. The archive does contain private notes and unpublished inventory, so protect it like the live database.
- **Save / replace server backup** writes `items-for-sale-backup.zip` to the directory configured by `INVENTORY_BACKUP_DIR`. Set this to the mounted USB directory on the server (for example, `/mnt/inventory-backups` on Raspberry Pi OS). Create and mount that directory before starting the app; the app intentionally does not create a missing mount point, preventing an absent USB drive from silently filling the Pi's SD card. The app user needs write access. Each save replaces the previous archive atomically; copy older versions elsewhere if you want history.

The archive contains `inventory.sqlite3`, `uploads/`, `intake/`, and `backup-info.json`. To restore, stop the app, extract those entries, and copy the database and photo directories back to the configured data locations while preserving their relative filenames. Back up regularly and verify restoration periodically. A USB drive attached to the same server is convenient but is not off-site disaster recovery; keep another copy somewhere separate.

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
