# HJW Downloader — 批量视频下载器

基于 Flask 的 Web 应用，用于从 hjw01.com 批量下载视频。使用无头 Chrome（Selenium）提取 m3u8 流地址，再通过 FFmpeg 下载。后台爬虫（`reptile.py`）每 6 小时自动扫描站点，发现新视频。项目已容器化，适合 NAS 部署。

## 快速开始

**本地开发：**
```bash
pip install -r requirements.txt
python app.py          # 启动 Flask + SocketIO，监听 :5000，同时启动 reptile.py 后台线程
```

**`.env` 文件**（可选，用于 Telegram 通知和配置覆盖）：
```
TG_TOKEN=123:abc
TG_CHAT_ID=-456
REDIS_HOST=redis_db        # 默认值
CHROMEDRIVER_MIRROR=https://npmmirror.com/mirrors/chromedriver  # 国内可选
```

**Docker：**
```bash
docker-compose up -d   # 启动 app + Redis；Telegram 通知需要 .env 中配置 TG_TOKEN/TG_CHAT_ID
```

**CI/CD：** 推送到 `main`/`master` 分支会触发 `.github/workflows/docker-build.yml`，构建并推送镜像到 `ghcr.io/<owner>/hjdownload`。

## 架构

### 并发模型

Flask-SocketIO 使用 `threading` 模式运行 — 无需 eventlet/gevent monkey-patch。每个下载 Worker 运行在原生 `threading.Thread` 中。

- **线程模型：** 每个下载任务运行在独立的 `threading.Thread` 守护线程中。两个 `threading.Semaphore(3)` 分别限制并发数：`m3u8_semaphore` 控制 Selenium 解析任务，`mp4_semaphore` 控制直链下载任务。
- **实时更新：** `update_and_broadcast()` 将任务状态推送到 Redis → SocketIO WebSocket 广播 → SQLite（仅在关键状态转换时写库，避免锁竞争）。

### 数据流

1. 用户在首页 `/` 提交 `标题:/链接:/作者:` 格式的文本 — 通过正则解析字段（字段间用换行或 `|` 分隔，链接必须以 `http` 开头）
2. 任务被写入 SQLite（`tasks.db`）和 Redis，然后每个任务启动一个守护线程。任务 ID 同时存储在内存 `session_db` 字典中，作为 Redis 丢失映射时的回退
3. `download_worker()` 获取信号量 → `m3u8` 类型：Selenium 提取 m3u8 地址；`m3u8_direct`/`mp4` 类型：直接使用传入的 URL → FFmpeg 下载，解析 `time=` 行获取进度 → `ffprobe` 检测时长 ≥ 300 秒，丢弃过短片段
4. Redis 键结构：
   - `task:{id}` — Hash，单任务状态
   - `token:{token}:tasks` — Set，一批任务的所有 ID
   - `all_tokens` — Set，所有批次 token
   - `token_time:{token}` — 批次创建时间

### 数据库结构

两个 SQLite 数据库位于 `/app/data/`（均使用 WAL 日志模式）：

- **`tasks.db`**（app.py）：`tasks` 表 — `task_id TEXT PK, title, author, status, progress, done INTEGER, time_added`
- **`reptile.db`**（app.py + reptile.py 共享）：`new_data` 和 `old_data` 表 — `id INTEGER PK AUTOINCREMENT, title, url, author, time_added`

### 路由总览

| 路由 | 功能 |
|---|---|
| `GET/POST /` | 首页；POST 解析视频列表（`video_type` 表单参数：`m3u8`/`m3u8_direct`/`mp4`）并启动下载 |
| `/status/<token>` | 批次实时进度页（WebSocket） |
| `/tokens` | 所有活跃批次 token 概览 |
| `/history/manager_999` | 下载历史（token 保护） |
| `/reptile/new` | 未查看的爬取视频 |
| `/reptile/old` | 全部爬取视频（分页、可搜索） |
| `/api/reptile/export` | 导出 new_data 为 TXT 并移至 old_data |
| `/api/reptile/export_selected` | 导出勾选行到 TXT |
| `/api/reptile/download_selected` | POST — 对勾选行启动下载 |
| `/api/reptile/oneclick` | POST — 一键下载所有 new_data |

### reptile.py 后台爬虫

爬取 `https://www.hjw01.com` 的分页列表，按 URL 与 `new_data` 和 `old_data` 去重，有新内容时通过 Telegram 通知（需配置 `TG_TOKEN`/`TG_CHAT_ID`）。每 6 小时循环一次（`CHECK_INTERVAL = 21600`）。通过附加 `?t=` 时间戳应对 CDN 缓存。

### 配置常量（app.py）

| 常量 | 值 | 说明 |
|---|---|---|
| `MIN_DURATION` | 300 | 短于 5 分钟的视频下载后丢弃 |
| `MAX_THREADS` | 3 | 每种类型（m3u8/mp4）的最大并发下载数 |
| `HISTORY_TOKEN` | `"manager_999"` | `/history/` 页面的访问 token |
| `REDIS_HOST` | 环境变量，默认 `redis_db` | Compose 服务名 |

### Docker 部署

Dockerfile 从 Google 官方 .deb 安装 Chrome、FFmpeg，Python 依赖通过清华镜像安装。`docker-compose.yml` 定义两个服务：`app`（Flask 应用，端口 5000）和 `redis_db`（Redis 7 Alpine）。卷映射：`/downloads` → 宿主机视频存储，`./data` → 持久化数据库。

## 依赖项

- flask / flask-socketio — Web 框架 + WebSocket
- selenium / webdriver-manager — 浏览器自动化
- redis — 任务状态缓存
- requests / beautifulsoup4 — HTTP 和 HTML 解析
- ffmpeg / ffprobe — 视频下载和时长检测
- Google Chrome — 无头浏览器
