# Raspberry Pi public deployment guide

This guide describes one small, single-server production setup:

- A Raspberry Pi running Raspberry Pi OS 64-bit.
- This application served by Uvicorn on loopback only.
- Caddy as the public HTTPS reverse proxy.
- A domain name pointed at your home network. Several domains or subdomains can share the one public IP address; Caddy routes each request to the right site by hostname (see "Host multiple sites behind one public IP").
- Optional USB storage for server-side backups.

Commands assume Raspberry Pi OS/Debian and a local console or trusted LAN SSH session. Replace example names, paths, addresses, and UUIDs with your own. Do not paste passwords, password hashes, or API keys into shared messages or source control.

## 1. Confirm the prerequisites

Before installing anything, collect:

- The Pi model, power supply, and a reliable storage device. A Pi 3 can serve a small, low-traffic catalog, but has limited memory and CPU; OCR, image resizing, and package installation may be slow.
- A wired Ethernet connection if possible.
- A domain name you control, such as `example.com`, and the subdomain you want, such as `items.example.com`.
- Access to the router's DHCP reservation, firewall, and port-forwarding settings.
- Whether your ISP connection has a reachable public IP address.

### Check for carrier-grade NAT

1. Find the router's Internet/WAN IPv4 address in its status page.
2. From a device on the home network, find the public IPv4 address seen by the internet using a reputable IP-check service.
3. Compare the two addresses. If they differ, or the router's WAN address is in a private range (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) or the shared carrier range (`100.64.0.0/10`), inbound port forwarding may not work. Ask the ISP whether a public IPv4 address is available.
4. If a reachable address is unavailable, stop the port-forwarding plan here. Use a secure tunnel or a host with a public address instead. Review the tunnel provider's privacy, access controls, logging, and terms.

Do not expose SSH or Uvicorn to the public internet as a workaround.

## 2. Install and update Raspberry Pi OS

1. Use Raspberry Pi Imager to install a current **64-bit Raspberry Pi OS Lite** release supported by the Pi. Enable SSH in Imager only if you need it, and use a strong password or SSH key.
2. Boot the Pi, connect it to the home network, and apply updates:

   ```bash
   sudo apt update
   sudo apt full-upgrade
   sudo reboot
   ```

3. After reboot, check the architecture and Python version:

   ```bash
   uname -m
   python3 --version
   ```

   Prefer `aarch64` and Python 3.11 or newer. The app requires Python 3.11+. If the OS provides an older Python, use a supported OS/Python installation method rather than changing the system Python in place.

4. In the router, create a DHCP reservation for the Pi's Ethernet MAC address so its **private LAN address** remains stable. This is not the same as a public IP address.
5. Prefer a reliable power supply and a USB SSD or high-endurance storage for ongoing use. Keep the SD card or another separate device available for recovery.

## 3. Install the application and create its service account

Install system packages used by Python, Git, and the deployment script:

```bash
sudo apt install --yes git python3-venv python3-pip python3-dev build-essential curl
sudo apt install --yes tesseract-ocr
```

Tesseract is optional. It enables local OCR; it does not enable the vision providers. Python package installation may take time on a Pi 3. If a dependency cannot install, record the exact package/build error and resolve compatibility before making the site public.

Clone the repository into the deployment directory and run its installer:

```bash
sudo git clone <YOUR-REPOSITORY-URL> /opt/items-for-sale
cd /opt/items-for-sale
sudo bash scripts/deploy.sh
```

The script creates the `items-for-sale` service account and group, prepares `/var/lib/items-for-sale`, creates `.venv`, installs `requirements.txt`, and installs/enables the systemd service. On first run, it copies `.env.example` to `.env`, sets the persistent data directory, and leaves the service stopped until you configure an owner account. It never replaces an existing `.env` or existing systemd unit.

The source and virtual environment are root-owned and readable by the service group; the app process cannot modify its code or dependencies. Do not store runtime data or backups inside the source tree. The script does not install or configure Caddy, alter router settings, or set `APP_ENV=production`.

## 4. Configure persistent application data and credentials

The installer creates `/var/lib/items-for-sale` for persistent data and creates the private environment file on first run. Edit it:

```bash
sudo nano /opt/items-for-sale/.env
```

Set at least:

```dotenv
APP_ENV=development
INVENTORY_DATA_DIR=/var/lib/items-for-sale
INVENTORY_BACKUP_DIR=
INVENTORY_DEFAULT_OWNER=owner
INVENTORY_USERS={}
INVENTORY_AGENT_OWNERS={}
PUBLIC_CATALOG_URL=https://items.example.com
```

