# QuietLeaf Content Service (v0.8)

A small Python HTTPS-ready API **separate** from the Android reader. It supplies books through configured adapters while QuietLeaf stores progress and downloaded chapters locally. Running the service is optional: Gutenberg and imports work without it. When using the service, you need a reachable server; the app rejects plain HTTP.

The companion Android application is maintained in the separate private repository `QuietLeaf-Android`.

## Quick start

Requirements: Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
QUIETLEAF_API_TOKEN='generate-a-long-random-token' \
QUIETLEAF_ADMIN_PASSWORD='choose-a-long-unique-password' ./run.sh
```

The server is now listening only on `127.0.0.1:8765`. Chapters archived by the service are stored persistently in `archive/archive.sqlite3` (configurable with `QUIETLEAF_ARCHIVE_DIR`). It has a small **original fictional book** containing two downloadable text chapters and one metadata-only chapter. Check locally with:

```bash
curl -H 'Authorization: Bearer generate-a-long-random-token' \
  'http://127.0.0.1:8765/v1/search?q=lighthouse'
```

On Android, go to **Settings > Manage sources > Self-hosted content service** and enter its HTTPS URL and token. Find the new **My content service** tab in Discover. The sample chapters are safe test content; your own permitted catalog can replace them.

Open `http://127.0.0.1:8765/admin` and enter `QUIETLEAF_ADMIN_PASSWORD` to use the private service console. It shows runtime status, saved novels, archive jobs, and automatic checks, and can stop checks, permanently delete archives, or update safe operational settings. The browser receives an HTTP-only session cookie that remains valid for 30 days and survives service restarts. Logging out removes the cookie; changing the configured password invalidates existing sessions.

The admin password must contain at least 12 characters. If `QUIETLEAF_ADMIN_PASSWORD` is not set, the console accepts `QUIETLEAF_API_TOKEN` as a temporary compatibility fallback. Configure a separate password so the API token never needs to be entered into a browser.

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
export QUIETLEAF_ADMIN_PASSWORD='choose-a-long-unique-password'
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

## Private library (v0.10)

The authenticated private library stores owned **TXT, EPUB, CBZ, and PDF** files. Upload from Android's **Private** tab or the service console's **Private library** panel. Both use the same catalog, accessible as source `private` / **Private library**. The original uploaded bytes are retained for download from the console.

The library is enabled automatically when archive storage and an API token or admin password are configured. Its metadata database and immutable original files live under `QUIETLEAF_ARCHIVE_DIR/private-library/`; back up this directory with the archive. Text EPUBs are indexed in spine order, CBZ pages are naturally sorted, and image EPUBs retain spine image order. Image EPUBs additionally get an ordered CBZ offline copy. PDFs are rendered on demand. Android can read volumes from the server or keep a whole volume offline.

Defaults: **100,000,000 bytes per upload**, **10 GiB total** private-library storage, and **2,000 image/PDF pages** per volume. Override `QUIETLEAF_PRIVATE_MAX_UPLOAD_BYTES` and `QUIETLEAF_PRIVATE_MAX_BYTES` if needed, taking your reverse proxy's upload limit into account. TXT files up to 32 MiB are split into reader-sized parts; each extracted EPUB text chapter must fit the existing 2 MiB chapter limit. ZIP expansion is bounded. Uploads use a streamed raw request body, not multipart or base64. Encrypted EPUB content and password-protected PDFs require an unlocked input file.

```bash
curl -X POST 'https://your-service.example/v1/private/books?filename=owned-book.epub' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/octet-stream' \
  --data-binary '@owned-book.epub'
```

| Endpoint | Purpose |
|---|---|
| `GET /v1/private/books?q=...&offset=0&limit=100` | Search/paginate private items |
| `POST /v1/private/books?filename=...&title=...&author=...` | Stream an upload; optional title/author overrides |
| `GET /v1/private/books/{id}/file` | Download the unchanged original |
| `GET /v1/private/books/{id}/download` | Offline document (ordered CBZ for image EPUB, otherwise original) |
| `GET /v1/private/books/{id}/pages/{zeroBasedPage}` | Authenticated JPEG page rendering |
| `DELETE /v1/private/books/{id}` | Delete the server item and original |

