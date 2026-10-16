# AMP Generic Instance and Cloudflare Tunnel Deployment

QuietLeaf can run directly inside a CubeCoders AMP Generic instance. Docker is not required.

```text
Android or browser -> https://books.example.com -> Cloudflare Tunnel
                   -> 127.0.0.1:8765 -> AMP Generic instance -> QuietLeaf/Uvicorn
```

The process stays in the foreground so AMP can monitor, stop, and restart it. Run exactly one Uvicorn worker because the archive scheduler and SQLite store are designed for one service process.

## Prerequisites

- A Linux AMP host with a Generic instance.
- Python 3.11 or newer with the `venv` module.
- Outbound HTTPS and DNS access.
- A Cloudflare Tunnel connector on the same host, if the service must be reachable remotely.
- Explicit permission before enabling authorized NovelPing access or archiving content.

The service binds to loopback by default. Do not open its port in the public firewall.

## 1. Put the release in the AMP instance

Upload or clone this repository into a persistent directory owned by the AMP instance user. The directory must contain `pyproject.toml`, `amp-start.sh`, `catalog/`, and `quietleaf_service/`.

Do not place the release in a temporary update directory. The default SQLite archive is stored under `archive/` inside this directory and must survive restarts and updates.

## 2. Install the Python environment

From the service directory, run:

```bash
sh install-amp.sh
```

Re-run the same install command after pulling a release that changes Python dependencies. Do not run it on every service start.

## 3. Configure the instance

Copy `amp.env.example` to `.env` and generate a long random token:

```bash
umask 077
cp amp.env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value after `QUIETLEAF_API_TOKEN=` in `.env`. Protect that file so only the AMP instance user can read it:

```bash
chmod 600 .env
```

Important settings:

```dotenv
QUIETLEAF_API_TOKEN=<long-random-token>
PORT=8765
QUIETLEAF_ARCHIVE_MAX_BYTES=2147483648
QUIETLEAF_ENABLE_NOVELPING=0
QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=0
```

Change `QUIETLEAF_ENABLE_NOVELPING` to `1` only when you intentionally want the source enabled. Set `QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=1` only when authorization covers automated access to the book, full chapter-list, and chapter pages. Search remains subject to the source's public robots policy.

For storage outside the release directory, use absolute paths:

```dotenv
QUIETLEAF_CATALOG_DIR=/srv/quietleaf/catalog
QUIETLEAF_ARCHIVE_DIR=/srv/quietleaf/archive
```

The AMP instance user needs read access to the catalog and read/write access to the archive. Back up the entire archive directory, not only `archive.sqlite3`, because SQLite may have active WAL files.

## 4. Configure AMP Generic

Use these process settings in the Generic instance:

| Setting | Value |
|---|---|
| Working directory | QuietLeaf service directory |
| Executable | `/bin/sh` |
| Arguments | `amp-start.sh 8765` |
| Shutdown | Normal SIGTERM/SIGINT process stop |
| Restart on failure | Enabled |
| Sleep when empty | Disabled |

Replace `8765` with the port allocated to this instance. The first argument overrides `PORT` in `.env`, so the same release can be used in different AMP instances.

Do not append `&`, use `nohup`, enable Uvicorn `--reload`, or start multiple workers. `amp-start.sh` uses `exec`, leaving Uvicorn as AMP's foreground process.

Start the instance, then validate it from the host:

```bash
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS -H 'Authorization: Bearer <token>' http://127.0.0.1:8765/v1/health
```

The first endpoint is a minimal probe for AMP. Detailed status and all archive data remain Bearer-protected.

## 5. Connect Cloudflare Tunnel

In Cloudflare, add a published application route:

| Field | Value |
|---|---|
| Hostname | `books.example.com` |
| Service | `http://127.0.0.1:8765` |

Use the actual loopback port assigned to the AMP instance. Keep the QuietLeaf Bearer token enabled and do not add an interactive Cloudflare Access login in front of this hostname: the Android client sends the QuietLeaf `Authorization` header and cannot complete a browser login redirect.

Recommended controls:

- Keep the origin port closed to the public network.
- Add Cloudflare rate limits and a cache-bypass rule for the API hostname.
- Treat the tunnel token and QuietLeaf API token as secrets.
- Monitor `/healthz`; it reveals no source or archive information.

## 6. Use the web console

Open:

```text
https://books.example.com/admin
```

Enter the same API token used by Android. The token remains only in that browser tab's memory. The console shows runtime information, archived novels, recent jobs, and automatic follow status. It can stop automatic checks and permanently delete completed or failed archives.

Deletion removes the saved chapters, archived cover, job history, and follow state. A novel with a queued, running, or paused archive job cannot be deleted until that job finishes or is canceled through the API.

## 7. Connect Android

In QuietLeaf, open **Settings > Manage sources > Self-hosted content service** and enter:

```text
https://books.example.com
```

Use the same API token from `.env`. Validate search, book details, the full chapter list, chapter text, archive jobs, and covers over both Wi-Fi and cellular data.

## Updates and backups

1. Stop the AMP instance.
2. Back up `archive/`, `catalog/`, `.env`, and any external configuration.
3. Replace or pull the application files without deleting `.env` or `archive/`.
4. Re-run `sh install-amp.sh` to refresh the virtual environment.
5. Start the instance and check `/healthz`, `/v1/health`, and `/admin`.

Do not run old and new instances concurrently against the same archive directory. For rollback, stop the instance before restoring both the release and its matching archive backup.

## Troubleshooting

- **AMP stops immediately:** inspect the instance log. The start script rejects a missing token, missing virtual environment, or invalid port.
- **Dashboard opens but login fails:** verify `QUIETLEAF_API_TOKEN` and ensure Cloudflare is not replacing the response with an Access login.
- **NovelPing details fail:** confirm both NovelPing settings in `.env`, the authorization status, and outbound HTTPS access, then inspect the AMP log for the upstream response.
- **Only a short NovelPing preview appears:** confirm this release is current and authorized mode is enabled; full lists use NovelPing's same-origin chapter-archive response.
- **Automatic checks do not run:** disable AMP sleep and confirm the dashboard reports the archive worker as running.