Replace `items.example.com` with your actual hostname. Keep `APP_ENV=development` until the HTTPS proxy is working and the admin checks in step 9 are complete.

Generate the owner's password hash without putting the password in shell history:

```bash
cd /opt/items-for-sale
sudo -u items-for-sale .venv/bin/python -m scripts.create_user
```

The command prompts for the username and password and prints a JSON object. Copy that object into the `INVENTORY_USERS` line in `.env` (replacing `{}`). The password must be at least 14 characters. Use a strong, unique password.

Each configured username has a separate inventory workspace. Before the updated app's first startup, set `INVENTORY_DEFAULT_OWNER` to the account that should receive any inventory already in the database. To add a separate seller, generate another account hash and add it to `INVENTORY_USERS`. To let an agent work in a seller's workspace, add a mapping such as `INVENTORY_AGENT_OWNERS={"helper":["owner"]}`; this grants that agent view and edit access. Configure the grant on the server and restart the service. The app's root catalog shows the default owner's published items; other users' public catalogs use `/catalog/<username>`.

For an existing installation, stop the service and make a full backup of the data directory before installing/starting the updated version. The ownership migration assigns existing records to `INVENTORY_DEFAULT_OWNER`; set it to an account that exists in `INVENTORY_USERS` before startup. Keep usernames stable because they identify workspace ownership.

Workspace isolation is enforced in the database/API: item and event records, categories, intake records, and their photo links are scoped to the owner. The app currently keeps these records in one SQLite database and UUID-named photo directories rather than creating separate OS accounts or physical directories per seller. Public catalogs show only each owner's approved content, and each workspace's backup contains only that workspace's records and associated photos.

Optionally add provider credentials such as `GROQ_API_KEY` to `.env`. Do not put them in the Caddyfile, browser code, Git, or a public message. Restart the app service after editing `.env`.

Confirm the service account can read the settings and write application data, then start the service:

```bash
sudo -u items-for-sale test -r /opt/items-for-sale/.env && echo ".env readable"
sudo -u items-for-sale test -w /var/lib/items-for-sale && echo "data directory writable"
sudo systemctl start items-for-sale
sudo systemctl status items-for-sale --no-pager
curl --fail http://127.0.0.1:8000/health
```

## 5. Run Uvicorn as a local system service

The installer writes `/etc/systemd/system/items-for-sale.service` with the settings below. `--host 127.0.0.1` ensures Uvicorn does not listen on the LAN or public interfaces. The proxy-header trust list is limited to loopback because Caddy will connect from this Pi. Do not change it to `*`. If you need to customize the unit, edit it directly; subsequent installer runs preserve an existing unit.

```ini
[Unit]
Description=Items for Sale application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=items-for-sale
Group=items-for-sale
WorkingDirectory=/opt/items-for-sale
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/items-for-sale/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

Expected health response:

```json
{"status":"ok"}
```

If the service fails, inspect its logs before proceeding:

```bash
sudo journalctl -u items-for-sale -n 100 --no-pager
```

Test the local site from a device on the Pi's LAN if you choose, but Uvicorn should remain loopback-only. You can verify its listener with:

```bash
sudo ss -ltnp | grep ':8000'
```

It should show `127.0.0.1:8000`, not `0.0.0.0:8000` or the Pi's LAN address.

## 6. Prepare DNS and the router

Do this only after the local app works.

1. In your DNS provider, create an **A record** for `items.example.com` pointing to your public IPv4 address. For each additional site, create another A record (for any of your domains or subdomains) pointing to the **same** public IP. Ports 80 and 443 are forwarded once, to the machine running Caddy.
2. If your public IP changes, use a dynamic DNS updater that updates this record. Caddy obtains certificates but does not update your DNS records by itself.
3. Add an **AAAA record** only if you have working public IPv6 connectivity, the Pi and Caddy are reachable over IPv6, and your router/firewall permits inbound IPv6 web traffic. A stale or unreachable AAAA record can make some visitors fail to connect; remove it if you cannot support IPv6.
4. Wait for DNS to resolve and verify the A/AAAA records using your DNS provider or a DNS lookup tool.
5. In the router, forward TCP ports **80 and 443** to the Pi's stable private LAN address. Do not forward port 8000 or SSH (port 22) to the internet.
6. If using a host firewall, permit inbound TCP 80 and 443. Permit SSH only from your trusted LAN if needed. Configure and test the SSH allow rule before enabling a firewall remotely, so you do not lock yourself out.

If ISP/router networking prevents inbound traffic, stop here and use a tunnel instead. Do not expose Uvicorn directly.

## 7. Install and configure Caddy

Use Caddy's official Debian/Raspbian package repository so the packaged systemd service and updates are available. Confirm the current instructions at [Caddy's install page](https://caddyserver.com/docs/install) for your OS release. The stable Debian/Raspbian commands are:

```bash
sudo apt install --yes debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

