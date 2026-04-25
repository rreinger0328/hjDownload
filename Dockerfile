# 1. 使用 Python 3.9 的官方 slim（精简）版作为基础镜像。
# slim 版去掉了不必要的工具，能大幅减小镜像体积，适合生产环境。
FROM python:3.9-slim

# 2. 设置系统环境变量：
# DEBIAN_FRONTEND=noninteractive: 在安装软件时自动确认所有提示，防止构建卡死在地理位置或时区确认上。
# PYTHONUNBUFFERED=1: 强制 Python 不使用缓存直接输出日志，这样你能在飞牛 NAS 的 UI 面板上实时看到下载进度。
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# 3. 安装 Linux 系统级的基础依赖工具：
# ffmpeg: 核心工具，用于下载 m3u8 并合并视频流。
# curl, gnupg, ca-certificates: 用于后续网络请求、密钥验证和安全证书管理。
# --no-install-recommends: 不安装建议的非必要插件，保持系统纯净。
# rm -rf /var/lib/apt/lists/*: 安装完立即删除本地软件包索引，显著减小镜像体积。
RUN apt-get update && apt-get install -y \
    ffmpeg curl gnupg ca-certificates --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# 4. 设置容器内的默认工作目录为 /app。
# 之后所有的 COPY 和 CMD 指令都会在这个目录下执行。
WORKDIR /app

# 5. 利用本地提供的 .deb 文件安装 Google Chrome（用于 Selenium 网页解析）：
# COPY: 将项目目录下的 Chrome 安装包复制到容器的 /tmp 临时目录。
COPY google-chrome-stable_current_amd64.deb /tmp/chrome.deb

# 6. 执行 Chrome 安装逻辑：
# apt-get install -y /tmp/chrome.deb: 尝试安装本地 deb 包。
# || apt-get install -fy: 如果因为缺少系统依赖报错，自动从官方源下载并修复缺失的底层库（如 libnss3 等）。
# rm /tmp/chrome.deb: 安装完后立即删除安装包，节省空间。
RUN apt-get update && \
    apt-get install -y /tmp/chrome.deb || apt-get install -fy && \
    rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

# 7. 将当前电脑/NAS 项目目录下的所有文件（app.py, templates 文件夹等）复制到容器的 /app 目录。
COPY . .

# 8. 安装 Python 项目依赖库：
# -i https://pypi.tuna.tsinghua.edu.cn/simple: 使用清华大学镜像源，大幅提升国内下载速度。
# flask, flask-socketio: 网页框架及 WebSocket 实时通信支持。
# eventlet: 高性能异步并发库，是 WebSocket 正常运行的底层支柱。
# selenium, webdriver-manager: 用于模拟浏览器操作，动态抓取视频的 m3u8 地址。
# redis: 引入 Redis 驱动，用于在高并发下载时作为进度缓存，解决 SQLite 锁死问题。
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    flask flask-socketio eventlet selenium webdriver-manager redis

# 9. 声明容器运行时监听的端口号。
# 对应 docker-compose.yml 里的 5000:5000。
EXPOSE 5000

# 10. 容器启动时执行的最终命令。
# 运行 app.py 脚本启动整个下载器后端。
CMD ["python", "app.py"]