Private books also use the ordinary `/v1/books/private/{id}` metadata, chapter, text, and cover endpoints. Identical uploads are deduplicated by file hash. Private items are already stored on the server and do not need source-follow/archive jobs. Server deletion retains copies already downloaded to a phone.

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

Service v0.12.1 preserves the website's explicit search results instead of applying a second exact-phrase filter. NovelPing's escaped `\%` is displayed as `%`, including saved server metadata. A search for `100% drop rate` now returns the site's matches; recommendations outside the result area are excluded even when the search has no results.

## WTR-LAB opt-in integration

The dedicated `wtr-lab` source supports search, browse shelves, metadata, full chapter lists, chapter text, covers, Android downloads, and the server archive. It uses WTR-LAB's same-origin page and reader requests, with optional account-session configuration below. All upstream requests, including signed chapter content and cover images, share a process-local limit of one request every two seconds.

WTR-LAB's `%{visible name|alternate-name metadata}` title annotations are displayed as their visible names in book and chapter titles. This also applies when serving previously saved server archives, even with the live adapter disabled. Refresh the Android home shelves or book details to replace old cached titles.

Service v0.12.1 also supports the current indexed chapter glossary (`※13⛬` / `※13〓`) alongside the older dictionary/encrypted glossary. For example, *Game Arrival: 100% Drop Rate, I'm Invincible!* chapter 1 now displays **Lin Feng** rather than `※13⛬`. Missing glossary entries produce a retryable error instead of saving unreadable names. Existing plain-text downloads do not contain the glossary needed to repair those markers. For affected chapters already saved on the server, use **Re-archive** in the service console. Delete and re-download any affected phone-local copies afterward.

Recognized source promotional sentences (for example, “Hosted on NovelPing” or “Source is WTR-LAB”) are removed from chapter text for the matching provider. Cleanup applies to new source responses and previously stored server chapters when served, without rewriting the archive. Ordinary story prose and dialogue are preserved. Android v0.9.1 also applies the cleanup while reading old phone-local downloads.

Service v0.10.2 excludes inline `display:none`, `visibility:hidden`, and HTML `hidden` elements from chapter extraction, including hidden branding spans whose wording is not recognized. Existing plain-text archives and Android v0.10.3 recognize source-name lookalikes (such as the Cyrillic `о` in `nоvelping.com`) for comparison only. The paired “Enjoying the story? Read on at …” notice is removed together; standalone questions and quoted dialogue are preserved. Reopen a chapter after updating; no archive or phone chapter re-download is required.

Service v0.9.5 and Android v0.9.2 expand this cleanup to the observed rotating notices, domain variants such as `novelping(.)com`, `novelping . com`, and `novelping dot com`, and notices immediately after quoted dialogue. Every matching sentence is removed, not just the first. Reopen a chapter after updating to reload its displayed text; existing archived or downloaded chapters do not need to be downloaded again.

To find a WTR-LAB novel by ID, select **WTR-LAB** in Android's Discover screen and enter the number from its `/en/novel/{id}/...` URL in the existing search box. Plain IDs such as `113119`, `#113119`, and `id:113119` resolve the exact novel; one- and two-digit IDs also work. Genre filters still apply. An unknown ID returns no matches, while ordinary text continues to use title search. This requires content service v0.9.3 or newer and no additional APK update.

This integration is disabled by default and will not start from the enable flag alone. Enable it only when you have authorization for automated access and for the reading or copying you perform:

```bash
export QUIETLEAF_API_TOKEN='your-secret-token'
export QUIETLEAF_ENABLE_WTRLAB=1
export QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED=1
docker compose up --build -d
```

### WTR-LAB account session (v0.12.1)

WTR-LAB permits guests to preview only the first **10 AI-translated chapters**. Later chapters return account-required code `1401`; the enable/authorization flags above do not sign you in.

