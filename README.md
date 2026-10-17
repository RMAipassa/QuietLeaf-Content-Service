# QuietLeaf Content Service (v0.7)

A small Python HTTPS-ready API **separate** from the Android reader. It supplies books through configured adapters while QuietLeaf stores progress and downloaded chapters locally. Running the service is optional: Gutenberg and imports work without it. When using the service, you need a reachable server; the app rejects plain HTTP.

The companion Android application is maintained in the separate private repository `QuietLeaf-Android`.

## Quick start

Requirements: Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
QUIETLEAF_API_TOKEN='generate-a-long-random-token' ./run.sh
```

The server is now listening only on `127.0.0.1:8765`. Chapters archived by the service are stored persistently in `archive/archive.sqlite3` (configurable with `QUIETLEAF_ARCHIVE_DIR`). It has a small **original fictional book** containing two downloadable text chapters and one metadata-only chapter. Check locally with:

```bash
curl -H 'Authorization: Bearer generate-a-long-random-token' \
  'http://127.0.0.1:8765/v1/search?q=lighthouse'
```

On Android, go to **Settings > Manage sources > Self-hosted content service** and enter its HTTPS URL and token. Find the new **My content service** tab in Discover. The sample chapters are safe test content; your own permitted catalog can replace them.

Open `http://127.0.0.1:8765/admin` and enter the same token to use the private service console. It shows runtime status, saved novels, archive jobs, and automatic checks, and can stop checks or permanently delete archives.

### CubeCoders AMP Python App Runner

Create a native **Python App Runner** instance; Docker and a Generic instance are not required. The essential runner settings are:

```text
App Download Type: Git repo
App Download Source: https://github.com/RMAipassa/QuietLeaf-Content-Service.git
Run App Setup Command: enabled
App Setup Command: python -m pip install -e .
App Run Mode: Python module
App Module Name: uvicorn
App Command Line Arguments: quietleaf_service.app:app --host 127.0.0.1 --port {{$ServerPort}} --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

Create `.env` next to `pyproject.toml` from `amp.env.example`; QuietLeaf loads it directly from the runner's working directory. See [`DEPLOYMENT_AMP_CLOUDFLARE.md`](DEPLOYMENT_AMP_CLOUDFLARE.md) for all AMP fields, private repository authentication, Cloudflare Tunnel configuration, updates, backups, and security guidance. The shell helpers remain available for a manual Linux deployment outside Python App Runner.

### Docker Compose (alternative)

The service includes `Dockerfile` and `compose.yaml` for machines running Docker:

```bash
export QUIETLEAF_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build -d
```

Save the generated token securely; you will need the same token in the Android app. Compose binds only to `127.0.0.1:8765`. To expose it securely to your phone, put a trusted HTTPS proxy in front of that local port. The sample catalog is mounted read-only at `catalog`; replace it with your own permitted files as needed. The writable bind mount `archive/` contains the persistent SQLite archive and survives `docker compose down`, container recreation and image rebuilding. Do not delete this folder when upgrading.

### Reaching the server from Android

For a phone on another device, expose the local service **through HTTPS** using a trusted reverse proxy, a VPN service with a valid HTTPS hostname, or another TLS termination option under your control. Keep the API token enabled, do not expose the plain local HTTP port directly to the public internet, and do not place your token in a URL or Git repository. Android only permits verified HTTPS for this connector. Example Caddy configuration for a domain you control:

```caddy
books.example.org {
    reverse_proxy 127.0.0.1:8765
}
```

Ensure the reverse proxy can reach the local server and your domain has valid TLS. This is an example, not a deployed endpoint. In a real setup, use your hostname and appropriate firewall rules.

For a CubeCoders AMP host and Cloudflare Tunnel, see [`DEPLOYMENT_AMP_CLOUDFLARE.md`](DEPLOYMENT_AMP_CLOUDFLARE.md).

## Non-scraped content

`catalog/catalog.json` is a manually maintained catalog. Each book includes a stable ID, title, author, genre, description and chapter list. Each chapter may have a `file` pointing to a UTF-8 TXT file under `catalog/` and `copyPermitted: true`. A chapter without an eligible file is **metadata only** (not downloaded). No copying occurs when importing metadata. You can build this catalog from original work, author-provided feeds or legitimately obtained, downloadable chapters.

IDs use only letters, numbers, underscores and hyphens; they deliberately do **not** include hostnames. Do not rename book or chapter IDs if you want to preserve your Android reading history when a source domain changes.

Set `QUIETLEAF_CATALOG_DIR` to use a different catalog folder.

## HTML adapters (only where permitted)

The service includes a configurable `HtmlSource` using CSS selectors. It is *not* enabled by default. See `examples/authorized-html-source.example.json` for the schema.

1. Confirm authorization to extract and copy the source material, and confirm automated access is allowed.
2. Copy the example to your own JSON file and replace its sample hostname, paths and selectors.
3. Set `accessAuthorized` to `true` only when appropriate.
4. Start the service with `QUIETLEAF_HTML_SOURCES=/absolute/path/to/my-sources.json`.

The adapter checks `robots.txt`, rate-limits calls, uses HTTPS, declines cross-domain links or redirects, caps HTML sizes, and only extracts visible text. It does not bypass access controls, CAPTCHA, DRM or paywalls. Some sites require JavaScript and therefore won't work with this basic CSS adapter.

A trusted source changing domains requires **updating `baseUrl` yourself** and restarting the service after verifying the replacement domain through an authoritative source. The source ID and book/chapter path IDs remain unchanged. There is no unverified mirror search or automatic switching.

## NovelPing opt-in integration (experimental)

The source implementation in `quietleaf_service/novelping.py` reads ordinary, public HTML pages with no login and no DRM or anti-bot bypass. It keeps the source identifier `novelping` stable and validates URLs against the configured HTTPS origin. Its `robots.txt` policy checks and rate limit remain active. **This is designed for a locally hosted, single-reader deployment.** Do not expose a public redistribution endpoint.

NovelPing's published terms allow free reading and browsing, but restrict copying without proper authorization. Private use does not itself resolve that restriction or local copyright questions. Enable chapter downloads only for content you have the right to copy. You are responsible for checking applicable permissions.

Enable the adapter with:

```bash
QUIETLEAF_ENABLE_NOVELPING=1 QUIETLEAF_API_TOKEN='your-secret-token' ./run.sh
```

Or with Docker Compose:

```bash
export QUIETLEAF_API_TOKEN='your-secret-token'
export QUIETLEAF_ENABLE_NOVELPING=1
docker compose up --build -d
```

If the source owner has explicitly authorized automated access to NovelPing book, full chapter-list, and chapter pages despite its public robots policy, enable the separate authorized-access mode:

```bash
export QUIETLEAF_API_TOKEN='your-secret-token'
export QUIETLEAF_ENABLE_NOVELPING=1
export QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=1
docker compose up --build -d
```

This mode makes bounded HTTPS requests to `/book/` pages and to the same-origin `/ajax/chapter-archive` response used by NovelPing's Chapter List tab. It does not execute site JavaScript. It remains disabled by default, keeps the configured request interval and same-origin restrictions, blocks redirects, and limits HTML responses to 2 MiB. Search and browse pages continue through the robots-aware path. Enable it only while your authorization covers automated page and chapter-list access and copying or archiving the requested content.

The Android app sees the `novelping` provider through its existing **My content service** discovery tab, when that content service has been connected via Settings. Chapters that are returned through the API can use the existing local download queue.

**Verification status:** Search, metadata, current cover markup, full chapter archives, and chapter text have been validated against the live site as well as fixture tests. NovelPing's book page exposes only a short preview; QuietLeaf now reads the complete Chapter List response in authorized mode. The integration remains experimental because a site layout change can still break extraction; the adapter reports errors instead of inventing chapters. The opt-in switch does not mean the integration is production-ready.

If the website uses a different search route, set `QUIETLEAF_NOVELPING_SEARCH_PATH` to a relative path containing `{query}`. For example, `'/search?q={query}'`. If NovelPing changes to a verifiably authentic new domain, change `QUIETLEAF_NOVELPING_BASE_URL` and restart the service. The source IDs and chapter identifiers remain independent of its domain.

NovelBin is not preconfigured. It can use the generic adapter when you have a workable, permissible source and stable HTML. An adapter configuration does not grant rights to a novel.

## New in v0.7: AMP hosting, service console, and complete chapter lists

- CubeCoders AMP's native Python App Runner can install and run the service directly without Docker; `install-amp.sh` and `amp-start.sh` remain available for manual Linux deployments.
- `/admin` provides a responsive management console for service status, saved novels, archive jobs, follow state, cleanup, and stopping automatic checks.
- `/healthz` provides a minimal unauthenticated process probe for AMP and Cloudflare monitoring. It exposes no archive or source details.
- NovelPing authorized mode reads the full chapter archive instead of treating the short book-page preview as the complete list.

The admin shell contains no private data. Its token is kept only in browser memory, while every data and management request continues to require the configured Bearer token.

## New in v0.6 — covers saved with novel archives

The experimental NovelPing source extracts cover URLs from the live `novel-cover-loading` search/detail markup, lazy-loaded attributes, and Open Graph/Twitter image metadata. The generic HTML adapter also recognizes optional `resultCover` and `cover` CSS selectors for image elements.

When you archive a novel, the worker attempts to fetch and store its cover once, separately from chapter text. It accepts only bounded JPEG, PNG, GIF, or WebP bytes (maximum 2 MiB) and stores the image in `archive.sqlite3` with the book. Cover retrieval failure does not cancel book archiving. A previously archived book can acquire its cover on the next successful recheck.

The API exposes an authenticated GET `/v1/books/{source}/{bookId}/cover` when an image is saved, and returns `coverArchived: true` in book metadata. Once saved, the image is available even when the source site or adapter disappears. The Android client chooses the archived image endpoint when available and sends the Bearer token via a Coil image request; its disk cache is disabled for server-backed covers. This flow has been compiled and verified on an Android emulator.

Same-origin image URLs and NovelPing's verified `images.novelping.com` CDN are accepted by default. Additional official image CDNs require their exact hostnames in `QUIETLEAF_NOVELPING_IMAGE_HOSTS` (comma-separated). The service checks each host's robots.txt, rate-limits requests, refuses redirects, and never accepts arbitrary unapproved cover destinations. The Android app may load the original image URL while a cover is not yet archived.

Existing `archive.sqlite3` databases are upgraded automatically by creating the new `covers` table. The archive storage cap now includes the stored cover bytes. Removing an archived book also deletes its saved cover; unfollowing alone retains it.

## Previously in v0.5 — keep following novels automatically

Every full-book archive request also **follows** that novel. The server's own worker checks the source for newer chapters while the service is running. A phone does **not** need to remain connected. Chapter text already stored is never re-downloaded during a successful update check. New chapter text is archived to SQLite, so the book can still be served if the source subsequently disappears.

Default schedule: **once per 60 minutes per followed novel**. A novel read in the last 48 hours is checked **once per 30 minutes**. Requests are spaced according to each source adapter's rate limits; repeated failures back off exponentially to a maximum of 24 hours. Intervals can be changed using `QUIETLEAF_FOLLOW_INTERVAL_SECONDS` (default 3600) and `QUIETLEAF_ACTIVE_INTERVAL_SECONDS` (default 1800); 1800 seconds is the minimum enforced for each. The worker checks for due work every 30 seconds and prioritizes recently read books. Restarting the server preserves follow status, timestamps, scheduled checks and saved chapter text.

To archive more than one book, the Android **Library > Select books to archive** action calls the batch endpoint. The batch size is limited to 50 unique books per request. Example:

```bash
curl -X POST 'http://127.0.0.1:8765/v1/archive/batch' \
  -H 'Authorization: Bearer your-secret-token' \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"source":"catalog","bookId":"example"}]}'
