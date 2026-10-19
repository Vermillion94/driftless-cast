# Operations runbook

## Hosting

- **Fly.io** app `driftless-cast`, region `ord`. One `shared-cpu-1x` / 256 MB machine, **always on** (`min_machines_running = 1`). Cost is about $2/month plus about $0.15/month for the 1 GB volume.
- **Storage:** SQLite at `/data/driftless_cast.db` on the `dc_data` volume. Fly takes daily volume snapshots (default retention 5 days).
  - Trip reports (`catch_log`) are the only data that can't be regenerated. Everything else rebuilds from upstream in about 5 minutes.
- **Deploys:** every push to `main` triggers `.github/workflows/fly-deploy.yml`. `main` is protected and needs the "Synthetic acceptance suite" and "Hindcast against USGS + Mohseni" checks.

## Forecast freshness

- An in-process loop rebuilds every 60 min (±2 min jitter), plus once at startup.
- `GET /status` reports `newest_computed_at` and `is_stale`, which becomes true after 90 min.
- `POST /refresh` triggers a rebuild in the background. Public callers are limited to one every 20 minutes. Operators can bypass the limit with an `X-Admin-Token` header matching the `DC_ADMIN_TOKEN` secret:
  - Set the secret once: `fly secrets set DC_ADMIN_TOKEN=…`
  - Then: `curl -X POST -H "X-Admin-Token: …" https://driftless-cast.fly.dev/refresh`

## Health

- The Fly check is `GET /healthz` (async, no dependencies), every 30 s with a 10 s timeout.
- During a rebuild the builder yields 0.5 s between reaches (`DC_BUILD_YIELD_SECONDS`) so requests stay responsive on the single shared CPU.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DC_DB_PATH` | `./driftless_cast.db` | SQLite location (`/data/...` on Fly) |
| `SERVE_STATIC` | `1` | Serve `web/` from the API process |
| `DC_ADMIN_TOKEN` | unset | Operator bypass for the `/refresh` cooldown |
| `DC_CONTACT` | site URL | Contact in the upstream User-Agent (required by NWS) |
| `DC_REBUILD_INTERVAL_SECONDS` | `3600` | Rebuild cadence |
| `DC_BUILD_YIELD_SECONDS` | `0.5` | Pause between reaches during a rebuild |

## Security posture

- No accounts, no secrets in the client, no third-party trackers.
- Security headers on every response: a strict CSP listing only the hosts used, HSTS, nosniff, frame-deny, referrer and permissions policies. Leaflet is SRI-pinned.
- Write endpoints are limited:
  - `POST /catch-log`: per-client rate limit (12/hour), bounded field lengths, date validation.
  - `POST /refresh`: 20-minute cooldown.
- Rate limiting is in-process, which fits the single-machine setup. Move it to a shared store if the app ever runs more than one machine.

## Common tasks

| Task | Command |
|---|---|
| Logs | `fly logs -a driftless-cast` |
| Shell | `fly ssh console -a driftless-cast` |
| Machine status | `fly status -a driftless-cast` |
| Back up trip reports | `fly ssh console -C "sqlite3 /data/driftless_cast.db .dump catch_log" > catch_log.sql` |
| Re-fit water temperature | `python -m src.scripts.refit_mohseni_driftless`, then `python -m src.scripts.backtest --skip flow` |
| UI smoke test | `python scripts/ui_smoke.py http://127.0.0.1:8000/` |

## Incident history

- **2026-06-20:** PR #38 (response compression and geometry trimming) caused an outage and was reverted (`e15ff5d`). The revert landed on `main` with PR #40 on 2026-10-19.
- **2026-08-19 → 2026-10-19:** GitHub auto-disabled the keep-warm cron for repo inactivity, and forecasts went up to 9 days stale between visits. Fixed by making the machine always on and deleting the cron.