1. Sign in to your own account at `https://wtr-lab.com/auth/login` in your browser and complete any site verification there.
2. In the browser's developer tools **Network** panel, open a successful `/api/reader/get` request. Copy the **value** of its request `Cookie` header (the `name=value; name=value` pairs, without `Cookie:`).
3. Set `QUIETLEAF_WTRLAB_COOKIE` to that value in the service's private AMP environment or ignored `.env` file, then restart the content service. Docker Compose forwards the same variable. No Android update or phone-side source configuration is required.

The session stays on the service and is sent only to `https://wtr-lab.com:443`, never to the image CDN, redirect destinations, API clients, or health responses. Cookie configuration with an alternate base origin is rejected. Treat the value as an account credential: do not commit or share it. If WTR-LAB expires or revokes the session, sign in again and replace it; QuietLeaf reports this explicitly. The service does not store your account password, sign in automatically, or spend account credits to unlock chapters. Unlock any locked chapter on WTR-LAB first.

Guest chapter 1 and the chapter-11 account-required response have been checked live. Authenticated chapter access requires your configured valid session; tests verify forwarding, expiry errors, redirect/CDN isolation, and locked-chapter handling using fixtures.

### Optional Camoufox browser probe (v0.12.2)

The one-shot `quietleaf_service.browser_probe` diagnostic tests whether a real browser on the current host can render chapters **1 and 11** of *Game Arrival: 100% Drop Rate, I'm Invincible!*. It uses `QUIETLEAF_WTRLAB_COOKIE` and `QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED` from the process environment or private `.env`. Only the account session token is imported into the browser; analytics and copied Cloudflare clearance cookies are excluded.

```bash
python -m pip install '.[browser-test]'
python -m camoufox fetch
python -m quietleaf_service.browser_probe
```

Install the browser's native dependencies first. Linux also requires `Xvfb` (`sudo apt-get install xvfb` on Debian/Ubuntu); the probe uses a virtual display there. Windows services require a Python installation accessible to the AMP service account. A Python installed only under an interactive user's profile is insufficient. Browser binaries, package/model data and runtime directories must be accessible to that same service account. Allow several GB of disk space for the browser and its dependencies.

The probe prints JSON status lines, character counts and a glossary-name check, never cookies, account details, chapter prose, page dumps, screenshots or raw exception messages. It spaces intercepted WTR-LAB requests by two seconds, blocks unrelated hosts and source image/font/media loads, caps source requests and stops on verification/login/locked-chapter screens. The overall browser-test timeout defaults to nine minutes. It does not click CAPTCHAs, unlock chapters, write archives, or register a browser-backed source adapter.

For a dedicated **test** Python App Runner instance, use setup command `python -m pip install '.[browser-test]'`, module `quietleaf_service.browser_probe`, and arguments `--fetch-browser`. Set the source variables privately first and provide the native dependencies on the host. The explicit fetch flag downloads the browser before starting the bounded test. Disable automatic restart/sleep for this one-shot diagnostic. Successful completion exits with code 0; an unsuccessful browser test exits 1; a prerequisite/configuration error exits 2. `--headed` can show the browser on an existing desktop, and `--browser-path` can select an already-installed Camoufox executable.

This is a feasibility diagnostic, not automatic integration into the existing HTTP source. A successful local run establishes the result only for that machine, browser and account session; the production AMP host still needs its own check.

### Sustained Camoufox archive-job diagnostic (v0.12.3)

`quietleaf_service.browser_archive_probe` tests the real `ArchiveWorker` and SQLite archive, fetching the first **40 consecutive chapters** of the same WTR-LAB novel. It retains one browser context throughout the job and runs the existing metadata, reader initialization, signed-content and glossary contracts through same-origin browser `fetch`. Every intercepted WTR-LAB request shares the two-second gate, including the initial page's scripts. Browser rendering is used to bootstrap the session; individual chapter responses are decoded by the existing WTR-LAB adapter.

```bash
python -m quietleaf_service.browser_archive_probe \
  --archive-dir ./camoufox-archive-test-40 --chapters 40 --timeout 900
```

