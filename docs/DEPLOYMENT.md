# Deployment guide

Driftless Cast runs as a single FastAPI process serving both the API and the
static frontend. SQLite is the only datastore; every other source is a public
no-key API hit live. That makes hosting cheap and simple — anywhere that runs
a Docker container with a small persistent volume works.

This guide gives one fully-worked recommended path (Fly.io free tier) plus
shorter notes for three alternatives.

---

## Recommended: Fly.io (free tier, ~5 minute setup)

**Why:** generous free tier (3 small VMs), persistent volumes, region near the
Driftless (Chicago `ord`), Dockerfile-native deploys, no payment card to start.

### One-time setup

```bash
# 1) Install flyctl
brew install flyctl                 # macOS
# OR: scoop install flyctl          # Windows
# OR: curl -L https://fly.io/install.sh | sh

# 2) Sign up / log in
fly auth signup                     # browser opens
# OR: fly auth login

# 3) Create the app from the included fly.toml
#    (--copy-config keeps OUR config; without it Fly will overwrite)
fly launch --no-deploy --copy-config

# 4) Create a 1GB persistent volume for the SQLite DB.
#    The volume must be in the same region as the app (we set ord above).
fly volumes create dc_data --size 1 --region ord

# 5) First deploy
fly deploy
```

After step 5 the app is live at `https://<your-app>.fly.dev`. The first
forecast build takes ~30-60s (USGS + NWS + Open-Meteo round trips for 21
reaches); subsequent loads are instant.

### Redeploys

```bash
fly deploy                          # ships current code
fly logs                            # tail logs
fly ssh console                     # SSH in for debugging
```

### Cost

Free tier covers one always-on machine of this size (256MB shared-cpu-1x).
The volume costs $0.15/GB/month — about $0.15/mo for our 1GB. Effectively
**$0/month** for personal use; ~$2/mo if you scale up.

### Persistent state

`fly.toml` mounts `/app/data` to the `dc_data` volume and sets
`DC_DB_PATH=/app/data/driftless_cast.db` so the SQLite file lives inside the
mount. Catch-log entries, cached forecast predictions, and Mohseni
calibration outputs all survive redeploys.

The seed JSON files in `data/seed/` ARE shipped with each image (they're
source, not state). Updates to seed data take effect on next deploy.

### Stop / start

```bash
fly machines list
fly machines stop <id>              # to pause
fly machines start <id>             # to resume
```

`auto_stop_machines = "stop"` in `fly.toml` lets the machine sleep after
inactivity and wake on the first request (cold-start ~3-5s). Set to `"off"`
in `fly.toml` if you want it always warm.

---

## Alternative: Render.com (browser-only deploy)

Good when you want zero CLI. Free tier sleeps after 15 min of inactivity.

1. Push this repo to GitHub.
2. Visit <https://render.com/new> → Web Service → connect your GitHub repo.
3. Settings:
   - Runtime: Docker (auto-detected from Dockerfile)
   - Plan: Free
   - Health Check Path: `/openapi.json`
   - Add a Disk: 1 GB mounted at `/app/data`
   - Add env var: `DC_DB_PATH=/app/data/driftless_cast.db`
4. Click "Deploy". First deploy takes ~5 min.

Cold-start on Render's free tier is ~30s after sleeping (worse than Fly).

---

## Alternative: a $6/month DigitalOcean droplet

Good when you want full control or already have a droplet.

