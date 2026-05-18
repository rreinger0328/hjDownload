# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

HJW Downloader is a Flask web app that batch-downloads videos from hjw01.com. It uses headless Chrome (Selenium) to extract m3u8 stream URLs, then downloads with FFmpeg. A background scraper (`reptile.py`) polls the site every 6 hours for new videos. The app is containerized for NAS deployment.

## Commands

**Local development (Windows):**
```bash
pip install flask flask-socketio selenium webdriver-manager redis requests beautifulsoup4
python app.py          # starts Flask + SocketIO on :5000, also launches reptile.py background thread
```

**`.env` file** (optional, for Telegram notifications and config overrides):
```
TG_TOKEN=123:abc
TG_CHAT_ID=-456
REDIS_HOST=redis_db        # defaults to "redis_db"
MAX_THREADS=2              # 并发下载线程数，默认 2（低内存），升级后可选 3-6
FFMPEG_THREADS=1           # FFmpeg 解码线程数，默认 1
CHROMEDRIVER_MIRROR=https://npmmirror.com/mirrors/chromedriver  # optional, for China
```

**Docker:**
```bash
docker-compose up -d   # starts app + Redis; requires .env with TG_TOKEN/TG_CHAT_ID for Telegram notifications
```

**CI/CD:** Push to `main`/`master` triggers `.github/workflows/docker-build.yml`, which builds and pushes `ghcr.io/<owner>/hjdownload` to GitHub Container Registry.

## Architecture

### Concurrency model

Flask-SocketIO runs in `threading` mode — no eventlet/gevent monkey-patching needed. Each download worker runs in a native `threading.Thread`.

- **Threading:** Each download runs in a daemon `threading.Thread`. Two separate `threading.Semaphore(3)` objects limit concurrency: `m3u8_semaphore` for Selenium-parsed downloads, `mp4_semaphore` for direct downloads.
- **Real-time updates:** `update_and_broadcast()` pushes task state to Redis → SocketIO WebSocket broadcast → SQLite (only on key status transitions to avoid write contention).

### Data flow

1. User submits `标题:/链接:/作者:` formatted text on `/` — input is parsed by regex (fields separated by newlines or `|`, URLs must start with `http`)
2. Tasks are inserted into SQLite (`tasks.db`) and Redis, then a daemon thread is spawned per task. Task IDs are also stored in the in-memory `session_db` dict as a fallback if Redis loses the token→tasks mapping.
3. `download_worker()` acquires semaphore → for `m3u8` type: Selenium extracts m3u8; for `m3u8_direct`/`mp4`: uses the URL directly → FFmpeg downloads, parsing `time=` lines for progress → `ffprobe` checks duration ≥ 300s, discarding short clips
4. Redis keys: `task:{id}` (hash, per-task state), `token:{token}:tasks` (set, task IDs in a batch), `all_tokens` (set of all batch tokens), `token_time:{token}` (creation timestamp)

### Database schema

Two SQLite databases in `/app/data/` (both use WAL journal mode):

- **`tasks.db`** (app.py): `tasks` table — `task_id TEXT PK, title, author, status, progress, done INTEGER, time_added`
- **`reptile.db`** (shared by app.py + reptile.py): `new_data` and `old_data` tables — `id INTEGER PK AUTOINCREMENT, title, url, author, time_added`

### Key routes

| Route | Purpose |
|---|---|
| `GET/POST /` | Main page; POST parses video list (`video_type` form param: `m3u8`, `m3u8_direct`, or `mp4`) and starts downloads |
| `/status/<token>` | Real-time progress page (WebSocket) for a batch |
| `/tokens` | Overview of all active batch tokens |
| `/history/manager_999` | Download history (token-protected) |
| `/reptile/new` | Unviewed scraped videos |
| `/reptile/old` | All scraped videos (paginated, searchable) |
| `/api/reptile/export` | Export new_data to TXT and move to old_data |
| `/api/reptile/export_selected` | Export checked rows to TXT |
| `/api/reptile/download_selected` | POST — start downloads for checked rows |
| `/api/reptile/oneclick` | POST — download all new_data at once |

### reptile.py background scraper

Scrapes `https://www.hjw01.com` paginated listings, deduplicates by URL against both `new_data` and `old_data`, sends new finds to Telegram if `TG_TOKEN`/`TG_CHAT_ID` are configured. Runs on a 6-hour loop (`CHECK_INTERVAL = 21600`). Handles CDN caching by appending `?t=` timestamps.

### Docker

The Dockerfile installs Chrome from Google's official `.deb`, FFmpeg, and Python deps via Tsinghua mirror. `docker-compose.yml` defines two services: `app` (the Flask app, port 5000) and `redis_db` (Redis 7 Alpine). Volumes map `/downloads` to the host's video storage and `./data` for persistent DBs.

### Config constants (app.py)

- `MIN_DURATION = 300` — videos shorter than 5 minutes are discarded after download
- `MAX_THREADS` — concurrent downloads per type (m3u8/mp4), default 2, configurable via .env
- `FFMPEG_THREADS` — FFmpeg decode threads, default 1, configurable via .env
- `MIN_DURATION = 300` — videos shorter than 5 minutes are discarded
- `HISTORY_TOKEN = "manager_999"` — access token for `/history/`
- `REDIS_HOST` — set via env var, defaults to `redis_db` (Compose service name)