The optional browser dependencies and account/authorization variables above are required. Use a dedicated output directory whose name starts with `camoufox-archive-test`; never pass the production archive. The diagnostic excludes covers and limits its chapter list to the selected consecutive range (maximum 100). It fails if that range includes locked/unavailable chapters. It does not spend credits or solve verification challenges.

Progress is logged only after the worker's durable job state changes. A verification response stops the job and retains previously saved chapters. To explicitly retry the same durable checkpoint after resolving the access requirement, rerun with `--resume`. A fresh run requires a fresh directory; its summary distinguishes **saved chapters** from **chapters fetched this run**, so cached files cannot masquerade as a successful sustained download. Automatic following is disabled after the run. The same module can be launched by a dedicated AMP test instance, with output visible in its console.

**Observed live result, 2026-11-10:** In a Windows AMP 2.8.0.10 service-account run with Camoufox 0.5.9, the 40-chapter job saved chapters **1–27**, then WTR-LAB requested interactive verification for **chapter 28**. The job stopped as failed with its 27/40 checkpoint intact. A read-only audit confirmed 27 distinct chapter bodies, 5,753–12,125 characters each, zero unresolved glossary markers, and no saved chapter-28 challenge response. This reproduces the sustained-download limitation: Camoufox browser transport alone does not eliminate WTR-LAB's periodic verification requirement.

### Normal reader UI and automatic verification experiment (v0.12.4)

The earlier archive test fetched chapter APIs from a browser parked on chapter 1. It stopped when verification was requested, without running that chapter's normal reader UI. `--reader-ui` navigates the real reader, lets the site's own verification widget/callback run, waits for actual prose in the requested chapter container, and archives that rendered text through the same worker.

```bash
python -m quietleaf_service.browser_archive_probe \
  --archive-dir ./camoufox-archive-test-ui-40 --chapters 40 \
  --timeout 1800 --reader-ui
```

`reader_verification_requested`, `reader_waiting_for_site_verification`, and `reader_verification_completed` events distinguish encountering a challenge from the site completing it automatically. The experiment does not click a CAPTCHA, extract/inject its tokens, or use a solving service. If the site does not complete verification/rendering within three minutes for a chapter, the job stops with its existing checkpoint retained.

Playwright routing disables its normal HTTP cache. The diagnostic therefore keeps a bounded, in-memory cache of **public** WTR-LAB `/_next/static/` JavaScript/CSS responses (maximum 64 MiB, no `Set-Cookie`). Chapter/API responses and Cloudflare challenge resources are never cached. Actual WTR-LAB network requests retain the two-second gate. `--chapter-delay 10` can add ten seconds before each chapter for a separately paced comparison; its default is zero.

Camoufox already uses Playwright. Optional engine comparisons are selected explicitly:

- **Plain Playwright Chromium:** install `.[browser-chromium-test]`, install its Chromium binary with `python -m playwright install chromium`, and use `--engine chromium`. `--blink-flag` adds `--disable-blink-features=AutomationControlled`; `--stealth` applies `playwright-stealth`. These switches are rejected for Camoufox/CloakBrowser to avoid mixing browser-specific patches.
- **CloakBrowser:** install `.[browser-cloak-test]`, configure your own current CloakBrowser license in private `CLOAKBROWSER_LICENSE_KEY`, and use `--engine cloakbrowser`. Its current binary requires its own license key, including the free tier. The WTR-LAB account session is a separate credential.
- `--headed` requests a visible browser; a service host needs a working desktop/display for that mode. Linux can supply a virtual display. Windows AMP's headless service-account mode and an interactive user's headed browser are distinct environments.

These comparison options are diagnostic, not guarantees of site acceptance. Use a fresh output directory for each comparison and compare verification events as well as completed chapter counts.