Edit `/etc/caddy/Caddyfile`:

```caddyfile
items.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

Replace the hostname (and see the next section if you will host more than one site). Caddy's `reverse_proxy` sends the app the original request scheme and client information. Do not add broad custom `X-Forwarded-*` headers or configure Uvicorn to trust arbitrary sources.

Validate and reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
sudo journalctl -u caddy -n 100 --no-pager
```

Caddy automatically obtains and renews publicly trusted TLS certificates and redirects HTTP to HTTPS when its hostname is public, DNS points to the server, ports 80/443 reach Caddy, and its data directory is writable and persistent. See [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https). Certificate issuance can fail until DNS and router forwarding are correct.

From a device outside your home network (such as a phone with Wi-Fi disabled), open:

- `https://items.example.com/`
- `https://items.example.com/health`
- `https://items.example.com/manage`

The catalog and health check should load with a valid certificate; `/manage` should take an unauthenticated visitor to sign-in. Check Caddy logs if certificate issuance or proxying fails.

### Host multiple sites behind one public IP

Only one process can listen on public ports 80 and 443, so Caddy is the single front door for every site. It reads the hostname the browser asked for and sends the request to the matching backend. Each hostname gets its own automatically managed certificate. Add one site block per hostname to `/etc/caddy/Caddyfile`:

```caddyfile
items.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}

blog.example.com {
    encode zstd gzip
    root * /var/www/blog
    file_server
}

shop.other-domain.org {
    reverse_proxy 192.168.1.50:3000
}
```

- **Different app on this Pi:** run it on its own loopback port (for example `127.0.0.1:8001`) and use `reverse_proxy`. Never reuse port 8000.
- **Static files:** use `root` and `file_server`, as in the `blog` example.
- **Another machine on your LAN:** `reverse_proxy` to its private address and port. Give that machine a DHCP reservation and do not forward its port in the router; only Caddy is exposed. Traffic between Caddy and that machine is plain HTTP on your LAN unless you configure otherwise.
- **Another domain:** add its A record to the same public IP; no router changes are needed.
- **Isolation:** each site is a separate backend. Keep the `--forwarded-allow-ips=127.0.0.1` setting for this app, and keep each site's secrets and data separate. Sites sharing one Pi also share its CPU, memory, and failures, so a Pi 3 suits only light traffic.
- **Only this app needs the camera/HTTPS settings:** `APP_ENV` and `PUBLIC_CATALOG_URL` apply per application, not to Caddy.

Requests for a hostname without a site block receive no certificate or content, which is the desired behavior. After each edit, run `sudo caddy validate --config /etc/caddy/Caddyfile` and `sudo systemctl reload caddy`, then test every hostname from outside your network. A mistake in one block can prevent reload for all sites, so validate before reloading.

## 8. Optional: mount a USB drive on the server

The app's server backup feature expects `INVENTORY_BACKUP_DIR` to be a directory that is **already mounted and writable**. It intentionally does not create a missing mount point, to avoid saving backups to the Pi's system disk when a USB drive is disconnected.

1. Attach the drive and identify its partition and filesystem:

   ```bash
   lsblk -f
   ```

   Do not format a drive containing data. If formatting is required, first make sure you have chosen the correct device and have another copy of anything important; formatting destroys data.

2. Create a mount point and copy the UUID from `lsblk -f`:

   ```bash
   sudo mkdir -p /mnt/items-backups
   sudo nano /etc/fstab
   ```

3. For an **ext4** partition, add an entry using its actual UUID:

   ```fstab
   UUID=REPLACE_WITH_ACTUAL_UUID /mnt/items-backups ext4 defaults,nofail 0 2
   ```

   For exFAT or NTFS, use the filesystem type and mount options appropriate to the installed driver. Do not paste the ext4 example for a different filesystem.

4. Verify and mount:

   ```bash
   sudo findmnt --verify
   sudo mount /mnt/items-backups
   mountpoint /mnt/items-backups
   df -h /mnt/items-backups
   ```

   Confirm the mount point reports as mounted and the expected USB filesystem is shown. `nofail` allows the Pi to boot if the drive is absent; the app UI will disable server backup while the mount is unavailable.