```

All batch operations are **server-only**. On Android, chapters can now be read by streaming them into memory from the service; this does not place permanent chapter files in app storage. Explicit **Download all** and **Keep this chapter offline** still store content on the phone. If the phone is offline and has no local copies, it cannot reach your server archive. If the original website is down but your content service remains reachable, stored server chapters are readable.

Use `GET /v1/archive/follows` to see scheduled and last-check status, `POST /v1/archive/activity/{source}/{book}` to prioritize a recently read book, and `DELETE /v1/archive/follows/{source}/{book}` to stop checking without deleting the archive. A book deletion clears its follow status. The schedule is **only active while the Python service runs**, is not a cloud task, and should be used with one worker process against its SQLite archive directory.

## Persistent personal archive (v0.4)

The content service can **keep complete chapter text, chapter order, metadata and download-job state in SQLite**. A book is queued manually; the worker fetches its available chapters one at a time using its configured source adapter. Data written before a request fails remains in the archive, and finished chapters are not fetched twice on retry. The archive survives server restarts, Docker rebuilds and a source adapter being disabled.

The Android **book details** screen includes **Archive full book + follow updates** for service-backed books, with basic progress reporting. This is separate from **Download all**, which copies chapters into the phone's private storage. If the source later goes offline, the service serves saved chapters from SQLite before trying the live site. If the service itself is unreachable when you're away from home, only chapters downloaded **onto your Android phone** will be available offline.

For manual archiving, start the local service with a nonempty API token and a configured source, then run:

```bash
curl -X POST -H 'Authorization: Bearer your-secret-token' \
  'http://127.0.0.1:8765/v1/archive/novelping/YOUR-BOOK-ID'
```

The response includes a `job id`. Read progress using:

```bash
curl -H 'Authorization: Bearer your-secret-token' \
  'http://127.0.0.1:8765/v1/archive/jobs/JOB_ID'

