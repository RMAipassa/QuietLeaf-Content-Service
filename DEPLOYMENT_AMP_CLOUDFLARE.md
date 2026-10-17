# AMP Python App Runner and Cloudflare Tunnel

This deployment runs QuietLeaf natively in CubeCoders AMP's **Python App Runner**. It does not require Docker or a Generic instance.

## 1. Prerequisites

- AMP 2.5.0.8 or newer with the Python App Runner template.
- Python 3.11 or newer and `venv` installed for all users on the AMP host.
- Git and outbound HTTPS access if AMP will clone the repository.
- `cloudflared` on the AMP host for public HTTPS access.
- A private DNS hostname, such as `quietleaf.example.com`.

Keep this service single-instance and single-worker. Archive state is stored locally and is not designed for multiple concurrent processes.

## 2. Create the instance

In AMP's **Create Instance** page:

1. Select **Python App Runner** as the application.
2. Leave Docker/containerization disabled so the instance runs natively.
3. Assign the application port you want QuietLeaf to use. `7777` is the template default.
4. Create and start the instance once so AMP initializes its directories, then stop it before configuration.

If AMP offers sleep settings for the instance, disable them. QuietLeaf must keep running to process archive jobs and automatic checks.

## 3. Configure Python App Runner

Open the instance's **Configuration** page and set these fields:

| AMP field | Value |
| --- | --- |
| App Download Type | `Git repo` |
| App Download Source | `https://github.com/RMAipassa/QuietLeaf-Content-Service.git` |
| Git Repo Branch | `main` |
| Git Repo Username | `RMAipassa` |
| Git Repo Password/Token | A fine-grained GitHub token with read-only **Contents** access to this repository |
| Python Version | `3.12` or another installed version from 3.11 onward |
| Python Packages Install Method | `None` |
| Run App Setup Command | Enabled |
| App Setup Command | `python -m pip install -e .` |
| App Run Mode | `Python module` |
| App Module Name | `uvicorn` |
| App Subdirectory | Leave empty |
| App Command Line Arguments | `quietleaf_service.app:app --host 127.0.0.1 --port {{$ServerPort}} --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1` |

The double braces in `{{$ServerPort}}` are required AMP syntax. Do not replace them with a fixed port in the command.

Save the configuration, then use AMP's **Update** action. Python App Runner will clone the repository, create its `venv`, and run the setup command inside that environment.

GitHub no longer accepts account passwords for Git HTTPS access. Use a dedicated fine-grained token, restrict it to this repository, grant only read-only Contents access, and enter it only in AMP's password field. The Python App Runner update process uses the credential in the Git remote URL, so access to the AMP instance and its files must be restricted.

### Manual upload alternative

To avoid storing a GitHub token in AMP:

1. Set **App Download Type** to `None`.
2. Upload the repository contents into the Python App Runner's root directory with AMP's File Manager.
3. Keep the same Python, setup, module, and command-line settings.
4. Run **Update** to create the virtual environment and install the app.

Future releases must then be uploaded manually before running **Update** again.

## 4. Create `.env`

Use AMP's File Manager to create `.env` in the app's root working directory, next to `pyproject.toml`. Start from `amp.env.example`:

```dotenv
QUIETLEAF_API_TOKEN=replace-with-a-long-random-secret
QUIETLEAF_ENABLE_NOVELPING=0
QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=0
QUIETLEAF_NOVELPING_DELAY_SECONDS=1.0
QUIETLEAF_AUTO_ARCHIVE_MAX_CHAPTERS=0
```

Generate the token outside AMP, for example:

```bash
openssl rand -hex 32
```

QuietLeaf loads this file directly from the Python App Runner working directory. Normal process environment variables take precedence over values in `.env`.

Only enable NovelPing after confirming you are authorized to access and automate that source:

```dotenv
QUIETLEAF_ENABLE_NOVELPING=1
QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED=1
```

Keep NovelPing's delay at `1.0` seconds or higher. Set `QUIETLEAF_AUTO_ARCHIVE_MAX_CHAPTERS` to a positive limit only if new follows should automatically start bounded archive jobs.

The default catalog and archive paths are inside the app root. Both `archive/` and `.env` are ignored by Git, so AMP Git updates do not overwrite them. Back up `archive/` because it contains generated books, job state, and follow state.

## 5. Start and verify

Start the instance in AMP and inspect the console. The effective process should be equivalent to:

```bash
venv/bin/python3 -u -m uvicorn quietleaf_service.app:app \
  --host 127.0.0.1 \
  --port 7777 \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips 127.0.0.1
```

Verify locally on the AMP host:

```bash
curl http://127.0.0.1:7777/healthz
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:7777/v1/health
```

Expected responses include:

```json
{"status":"ok"}
```

and an authenticated service status from `/v1/health`.

If startup fails with a missing package, run AMP's **Update** action again and confirm the setup command completed successfully. Do not add a second Uvicorn worker to work around startup issues.

## 6. Configure Cloudflare Tunnel

Keep Uvicorn bound to `127.0.0.1`; Cloudflare Tunnel provides the public HTTPS endpoint without opening the AMP application port to the internet.

Install and authenticate `cloudflared` on the AMP host, create a tunnel, and route the hostname:

```bash
cloudflared tunnel login
cloudflared tunnel create quietleaf
cloudflared tunnel route dns quietleaf quietleaf.example.com
```

Create the Cloudflare configuration file, commonly `/etc/cloudflared/config.yml`:

```yaml
tunnel: YOUR_TUNNEL_UUID
credentials-file: /etc/cloudflared/YOUR_TUNNEL_UUID.json

ingress:
  - hostname: quietleaf.example.com
    service: http://127.0.0.1:7777
  - service: http_status:404
```

Install and start the tunnel as a host service:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

If the AMP instance uses a port other than `7777`, use that port in the tunnel and local verification commands. Keep Cloudflare SSL/TLS mode at **Full (strict)**.

Verify externally:

```bash
curl https://quietleaf.example.com/healthz
curl -H "Authorization: Bearer YOUR_TOKEN" https://quietleaf.example.com/v1/health
```

## 7. Dashboard and Android

Open the dashboard at:

```text
https://quietleaf.example.com/admin
```

Enter the same API token from `.env`. The browser stores it only in the current tab's session storage.

In the Android app, configure:

```text
Server URL: https://quietleaf.example.com
API token:  the same QUIETLEAF_API_TOKEN value
```

Use HTTPS for Android. Do not expose the Uvicorn port directly or send the Bearer token over plain HTTP.

## 8. Updates and operations

For a Git-backed instance:

1. Stop QuietLeaf in AMP.
2. Back up `archive/`.
3. Run AMP's **Update** action to pull `main`, recreate the virtual environment, and reinstall the package.
4. Start the instance and repeat the local and external health checks.

Operational rules:

- Run exactly one QuietLeaf instance and one Uvicorn worker against an archive directory.
- Keep `.env`, Git credentials, and archive backups private.
- Rotate both the GitHub token and `QUIETLEAF_API_TOKEN` if either is exposed.
- Delete completed jobs from the dashboard when their EPUB files are no longer needed.
- Use the dashboard's stop action before restarting an active automatic archive check.
- Review AMP, Uvicorn, and Cloudflare logs after updates or tunnel changes.

The repository's `install-amp.sh` and `amp-start.sh` remain available for a manual Linux deployment, but Python App Runner should use its built-in virtual environment and module settings above.
