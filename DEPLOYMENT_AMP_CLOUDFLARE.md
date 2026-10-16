# AMP Host and Cloudflare Tunnel Deployment

## Recommended architecture

Run the QuietLeaf content service with Docker Compose alongside AMP, rather than inside an AMP Generic instance:

```text
Android -> https://books.example.com -> Cloudflare Tunnel -> 127.0.0.1:8765 -> QuietLeaf container
```

AMP manages game/application instances, but it is not a Docker Compose orchestrator. Running Compose from an AMP Generic instance would require broad Docker socket access and would make AMP monitor Compose instead of the API. Keep AMP and QuietLeaf isolated on the same Linux host.

## Prerequisites

- An x86-64 Linux AMP host with SSH or console access.
- Docker Engine and Docker Compose v2.
- A domain using Cloudflare DNS.
- `cloudflared` installed on the host.
- At least 2 CPU cores, 2 GiB free RAM, and enough disk for the container image and archive.
- Outbound HTTPS/DNS and outbound TCP/UDP 7844 for Cloudflare Tunnel.
- Explicit permission before enabling authorized NovelPing browser access or archiving content.

Do not open inbound port 8765. The supplied Compose file binds it only to loopback.

## 1. Prepare persistent paths

Use stable host paths rather than an AMP instance directory:

```text
/opt/quietleaf/service       application release
/srv/quietleaf/catalog       catalog files
/srv/quietleaf/archive       SQLite archive and covers
/etc/quietleaf/quietleaf.env secrets and runtime settings
```

Copy `catalog` and any existing `archive` contents before the first start. Protect the environment file with mode `0600`.

Example `/etc/quietleaf/quietleaf.env`:

```dotenv
QUIETLEAF_API_TOKEN=<long-random-token-from-a-password-manager>
QUIETLEAF_ARCHIVE_MAX_BYTES=2147483648
QUIETLEAF_FOLLOW_INTERVAL_SECONDS=3600
QUIETLEAF_ACTIVE_INTERVAL_SECONDS=1800
QUIETLEAF_ENABLE_NOVELPING=1
QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=0
QUIETLEAF_NOVELPING_IMAGE_HOSTS=images.novelping.com
```

Set `QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=1` only when the source owner has authorized the automated book and chapter access described in the README.

For host-level persistent paths, add a Compose override outside the release directory:

```yaml
services:
  content-api:
    init: true
    volumes:
      - /srv/quietleaf/catalog:/app/catalog:ro
      - /srv/quietleaf/archive:/app/archive:rw
```

## 2. Build and start

From `/opt/quietleaf/service`:

```bash
docker compose \
  --env-file /etc/quietleaf/quietleaf.env \
  -f compose.yaml -f /etc/quietleaf/compose.host.yaml \
  config --quiet

docker compose \
  --env-file /etc/quietleaf/quietleaf.env \
  -f compose.yaml -f /etc/quietleaf/compose.host.yaml \
  up -d --build
```

The first build downloads Chromium and its Linux dependencies. Keep one API container and one Uvicorn worker because the archive scheduler uses a single SQLite database.

Validate locally without placing the token in a URL:

```bash
curl -fsS \
  -H 'Authorization: Bearer <token>' \
  http://127.0.0.1:8765/v1/health
```

The response should identify `quietleaf-content-api`, report API version 1, and list the expected sources.

## 3. Create the Cloudflare Tunnel

1. In Cloudflare, create a named tunnel under **Networking > Tunnels**.
2. Install the dashboard-provided connector command as a native system service on the AMP host. Treat the tunnel token as a secret.
3. Add a published application route:
   - Hostname: `books.example.com`
   - Service: `http://127.0.0.1:8765`
4. Confirm Cloudflare creates the proxied tunnel DNS record.
5. Enable tunnel health notifications and add a cache-bypass rule for this API hostname.

No WebSocket option is needed; QuietLeaf uses ordinary HTTP requests and polling.

## 4. Authentication policy

Keep QuietLeaf's long random Bearer token enabled. The current Android client is not compatible with a normal Cloudflare Access login or service-token policy because it sends only the origin `Authorization: Bearer ...` header and rejects redirects.

For this version:

- Do not put an interactive Cloudflare Access challenge in front of the API hostname.
- Use the Cloudflare Tunnel, the QuietLeaf API token, WAF/rate limits, and the loopback-only origin binding.
- Do not expose port 8765 on the public host firewall.

Cloudflare Access can be added later after Android supports the separate `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers for API and cover requests.

## 5. External validation

From a machine outside the AMP network:

```bash
curl --max-redirs 0 -sS -o /dev/null -w '%{http_code}\n' \
  https://books.example.com/v1/health
```

Expect `401`. Then repeat with the Bearer token and expect `200`. A `302` or HTML login page means a Cloudflare Access policy is intercepting the app.

In Android, connect **Settings > Manage sources > Self-hosted content service** to exactly:

```text
https://books.example.com
```

Validate search, cover images, book details, chapter text, archive jobs, and an archived cover over both Wi-Fi and cellular data.

## 6. Backups, updates, and rollback

- Back up `/srv/quietleaf/archive`, `/srv/quietleaf/catalog`, and deployment configuration off-host.
- For a simple consistent SQLite backup, stop the API container, copy the full archive directory, and restart it.
- Stage each update in a new release directory and use an immutable image tag.
- Run the Python and Android tests before deployment.
- Do not run old and new containers concurrently against the same archive.
- Keep the previous image and pre-update archive backup for rollback.
- Preserve the hostname and loopback port so Android and Cloudflare need no changes during rollback.

## Information needed before deployment

- AMP host operating system and architecture.
- SSH/root access and whether Docker Compose v2 is already installed.
- The chosen domain and subdomain.
- Whether Cloudflare already has a tunnel connector on the host.
- Desired archive size and backup destination.
- Whether authorized NovelPing browser mode should remain enabled.
