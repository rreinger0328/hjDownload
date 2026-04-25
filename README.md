# HJW Downloader

HJW Downloader 是一个基于分布式和实时反馈架构的视频批量下载和管理工具，专为 NAS 与本地批量缓存场景设计。它通过无头浏览器自动抓取视频流并利用 FFmpeg 实时下载，还提供完善的 Web 界面供实时追踪与历史记录管理。

## ✨ 核心特性

- 🚀 **一键批量解析下载**: 支持同时导入多个（标题、链接、作者）组合；
- ☁️ **容器化部署与 NAS 整合**: 非常容易集成到包含 NAS 等外部挂载卷的环境中；
- 🔄 **实时进度监控 (WebSocket + Redis)**: 通过 Redis 实时追踪并发进程（FFmpeg）状况，前端无缝刷新下载动态；
- ⚡ **智能短片过滤**: 使用 `ffprobe` 自动剔除短于 5 分钟的冗余视频或广告片段；
- 📂 **整理归档**: 自动根据“作者”建立文件夹，保持你的媒体库干净整洁；
- ⏳ **下载历史追溯**: 提供系统级的安全管理页面查询历史下载。

---

## 📦 环境与依赖

本项目推荐使用 **Docker Compose** 一键直接部署，并配置了自动推送到 GitHub Container Registry (ghcr.io) 的 CI/CD 流程。

- Docker & Docker Compose
- (若本地运行) Python 3.9+ / Redis / FFmpeg / Google Chrome / Selenium

---

## 🛠 安装与部署 (Docker 推荐)

您只需获取项目中的 `docker-compose.yml` 并修改为你的需求路径后直接拉取容器即可。

### 1. 修改本地映射路径

打开 `docker-compose.yml`，修改映射到本机的下载目录与数据目录：

```yaml
    volumes:
      # 将 `/vol1/1000/video/AVI` 替换为你物理机(NAS)存放视频的路径
      - /vol1/1000/video/AVI:/downloads
      # 映射本地配置和数据库文件目录
      - ./data:/app/data
```

### 2. 启动服务

```bash
docker-compose up -d
```
启动后访问 `http://localhost:5000` 即可看到主页界面。

> **说明：** 默认配置下，服务将自动从 GitHub Container Registry 拉取已构建好的最新镜像 (`ghcr.io/rreinger0328/hjdownload:main`)。 

---

## 📖 使用指南

### 1. 提交任务 (主页)

在主页文本框中输入规定格式的内容。多条记录可以换行或以分隔符隔开。例如：

```text
标题: 测试视频01
链接: https://example.com/video/12345
作者: 用户A

标题: 另一个视频
链接: https://example.com/video/67890
作者: 用户B
```

提交后会自动跳转到对应的批次**实时进度页**。

### 2. 查看历史总任务 

如果您想查看全量系统的下载记录日志，可以通过访问：  
`http://localhost:5000/history/manager_999` 查看所有下载的历史状态与进度（`manager_999` 可以在配置中设定为你独属的 Token）。

---

## 🖥 架构简述

- **前端层**: Flask 原生模板渲染界面 / Flask-SocketIO 接管 Websocket 推送
- **业务层**: `download_worker` 并发队列（由 `threading.Semaphore` 限制数量以保护硬件）。首先由 Selenium 请求解析出真实的 `m3u8` 视频流后移交给底层的 `FFmpeg` 管道。
- **数据层**: SQLite 做长效历史数据落地并利用 WAL 提升并发性能，Redis 做高速缓存及实时状态同步站。