5. For an ext4 drive, make the mounted directory writable by the app account:

   ```bash
   sudo chown items-for-sale:items-for-sale /mnt/items-backups
   sudo chmod 750 /mnt/items-backups
   sudo -u items-for-sale test -w /mnt/items-backups && echo "backup directory writable"
   ```

6. Set this in `/opt/items-for-sale/.env`:

   ```dotenv
   INVENTORY_BACKUP_DIR=/mnt/items-backups
   ```

7. Restart Uvicorn after editing `.env`:

   ```bash
   sudo systemctl restart items-for-sale
   ```

## 9. Turn on production HTTPS checks and verify

Do this only after the public HTTPS site has a valid certificate and the proxy is working.

1. Confirm Uvicorn is bound to `127.0.0.1:8000` and port 8000 is not forwarded in the router.
2. From the public HTTPS address, sign in, view the catalog, visit `/manage`, upload a test photo, and perform an admin action.
3. Edit `.env` and change:

   ```dotenv
   APP_ENV=production
   PUBLIC_CATALOG_URL=https://items.example.com
   ```

4. Restart the app:

   ```bash
   sudo systemctl restart items-for-sale
   sudo systemctl status items-for-sale --no-pager
   ```

5. Sign out and sign in again over HTTPS. Confirm the browser remains authenticated and admin actions work. The app rejects admin requests it sees as HTTP and marks authentication cookies `Secure`; a proxy/scheme-forwarding error will make administration fail. Check Uvicorn and Caddy logs rather than disabling HTTPS enforcement.
6. Confirm plain HTTP redirects to HTTPS and never exposes admin cookies over HTTP.

`APP_ENV=production` does not enable TLS, configure Caddy, create accounts, or install the application as a service. Those are separate steps above.

## 10. Create and test backups

1. Sign in at `https://items.example.com/manage` and open **Backups**.
2. Choose **Download backup ZIP** to save the selected workspace's archive to the computer/browser you are using. Select a client-attached USB drive in the browser's download location if desired.
3. If the server USB is mounted and configured, confirm the page reports the server backup directory is available and choose **Save / replace server backup**. This creates/replaces a selected-workspace archive such as `/mnt/items-backups/items-for-sale-owner-backup.zip`.
4. Keep at least one separate copy away from the Pi. A USB drive attached to the same Pi is not off-site disaster recovery.
5. The ZIP contains only the selected workspace's `inventory.sqlite3` records and associated `uploads/` and `intake/` photos; it contains private notes and unpublished inventory, so protect it. It is not a full multi-user database backup.

For disaster recovery of a multi-user installation, use a trusted server-operator backup of the entire data directory while the service is stopped. This captures every workspace and its photos without exposing the other users' records through the application UI:

```bash
sudo systemctl stop items-for-sale
sudo tar -C /var/lib -czf "/mnt/items-backups/items-for-sale-data-$(date +%Y%m%d-%H%M%S).tar.gz" items-for-sale
sudo systemctl start items-for-sale
```

Keep that archive restricted because it contains every user's private data. Back up the deployment configuration and source separately; never put `.env` secrets in an unencrypted archive.

For multi-user disaster recovery, restore the full data-directory archive as follows:

1. Inspect the archive and confirm it contains `items-for-sale/`:

   ```bash
   DATA_BACKUP=/path/to/items-for-sale-data-backup.tar.gz
   sudo tar -tzf "$DATA_BACKUP" | sed -n '1,20p'
   ```

2. Stop the service and rename the current data directory for rollback:

   ```bash
   sudo systemctl stop items-for-sale
   sudo mv /var/lib/items-for-sale "/var/lib/items-for-sale-before-restore-$(date +%Y%m%d-%H%M%S)"
   ```

3. Restore the full directory, correct ownership, and verify:

   ```bash
   sudo tar -C /var/lib -xzf "$DATA_BACKUP"
   sudo chown -R items-for-sale:items-for-sale /var/lib/items-for-sale
   sudo systemctl start items-for-sale
   sudo systemctl status items-for-sale --no-pager
   curl --fail http://127.0.0.1:8000/health
   ```

4. Verify inventory and photos from each workspace before removing the preserved directory.

For a restore from the workspace ZIP below, use the following procedure only on a single-workspace deployment. Replacing the database with a one-workspace ZIP on a multi-user installation removes the other workspaces. Before restoring, check `backup-info.json` and ensure `.env` still configures an account with the same username as the archived workspace; changing `INVENTORY_DEFAULT_OWNER` does not reassign already-owned rows.

