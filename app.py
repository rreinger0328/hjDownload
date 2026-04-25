import eventlet
eventlet.monkey_patch()

import os, re, time, threading, subprocess, uuid, sqlite3, redis
from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET_KEY'] = 'hjw_redis_secure_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 配置 ---
BASE_SAVE_DIR = "/downloads"
DB_PATH = "/app/data/tasks.db"
FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"
MAX_THREADS = 3
MIN_DURATION = 300 # 5分钟
HISTORY_TOKEN = "manager_999"

# Redis 连接 (通过 Compose 里的服务名连接)
r = redis.Redis(host=os.environ.get('REDIS_HOST', 'localhost'), port=6379, decode_responses=True)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.execute('PRAGMA journal_mode=WAL;') # 开启高性能模式
        conn.execute('''CREATE TABLE IF NOT EXISTS tasks 
                     (task_id TEXT PRIMARY KEY, title TEXT, author TEXT, 
                      status TEXT, progress TEXT, done INTEGER, time_added TEXT)''')

def update_and_broadcast(task_id, status=None, progress=None, done=None):
    """
    数据流向：
    1. 实时更新 Redis (高频)
    2. WebSocket 广播 (实时)
    3. 写入 SQLite (低频同步)
    """
    payload = {"task_id": task_id}
    
    # 1. 更新 Redis 缓存
    if status: 
        r.hset(f"task:{task_id}", "status", status)
        payload["status"] = status
    if progress: 
        r.hset(f"task:{task_id}", "progress", progress)
        payload["progress"] = progress
    if done is not None: 
        r.hset(f"task:{task_id}", "done", 1 if done else 0)
        payload["done"] = 1 if done else 0

    # 2. 实时广播
    socketio.emit('task_update', payload)

    # 3. 同步到持久化数据库 (仅在关键状态或完成时)
    if done or status in ["解析中...", "下载中", "已完成", "错误"]:
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                if status: conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))
                if progress: conn.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress, task_id))
                if done is not None: conn.execute("UPDATE tasks SET done = ? WHERE task_id = ?", (1 if done else 0, task_id))
        except:
            pass # 即使锁定也无妨，Redis 里已经有最新数据了

def is_too_short(file_path):
    try:
        cmd = [FFPROBE_PATH, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if res.stdout.strip() and float(res.stdout.strip()) < MIN_DURATION:
            return True
    except: pass
    return False

def get_video_src(page_url):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.binary_location = "/usr/bin/google-chrome"
    
    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(page_url)
        video_el = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "video.dplayer-video-current")))
        for _ in range(10):
            src = video_el.get_attribute("src")
            if src and "m3u8" in src: return src
            time.sleep(1)
    except: return None
    finally:
        if driver: driver.quit()

semaphore = threading.Semaphore(MAX_THREADS)
def download_worker(task_id, title, url, author):
    with semaphore:
        author_folder = re.sub(r'[\\/:*?"<>|]', '_', author).strip() or "未分类"
        target_dir = os.path.join(BASE_SAVE_DIR, author_folder)
        os.makedirs(target_dir, exist_ok=True)

        update_and_broadcast(task_id, status="解析中...")
        m3u8 = get_video_src(url)
        if not m3u8:
            update_and_broadcast(task_id, status="解析失败", done=True)
            return

        update_and_broadcast(task_id, status="下载中")
        safe_title = re.sub(r'[\\/:*?<>|]', '_', title)[:80]
        out_path = os.path.join(target_dir, f"{safe_title}.mp4")
        
        cmd = [FFMPEG_PATH, '-headers', "Referer: https://www.hjw01.com/\r\n", '-i', m3u8, '-c', 'copy', '-y', out_path]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8')
        
        last_broadcast_time = 0
        for line in proc.stdout:
            if "time=" in line:
                match = re.search(r"time=(\d{2}:\d{2}:\d{2})", line)
                if match:
                    # 频率限制：0.5秒广播一次，大幅减少负载
                    if time.time() - last_broadcast_time > 0.5:
                        update_and_broadcast(task_id, progress=match.group(1))
                        last_broadcast_time = time.time()
        
        proc.wait()
        if proc.returncode == 0 and os.path.exists(out_path) and is_too_short(out_path):
            os.remove(out_path)
            update_and_broadcast(task_id, status="已跳过(不足5min)", done=True)
        else:
            update_and_broadcast(task_id, status="已完成" if proc.returncode == 0 else "错误", done=True)

session_db = {}
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        content = request.form.get('video_txt', '')
        stoken = str(uuid.uuid4())[:12]
        session_db[stoken] = []
        
        titles = re.findall(r"标题:\s*(.*?)\s*(?:\||$|\n)", content)
        urls = re.findall(r"链接:\s*(https?://[^\s\n|]+)", content)
        authors = re.findall(r"作者:\s*(.*?)\s*(?:\n|$)", content)
        
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            for i in range(len(titles)):
                t, u, a = titles[i], urls[i], (authors[i] if i<len(authors) else "Unknown")
                tid = str(uuid.uuid4())[:12]
                conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", 
                             (tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                session_db[stoken].append(tid)
                # 同步到 Redis
                r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                threading.Thread(target=download_worker, args=(tid, t, u, a), daemon=True).start()
        return redirect(url_for('status_page', token=stoken))
    return render_template('index.html', history_token=HISTORY_TOKEN)

@app.route('/status/<token>')
def status_page(token):
    if token not in session_db: return "Expired", 404
    tasks = []
    for tid in session_db[token]:
        data = r.hgetall(f"task:{tid}")
        if data:
            data['task_id'] = tid
            tasks.append(data)
    return render_template('status.html', tasks=tasks)

@app.route('/history/<token>')
def history_page(token):
    if token != HISTORY_TOKEN: return "Denied", 403
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tasks ORDER BY time_added DESC LIMIT 500").fetchall()
    return render_template('history.html', tasks=rows)

if __name__ == '__main__':
    init_db()
    socketio.run(app, host='0.0.0.0', port=5000)