```bash
# On the droplet (Ubuntu 22.04+):
sudo apt update && sudo apt install -y docker.io
git clone <your-repo-url> /srv/driftless-cast
cd /srv/driftless-cast
docker build -t driftless-cast .
docker run -d --name dc \
    --restart unless-stopped \
    -p 80:8000 \
    -v /srv/dc-data:/app/data \
    -e DC_DB_PATH=/app/data/driftless_cast.db \
    driftless-cast

# Optional: front with Caddy for free HTTPS
sudo apt install -y caddy
echo 'driftless.example.com {
    reverse_proxy 127.0.0.1:80
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

---

## Alternative: a Raspberry Pi or home server

Same Docker invocation as the DO droplet works on a Pi 4 or better. SQLite
on a flash card has the usual write-amplification caveats; if you're going
to run for years, mount `/srv/dc-data` on an SSD or a USB stick rated for
sustained writes.

The original project plan called this out as a target — the app is
specifically built to run cheap.

---

## Local development vs hosted production

| | Local dev | Production |
|---|---|---|
| API | `uvicorn src.api.main:app --reload --port 8000` | Same image, no `--reload` |
| Frontend | `python serve_static.py` on port 8080 | Mounted at `/` by FastAPI |
| `API_BASE` in `web/map.js` | `http://localhost:8000` (auto-detected when port 8080) | `""` same-origin |
| DB path | `./driftless_cast.db` | `/app/data/driftless_cast.db` (volume) |
| Static serving | nginx via docker-compose, OR Python's `http.server` | FastAPI `StaticFiles` mount |

The single-port production path also makes it impossible to hit a CORS bug
in the browser, since API and UI share an origin.

---

## Periodic forecast rebuild

Currently the forecast is rebuilt **on process start** and on demand via
`POST /refresh`. There is **no scheduled rebuild** wired up yet (APScheduler
is in the dependencies but no job is registered). For a hosted instance you
have two reasonable options:

1. **External cron-like trigger.** Easiest. From any machine:
   ```bash
   curl -X POST https://<your-app>.fly.dev/refresh
   ```
   Run that hourly (or every 3h) via GitHub Actions, a cron job on a Pi, or
   Fly Cron Scheduling.

2. **In-process scheduler.** Add to `src/api/main.py`:
   ```python
   from apscheduler.schedulers.background import BackgroundScheduler
   scheduler = BackgroundScheduler(timezone="UTC")
   scheduler.add_job(_build_forecast_background, "interval", hours=3)
   scheduler.start()
   ```
   Pros: self-contained. Cons: doubles process complexity; if the app sleeps
   (Fly auto-stop) the scheduler doesn't fire until first user request anyway.

External trigger is simpler. Pick that unless you specifically want
self-scheduling.

---

## Validation in CI

`python -m src.scripts.backtest` exits 0/1 based on whether the model still
beats configured thresholds. Wire it to GitHub Actions to gate merges:

```yaml
# .github/workflows/backtest.yml
name: backtest
on: [pull_request]
jobs:
  hindcast:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install poetry==1.8.3 && poetry install --without dev --no-root
      - run: poetry run python -m src.scripts.backtest --days 90
```

This won't run until you wire it up, but the script + thresholds are ready
in `src/scripts/backtest.py`.

---

## Periodic image rebuilds (security)

Container scanners flag CVEs in base images regularly. The pragmatic
mitigation is to rebuild against the latest `python:3.13-slim` tag at least
monthly so upstream Debian patches roll in:

```bash
docker build --no-cache -t driftless-cast .
fly deploy --no-cache
```

If you wired GitHub Actions for backtest above, add a `weekly: true` job
that rebuilds and redeploys on Sunday — that stays current with no
ongoing attention.

---

## Troubleshooting

- **Port 8000 still bound after a process exits** (Windows): an orphaned
  multiprocessing-fork worker can keep the port held. Find it with
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` and
  `Stop-Process` the orphan whose parent process is dead. We hit this twice
  during dev — see the troubleshooting note in `docs/REFERENCES.md`.
- **The forecast is empty after deploy**: the build runs in a background
  thread on startup and takes ~30-60s. Check `fly logs` — you'll see
  `forecast build complete: 21 reaches, ~3270 rows` when it finishes.
- **CORS errors in production**: shouldn't happen with the single-port
  setup, but if you split API and UI onto separate hosts, add the UI's
  origin to `allow_origins` in `src/api/main.py`.
- **Database locked**: SQLite is single-writer. The forecast build holds
  the write lock for ~30s during a full rebuild. Wait, then retry. If you
  see this consistently, switch to a worker that runs the rebuild outside
  the request path.