**Observed live result, 2026-11-11:** The normal reader UI automatically completed verification on chapters **1, 2 and 3** in a Windows AMP service-account preflight, without scripted CAPTCHA clicks or token injection. That preflight was paused at 4 chapters to correct the routing-disabled static cache. A separate fresh UI-mode job with public-static caching then completed **40/40 consecutive chapters**, all fetched in that run, with 40 distinct saved bodies (5,718–12,048 characters), zero unresolved glossary markers and zero verification-screen matches. No additional reader-verification requests were logged in that fresh 40-chapter run. It used Camoufox/Playwright in headless service mode, no extra stealth plugin and no added chapter delay; the two-second network gate remained active. The full-page UI run took about 29 minutes. This demonstrates the site's automatic UI flow and sustained archiving on that host; it does not isolate request speed as the cause of the earlier API-only failure.

The adapter rejects page, API, and reader redirects, untrusted content and cover hosts, oversized responses, and Turnstile challenges. Covers permit only WTR-LAB's single HTTPS image-proxy handoff to the allowlisted `img.wtr-lab.com` host; further redirects remain blocked. It does not bypass authentication, CAPTCHA, paywalls, or other access controls. WTR-LAB is then exposed through Android's existing **My content service** discovery source; no separate Android provider configuration is required. A full archive can take substantial time because each reader initialization and signed-content request remains subject to the shared two-second interval.

NovelBin is not preconfigured. It can use the generic adapter when you have a workable, permissible source and stable HTML. An adapter configuration does not grant rights to a novel.

## New in v0.8: Android home shelves

- Authenticated `GET /v1/home` supplies Popular Now, Completed Stories, Fresh Updates, New Releases, and Hot Reads to the Android home screen.
- Authenticated `DELETE /v1/home/cache` requests an immediate background refresh and waits for that refresh without making home reads wait.
- NovelPing categories use its real popular, completed, latest, recently added, and trending pages rather than relabeling duplicate search results.
- A single process-local worker refreshes supported sources at startup and every five minutes. `GET /v1/home` only reads a deep-copied last-good in-memory snapshot, so it never waits on NovelPing network access.
- During cold startup, before the first successful refresh, NovelPing contributes empty shelves and the existing generic source error. A failed refresh leaves the previous complete snapshot intact, and refresh failures are isolated per source.
- Manual requests coalesce with an in-progress refresh. Deploy with one Uvicorn worker so all requests share the same in-memory snapshot and worker.

## New in v0.7: AMP hosting, service console, and complete chapter lists

- CubeCoders AMP's native Python App Runner can install and run the service directly without Docker; `install-amp.sh` and `amp-start.sh` remain available for manual Linux deployments.
- `/admin` provides a responsive management console for service status, saved novels, archive jobs, follow state, cleanup, and stopping automatic checks.
- `/healthz` provides a minimal unauthenticated process probe for AMP and Cloudflare monitoring. It exposes no archive or source details.
- NovelPing authorized mode reads the full chapter archive instead of treating the short book-page preview as the complete list.

The admin shell contains no private data. It exchanges the configured admin password for a signed HTTP-only session cookie; Android and other API clients continue to use the configured Bearer token.

## New in v0.6 — covers saved with novel archives

The experimental NovelPing source extracts cover URLs from the live `novel-cover-loading` search/detail markup, lazy-loaded attributes, and Open Graph/Twitter image metadata. The generic HTML adapter also recognizes optional `resultCover` and `cover` CSS selectors for image elements.

When you archive a novel, the worker attempts to fetch and store its cover once, separately from chapter text. It accepts only bounded JPEG, PNG, GIF, or WebP bytes (maximum 2 MiB) and stores the image in `archive.sqlite3` with the book. Cover retrieval failure does not cancel book archiving. A previously archived book can acquire its cover on the next successful recheck.

The API exposes an authenticated GET `/v1/books/{source}/{bookId}/cover` when an image is saved, and returns `coverArchived: true` in book metadata. Once saved, the image is available even when the source site or adapter disappears. The Android client chooses the archived image endpoint when available and sends the Bearer token via a Coil image request; its disk cache is disabled for server-backed covers. This flow has been compiled and verified on an Android emulator.

