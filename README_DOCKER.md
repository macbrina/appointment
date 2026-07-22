# frontdesk_watch — Docker instructions

This repository contains a watcher that scrapes Frontdesk reservation pages and sends Telegram alerts.

Quickstart (build & run):

1. Create a `.env` in the repo root with your Telegram credentials:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

2. Use a named Docker volume for persistence (recommended)

The included `docker-compose.yml` now creates a named volume `watcher_state` and mounts
it at `/usr/src/app/state` inside the container. Docker (and most hosts) will create the
volume automatically, so you don't need to pre-create host files.

Build & run with docker-compose:

```bash
docker-compose up --build
```

If you prefer host file mounts for local testing, you can still create files and mount them:

```bash
echo '[]' > seen_slots.json
touch last_fingerprint.txt
docker run --rm --env-file .env \
  -v "$PWD/seen_slots.json":/usr/src/app/seen_slots.json \
  -v "$PWD/last_fingerprint.txt":/usr/src/app/last_fingerprint.txt \
  frontdesk-watch:latest
```

Notes and recommendations:

- The image is based on the official Playwright Python image which already includes browser binaries.
- Ensure outbound network access to `reservation.frontdesksuite.com` and `api.telegram.org` from your host/container.
- The watcher writes `seen_slots.json` and `last_fingerprint.txt` to the mounted files so availability is persisted across restarts.
- If you prefer a named Docker volume instead of host files, the `docker-compose.yml` example already uses a named volume `watcher_state`.
- Keep `interval_seconds` and `jitter_seconds` conservative to avoid overloading the target site.
- For production heartbeat notifications, the watcher sends a startup message on first launch and then a daily heartbeat. You can override the interval with `FRONTDESK_HEARTBEAT_INTERVAL_SECONDS` (in seconds) if you want a different cadence.
