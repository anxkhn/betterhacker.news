# BetterHacker

BetterHacker is a read-only reader for Hacker News. It shows the six HN feeds
(top, new, best, show, ask, jobs) with one thumbnail per story, and it refreshes
every five minutes.

The app is a FastAPI and Jinja2 server. A worker script writes the feeds and
thumbnails to local files. The web process only reads those files, so a request
never calls Hacker News or any article site.

## Attribution

BetterHacker is a fork of [olegkhomenko/betterhacker.news](https://github.com/olegkhomenko/betterhacker.news),
the original project by Oleg Khomenko. The code is MIT licensed, and the
copyright notice is in [LICENSE](LICENSE). This fork is maintained at
[anxkhn/betterhacker.news](https://github.com/anxkhn/betterhacker.news).

## Current state

- Refresh: a systemd user timer runs `worker.py` every five minutes. It is
  persistent, so a run missed while the machine was off runs at the next start.
- Storage: the worker writes `current.json`, `thumbnail-cache.json`, and
  `media/<id>.webp` under `data/`. Every write goes to a temporary file that is
  fsynced and then renamed over the target. A failed write leaves the previous
  file in place.
- Thumbnails: each story gets a generated editorial image first. The worker then
  tries to fetch the article's Open Graph image. A successful preview is kept for
  24 hours. The worker makes at most 24 preview attempts per run, oldest attempt
  first. A story with no usable preview keeps its generated image.
- Preview fetches: a URL must be http or https on the default port, without
  credentials. Every DNS answer must be a public address. The connection goes to
  the checked address, so the hostname is not resolved a second time. Each
  redirect is checked again. Responses are capped at 512 KB for HTML and 3 MB for
  images. Each fetch has a deadline.
- No paid APIs. There is no OpenAI or other model call anywhere in the pipeline.
- Per-feed status: the snapshot stores a `feed_generated_at` timestamp for each
  feed. The page shows that feed's refresh time and `/api/status` lists stale feeds.

Tested state on Oct 31, 2026: one worker run produced 196 stories across the six
feeds, with 19 real previews and 177 generated images.

## Requirements

- Python 3.11 or newer. `.python-version` pins 3.11.
- [uv](https://docs.astral.sh/uv/) for dependencies and commands.
- Linux with systemd user services for the deployment units. Linger must be
  enabled for the service user so the units run without a login session.
- `cloudflared` for the public tunnel. It is only needed for public access.

## Local setup

```bash
uv sync
uv run python worker.py                                   # writes data/
uv run uvicorn app.app:app --host 127.0.0.1 --port 8791
```

Open http://127.0.0.1:8791. Until the worker has run once, the page shows
"Waiting for first refresh" and `/healthz` returns 503.

Set `BETTERHACKER_DATA` to use another data directory. Set
`BETTERHACKER_PUBLIC_URL` to change the public URL the app reports. Its default
is `http://localhost:8791`. The deployed web service sets the temporary public URL.

## Tests and checks

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check
uv run ruff format --check
```

The unit tests cover URL and DNS validation, pinned connections, the response
limits, atomic writes, retry rotation, and the health endpoint. They do not use
the network.

Run `uv run ruff format` on files you have changed.

## Deploying with systemd

The units in `deploy/` use the absolute path `/home/hermes/betterhacker.news`.
Change that path in the copied files if the checkout lives somewhere else.

- `betterhacker-web.service`: uvicorn on `127.0.0.1:8791`, with a 384 MB memory
  cap.
- `betterhacker-refresh.service` and `.timer`: runs `worker.py`. The timer uses
  `OnCalendar=*-*-* *:0/5:00` and `Persistent=true`. The service has a 235 second
  hard alarm, and systemd stops it at 270 seconds.
- `betterhacker-named-tunnel.service`: the named Cloudflare tunnel. It reads its
  token from `~/.config/betterhacker/tunnel-token`.
- `betterhacker-tunnel.service`: the interim quick tunnel. It is described in
  the next section.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/betterhacker-web.service deploy/betterhacker-refresh.service \
   deploy/betterhacker-refresh.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now betterhacker-web.service betterhacker-refresh.timer
```

## Public access

### Temporary: quick tunnel

`betterhacker-tunnel.service` runs `cloudflared tunnel --url`. Cloudflare gives
it a random `*.trycloudflare.com` address on every start, and the address can
change after a restart. The current preview is:

https://minister-hawk-involve-supplier.trycloudflare.com

Treat that URL as temporary. Do not share it as a stable address. Do not
Set the current address with `BETTERHACKER_PUBLIC_URL` in the web service.

### Custom domain: blocked

The intended address is `https://betterhacker.anaskhan.me`. It is not deployed.
Creating the named tunnel and DNS record needs a Cloudflare API token that has
access to the anaskhan.me zone. No token is configured on this host yet, so this
step has not run.

`deploy/configure_cloudflare.py` does the setup with that token. It:

- requires the token file to be owner-only (mode 0600 or stricter),
- creates the `betterhacker-azure` named tunnel, or reuses it if it exists,
- sets the tunnel ingress to `http://127.0.0.1:8791`,
- refuses to run if the hostname already has DNS records it did not create,
- adds a proxied CNAME for `betterhacker.anaskhan.me`,
- saves the tunnel token to `~/.config/betterhacker/tunnel-token` with mode 0600.

Run it with a token file you create yourself, outside the repository:

```bash
install -m 600 /dev/null /path/to/cloudflare-api-token
# paste the API token into that file, then:
uv run python deploy/configure_cloudflare.py --token-file /path/to/cloudflare-api-token
```

After the script succeeds, switch from the quick tunnel to the named tunnel:

```bash
cp deploy/betterhacker-named-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now betterhacker-named-tunnel.service
systemctl --user disable --now betterhacker-tunnel.service
```

Confirm the named tunnel is healthy before disabling the quick tunnel. Then
delete the saved API token file. Do not commit the token file or the tunnel
token. Both paths are outside the repository.
Set `BETTERHACKER_PUBLIC_URL=https://betterhacker.anaskhan.me` in the web service,
reload the user systemd manager, and restart `betterhacker-web.service`.

### Named tunnel template

`deploy/cloudflared.yml.example` shows the ingress shape for a credentials-file
tunnel. The current service uses a token instead, so Cloudflare holds the
ingress rules and the local YAML file is not read. The example is kept for
reference. Copy it to a file outside the repository and fill in the tunnel ID
and credentials path if you use that style.

## Security model

The app is read-only. It has no accounts, no login, no write endpoints, and no
stored credentials. It listens on loopback only. Public traffic reaches it
through an outbound-only `cloudflared` tunnel, so the host opens no inbound
ports.

Responses carry a Content-Security-Policy that allows scripts and styles from
`'self'` only, and `frame-ancestors 'none'`. Other headers are `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a restrictive
Permissions-Policy. `/docs` and `/openapi.json` are disabled. The Host header is
checked against an allow list.

This reduces the attack surface, but it does not make the service unhackable.
It is one small process with third-party dependencies, behind a tunnel, serving
text and images that come from third-party sites. Keep dependencies updated,
and review the code before you rely on it for anything sensitive.

## Monitoring

- `GET /healthz` returns 200 when the snapshot and every feed are less than
  15 minutes old, and 503 otherwise.
- `GET /api/status` returns the snapshot time, age in seconds, story counts per
  feed, stale feed names, and thumbnail statistics.
- Logs: `journalctl --user -u betterhacker-refresh.service` for worker runs,
  `journalctl --user -u betterhacker-web.service` for the web app, and
  `journalctl --user -u betterhacker-tunnel.service` for the tunnel.

A stale snapshot usually means the timer is not running. Check
`systemctl --user list-timers betterhacker-refresh.timer` first.

## Repository layout

```text
app/app.py          FastAPI routes, security headers, health endpoints
app/previews.py     URL validation, pinned fetches, previews, generated images
worker.py           Feed refresh, thumbnail refresh, atomic snapshot writes
templates/          Jinja2 page
static/             CSS, client script, icons
deploy/             systemd units, tunnel template, Cloudflare setup script
data/               Runtime output (ignored by git except the sample file)
tests/              unittest suite
```

## Deployment status

The Azure web service and five-minute refresh timer are enabled. The temporary
quick tunnel is active. The custom domain still needs Cloudflare access.
Canonical and Open Graph URLs use `BETTERHACKER_PUBLIC_URL`.
`uv.lock` pins the installed dependencies.
Unused thumbnail files older than one day are removed after a successful refresh.