Same-origin image URLs and NovelPing's verified `images.novelping.com` CDN are accepted by default. Additional official image CDNs require their exact hostnames in `QUIETLEAF_NOVELPING_IMAGE_HOSTS` (comma-separated). The service checks each host's robots.txt, rate-limits requests, refuses redirects, and never accepts arbitrary unapproved cover destinations. The Android app may load the original image URL while a cover is not yet archived.

Existing `archive.sqlite3` databases are upgraded automatically by creating the new `covers` table. The archive storage cap now includes the stored cover bytes. Removing an archived book also deletes its saved cover; unfollowing alone retains it.

## Previously in v0.5 — keep following novels automatically

Every full-book archive request also **follows** that novel. The server's own worker checks the source for newer chapters while the service is running. A phone does **not** need to remain connected. Chapter text already stored is never re-downloaded during a successful update check. New chapter text is archived to SQLite, so the book can still be served if the source subsequently disappears.

Default schedule: **once per 60 minutes per followed novel**. A novel read in the last 48 hours is checked **once per 30 minutes**. Requests are spaced according to each source adapter's rate limits; repeated failures back off exponentially to a maximum of 24 hours. Intervals can be changed using `QUIETLEAF_FOLLOW_INTERVAL_SECONDS` (default 3600) and `QUIETLEAF_ACTIVE_INTERVAL_SECONDS` (default 1800); 1800 seconds is the minimum enforced for each. The worker checks for due work every 30 seconds and prioritizes recently read books. Restarting the server preserves follow status, timestamps, scheduled checks and saved chapter text.

To archive more than one book, the Android **Library > Select books to archive** action calls the batch endpoint. The batch size is limited to 50 unique books per request. The service processes up to three different novels concurrently by default, while chapters within each novel remain sequential. Set `QUIETLEAF_ARCHIVE_CONCURRENCY` to `1`, `2`, or `3` to choose the initial limit, or update **Operational settings** in the admin console. Admin changes are stored in `archive.sqlite3`, apply immediately, and take precedence over environment defaults after restart. The same panel controls normal and recently-read follow intervals, with a minimum of 30 minutes. Continue using one Uvicorn worker process. Example:

```bash
curl -X POST 'http://127.0.0.1:8765/v1/archive/batch' \
  -H 'Authorization: Bearer your-secret-token' \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"source":"catalog","bookId":"example"}]}'
```

All batch operations are **server-only**. On Android, chapters can now be read by streaming them into memory from the service; this does not place permanent chapter files in app storage. Explicit **Download all** and **Keep this chapter offline** still store content on the phone. If the phone is offline and has no local copies, it cannot reach your server archive. If the original website is down but your content service remains reachable, stored server chapters are readable.

Use `GET /v1/archive/follows` to see scheduled and last-check status, `POST /v1/archive/activity/{source}/{book}` to prioritize a recently read book, and `DELETE /v1/archive/follows/{source}/{book}` to stop checking without deleting the archive. A book deletion clears its follow status. The schedule is **only active while the Python service runs**, is not a cloud task, and should be used with one worker process against its SQLite archive directory.

## Search capabilities and publication status (v0.12)

Book metadata includes `publicationStatus`: `completed`, `ongoing`, or `unknown`. NovelPing uses its explicit status metadata/badges; WTR-LAB's verified status codes are `0` = ongoing and `1` = completed. Missing or unsupported values remain unknown. Catalog entries can supply `publicationStatus`, and HTML adapters can configure optional `publicationStatus` and `resultPublicationStatus` selectors.

Status is saved in archive metadata and supplied while the source is offline. Existing archives migrate to unknown and acquire status through their next source check. `GET /v1/books/{source}/{id}?refresh=true` explicitly refreshes metadata from an enabled adapter and updates the saved metadata without re-downloading chapters. Normal reads still use the archive immediately. Source failures retain the previous saved details. Search and home responses reuse known archived status when a live card omits it.

`GET /v1/health` includes `sourceCapabilities`, keyed by source ID, with `genreSearch` booleans. NovelPing and WTR-LAB do not supply search-result genres, so this capability is false rather than advertising a filter that drops valid matches. Publication status is separate from Android's user-controlled reading/library status.