curl -H 'Authorization: Bearer your-secret-token' \
  'http://127.0.0.1:8765/v1/archive/books'
```

If interrupted, a running job becomes queued again after service restart. Failed jobs can be retried by selecting **Archive full book + follow updates** again, or by sending `POST /v1/archive/jobs/JOB_ID/retry`. Other job actions: `pause`, `resume`, `cancel`. A running chapter request cannot be interrupted mid-transfer, but its result will not be saved after the job is paused or canceled. Use `DELETE /v1/archive/books/{source}/{id}` to remove the book and its stored chapter text; cancel an active job first.

SQLite is the only archival dependency. It has a default **2 GiB chapter-text storage limit** configured using `QUIETLEAF_ARCHIVE_MAX_BYTES`. The service archives only user-selected books and now automatically checks followed books while running; it never automatically archives an entire site. You select books; it preserves only chapters that it has actually fetched. The latest available chapter listing can still be retrieved if a source is online. If the source is offline, chapters which were indexed but never stored will not be readable.

Use a private network, an API token, and HTTPS when exposing the server beyond localhost. Make a backup of the `archive/` directory with the service stopped, or use SQLite's online backup API for a live backup. The archive is not encrypted at rest. Verify that you have the right to download the works you select. A publicly accessible website does not automatically grant copying rights; in the Netherlands, personal copying from an unlawful source is prohibited: https://www.government.nl/documents/question-and-answer/can-i-use-other-peoples-texts-music-or-photographs

**Live NovelPing status:** Search results, author metadata, cover URLs, book details, chapter lists, and chapter text have been validated against live NovelPing pages. Archive recovery and failure behavior remain covered primarily by simulated HTML and original test-story chapters. An archive job requires a working source adapter and cannot make an inaccessible chapter readable.

## HTTP API

| Endpoint | Result |
|---|---|
| `GET /healthz` | Minimal process probe without private details |
| `GET /admin` | Browser-based service console shell |
| `GET /v1/health` | Versioned service identity, source list and archive status |
| `GET /v1/search?q=...&genre=...` | Search across all registered sources |
| `GET /v1/recent` | Recently updated books where known |
| `GET /v1/books/{source}/{id}` | Book details |
| `GET /v1/books/{source}/{id}/chapters` | Ordered chapter metadata including `downloadable` |
| `GET /v1/books/{source}/{id}/chapters/{chapter}/text` | Archived chapter first; otherwise live source text if available |
| `POST /v1/archive/{source}/{id}` | Queue or reuse a full-book archive job and enable automatic updates |
| `POST /v1/archive/batch` | Archive and follow up to 50 user-selected novels on the server |
| `GET /v1/archive/follows` | View automatic-check subscriptions, next checks and last errors |
| `DELETE /v1/archive/follows/{source}/{id}` | Stop checks but retain saved text |
| `POST /v1/archive/activity/{source}/{id}` | Mark a book as recently read for the activity-based schedule |
| `GET /v1/archive/jobs` | List recent archive jobs |
| `GET /v1/archive/jobs/{job}` | View persistent job progress |
| `POST /v1/archive/jobs/{job}/{action}` | Pause, resume, retry, or cancel |
| `GET /v1/archive/books` | List books saved in the server archive |
| `GET /v1/archive/books/{source}/{id}` | Archived-chapter count and storage size |
| `DELETE /v1/archive/books/{source}/{id}` | Permanently delete an archived book |

Every `/v1` endpoint requires `Authorization: Bearer <token>` when `QUIETLEAF_API_TOKEN` is configured. The public `/admin` shell contains no service data and `/healthz` returns only process status. No accounts, ads or commercial services are needed by this backend; the private archive uses local SQLite. Access tokens are stored in the Android app's private preferences; they are not hardware-backed encrypted. Use a low-privilege token dedicated to this service.

## Tests

```bash
python -m pytest -q
```

Automated tests cover persistent archiving, source-offline fallback, restart and retry recovery, deduplication, storage limits, source removal, simulated NovelPing archiving, complete NovelPing chapter archives, dashboard security, per-source failure isolation, API authentication, text access, metadata-only chapters, book/chapter ordering, a fixture-driven HTML extraction adapter, cross-origin link blocking, robots restrictions, path traversal and authorization requirement.

## Current limitations

The service has no built-in automatic domain verification, multi-user management or HTTPS certificates. It does have periodic checks for specifically followed novels, but no broad site crawl. It now persists a user-selected chapter archive but has a scheduled new-chapter checker for followed books or update notifications. Do not use it as a publicly open bulk mirror. Download permission is configured by the catalog/source owner, not verified by a licensing service.