1. Extract and inspect the archive in a temporary directory; do not extract directly over live data:

   ```bash
   sudo mkdir -m 700 -p /tmp/items-for-sale-restore
   unzip -l /path/to/items-for-sale-owner-backup.zip
   sudo unzip /path/to/items-for-sale-owner-backup.zip -d /tmp/items-for-sale-restore
   ```

2. Confirm the extracted directory contains `inventory.sqlite3`, `uploads/`, `intake/`, and `backup-info.json`. Stop the app and rename the current data folder so it remains available as a rollback:

   ```bash
   sudo systemctl stop items-for-sale
   sudo mv /var/lib/items-for-sale "/var/lib/items-for-sale-before-restore-$(date +%Y%m%d-%H%M%S)"
   sudo install -d -o items-for-sale -g items-for-sale -m 750 /var/lib/items-for-sale
   ```

3. Copy the restored database and photos, then set ownership:

   ```bash
   sudo cp -a /tmp/items-for-sale-restore/inventory.sqlite3 /var/lib/items-for-sale/
   sudo cp -a /tmp/items-for-sale-restore/uploads /tmp/items-for-sale-restore/intake /var/lib/items-for-sale/
   sudo chown -R items-for-sale:items-for-sale /var/lib/items-for-sale
   ```

4. Restart and verify the app:

   ```bash
   sudo systemctl start items-for-sale
   sudo systemctl status items-for-sale --no-pager
   curl --fail http://127.0.0.1:8000/health
   ```

5. Sign in over HTTPS and inspect some inventory items and photos before removing the old data directory or temporary extracted archive. Keep the old directory until you have confirmed the restore.

The ZIP's SQLite snapshot is workspace-scoped. Restoring it replaces the live database with that selected workspace's records, so do not use a single-workspace archive to restore a multi-user installation while expecting other users' records to remain. The restore paths above assume the `INVENTORY_DATA_DIR=/var/lib/items-for-sale` configuration and default upload subdirectories. If you changed `INVENTORY_DB_PATH`, `INVENTORY_UPLOAD_DIR`, or `INVENTORY_INTAKE_DIR`, restore to those configured paths instead.

Schedule backups for a low-activity time. SQLite is copied with its online backup API, but photo files can still be added or removed while an archive is being built; avoid uploads, photo edits, and intake cleanup during the backup when you need the closest matching database/photo set.

## 11. Routine maintenance and troubleshooting

- Check services: `sudo systemctl status items-for-sale caddy --no-pager`.
- Check app logs: `sudo journalctl -u items-for-sale -n 100 --no-pager`.
- Check proxy/certificate logs: `sudo journalctl -u caddy -n 100 --no-pager`.
- Check data and USB free space: `df -h /var/lib/items-for-sale /mnt/items-backups`.
- Update app code and dependencies deliberately. Back up the data directory first, then pull the fast-forward update and run the deployment script, which refreshes the virtual environment and restarts the configured service:

  ```bash
  sudo systemctl stop items-for-sale
  sudo tar -C /var/lib -czf "/mnt/items-backups/items-for-sale-data-before-update-$(date +%Y%m%d-%H%M%S).tar.gz" items-for-sale
  sudo git -C /opt/items-for-sale pull --ff-only
  sudo bash /opt/items-for-sale/scripts/deploy.sh
  ```

  The script does not run `git pull` or replace `.env` or an existing systemd unit. If deployment fails, inspect the app and Caddy logs before making the site public again.
- Apply OS and Caddy security updates regularly, and periodically test reboot, USB mount, and restore behavior.
- If DNS resolves but certificate issuance fails, check DNS A/AAAA records, router forwarding, firewall rules, Caddy logs, correct system time, and whether the ISP blocks inbound traffic.
- If one hostname works and another fails, check that its A record points to the same public IP, its site block exists, and its backend is running. Use `sudo journalctl -u caddy -n 100 --no-pager` for per-site errors.
- If the app works locally but returns 502 through Caddy, verify the app service is healthy and Caddy proxies to `127.0.0.1:8000`.
- If the public site works but admin sign-in fails only after setting production mode, verify Uvicorn's proxy-header settings and that only the local Caddy proxy is trusted.

## References

- [Caddy installation](https://caddyserver.com/docs/install)
- [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [Caddy reverse proxy directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- [Uvicorn settings](https://www.uvicorn.org/settings/)
- [Project README](./README.md)