## Re-archive saved chapters (v0.11)

In the service console's **Saved novels** section, use **Re-archive saved chapters** on a novel, or select up to 50 novels and choose **Re-archive selected**. The service fetches each already-saved chapter again through its current source adapter. This physically replaces old flattened text with fresh extraction, including the removal of hidden website promotional spans whose wording may not be recognized by plain-text filters.

- A job snapshots the chapter IDs that have saved text when it is queued. Progress counts successfully replaced chapters; newly discovered, unsaved chapters remain the responsibility of normal archiving/follow checks.
- Existing text stays readable until its replacement is fetched, validated, and committed. Failed or oversized responses keep the previous copy. The storage quota accounts for replacement bytes rather than charging for two complete copies.
- Pause, resume, retry, and cancel are available under **Recent jobs**. Per-chapter checkpoints survive retries and service restarts. An in-flight response cannot overwrite text after its job is paused or canceled.
- Retries continue unfinished targets. A new re-archive action starts a fresh pass. Retrying a completed re-archive job through the API also resets its checkpoints for another pass.
- If an archived chapter is no longer downloadable upstream, other available targets are refreshed and the job reports the remaining chapters as failed, retaining their saved copies.
- Source request limits and access checks apply. The source adapter must be enabled. Existing follow/unfollow settings are preserved.

`POST /v1/archive/{source}/{book}/refresh` queues one novel. `POST /v1/archive/refresh` accepts the same `{"items":[{"source":"novelping","bookId":"..."}]}` selection format as the normal batch endpoint; queueing is atomic and deduplicated. Active re-archive jobs are reused; an active normal archive job must finish or be canceled first. Job responses include `refresh: true` for these jobs. Private-library originals use their own upload storage and are not source re-archive targets.

This operation updates the **server archive**. To replace phone-offline copies too, remove those downloads under **Manage downloads** and download them again after the server job completes. Reopen any currently displayed chapter to load the refreshed text.

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
| `GET /v1/home?source=...` | Bounded home shelves: popular, completed, fresh, new, and hot; optional source selects one provider before applying shelf limits |
| `DELETE /v1/home/cache` | Request an immediate home-shelf refresh and wait for it to finish |
| `GET /v1/recent?source=...` | Recently updated books where known; optional source selects one provider |
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
| `GET /v1/archive/config` | View persisted archive concurrency and follow intervals |
| `PUT /v1/archive/config` | Validate, save, and immediately apply safe operational settings |
| `GET /v1/archive/books` | List books saved in the server archive |
| `GET /v1/archive/books/{source}/{id}` | Archived-chapter count and storage size |
| `DELETE /v1/archive/books/{source}/{id}` | Permanently delete an archived book |

Every `/v1` endpoint requires `Authorization: Bearer <token>` when `QUIETLEAF_API_TOKEN` is configured. The admin console may instead use its signed, HTTP-only, same-site session cookie; browser write requests also require a custom confirmation header to prevent cross-site form submissions. The public `/admin` shell contains no service data and `/healthz` returns only process status. No accounts, ads or commercial services are needed by this backend; the private archive uses local SQLite. Access tokens are stored in the Android app's private preferences; they are not hardware-backed encrypted. Use a low-privilege token dedicated to this service.

## Tests

```bash
python -m pytest -q
```

Automated tests cover persistent archiving, source-offline fallback, restart and retry recovery, deduplication, storage limits, source removal, simulated NovelPing archiving, complete NovelPing chapter archives, dashboard security, per-source failure isolation, API authentication, text access, metadata-only chapters, book/chapter ordering, a fixture-driven HTML extraction adapter, cross-origin link blocking, robots restrictions, path traversal and authorization requirement.

## Current limitations

The service has no built-in automatic domain verification, multi-user management or HTTPS certificates. It does have periodic checks for specifically followed novels, but no broad site crawl. It now persists a user-selected chapter archive but has a scheduled new-chapter checker for followed books or update notifications. Do not use it as a publicly open bulk mirror. Download permission is configured by the catalog/source owner, not verified by a licensing service.
