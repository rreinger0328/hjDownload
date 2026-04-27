import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import eventlet
eventlet.monkey_patch()

import os, re, time, threading, subprocess, uuid, sqlite3, redis
from flask import Flask, render_template, request, redirect, url_for, Response
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

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Redis 连接 (通过 Compose 里的服务名连接)，增加超时时间防止应用卡死
r = redis.Redis(host=os.environ.get('REDIS_HOST', 'redis_db'), port=6379, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.execute('PRAGMA journal_mode=WAL;') # 开启高性能模式
        conn.execute('''CREATE TABLE IF NOT EXISTS tasks 
                     (task_id TEXT PRIMARY KEY, title TEXT, author TEXT, 
                      status TEXT, progress TEXT, done INTEGER, time_added TEXT)''')
                      
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('''CREATE TABLE IF NOT EXISTS new_data 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, author TEXT, time_added TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS old_data 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, author TEXT, time_added TEXT)''')

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

    # 3. 同步到持久化数据库 (任务完成或关键状态时写库)
    if done or status in ["解析中...", "已完成", "错误", "解析失败", "已跳过(不足5min)"]:
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                query = "UPDATE tasks SET "
                params = []
                if status:
                    query += "status = ?, "
                    params.append(status)
                if progress:
                    query += "progress = ?, "
                    params.append(progress)
                if done is not None:
                    query += "done = ?, "
                    params.append(1 if done else 0)
                
                if params:
                    query = query.rstrip(', ') + " WHERE task_id = ?"
                    params.append(task_id)
                    conn.execute(query, tuple(params))
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
    logging.info(f"[Worker] Started task {task_id} for '{title}'")
    with semaphore:
        author_folder = re.sub(r'[\\/:*?"<>|]', '_', author).strip() or "未分类"
        target_dir = os.path.join(BASE_SAVE_DIR, author_folder)
        os.makedirs(target_dir, exist_ok=True)
        logging.info(f"[Worker] Task {task_id} target directory: {target_dir}")

        update_and_broadcast(task_id, status="解析中...")
        logging.info(f"[Worker] Task {task_id} fetching m3u8 src via Selenium...")
        m3u8 = get_video_src(url)
        if not m3u8:
            logging.error(f"[Worker] Task {task_id} failed to get m3u8.")
            update_and_broadcast(task_id, status="解析失败", done=True)
            return
        logging.info(f"[Worker] Task {task_id} extracted m3u8: {m3u8[:50]}...")

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
        logging.info(f"[Worker] Task {task_id} FFmpeg exited with code {proc.returncode}")
        if proc.returncode == 0 and os.path.exists(out_path) and is_too_short(out_path):
            logging.warning(f"[Worker] Task {task_id} video is too short, skipping.")
            os.remove(out_path)
            update_and_broadcast(task_id, status="已跳过(不足5min)", done=True)
        else:
            final_status = "已完成" if proc.returncode == 0 else "错误"
            logging.info(f"[Worker] Task {task_id} finished with status: {final_status}")
            update_and_broadcast(task_id, status=final_status, done=True)

session_db = {}
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        app.logger.info("Received POST request to begin batch download")
        content = request.form.get('video_txt', '')
        stoken = str(uuid.uuid4())[:12]
        session_db[stoken] = []
        
        titles = re.findall(r"标题:\s*(.*?)\s*(?:\||$|\n)", content)
        urls = re.findall(r"链接:\s*(https?://[^\s\n|]+)", content)
        authors = re.findall(r"作者:\s*(.*?)\s*(?:\n|$)", content)
        app.logger.info(f"Parsed {len(titles)} tasks from POST input")
        
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                app.logger.info("Successfully connected to SQLite DB")
                db_values = []
                for i in range(len(titles)):
                    t, u, a = titles[i], urls[i], (authors[i] if i<len(authors) else "Unknown")
                    tid = str(uuid.uuid4())[:12]
                    db_values.append((tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                    session_db[stoken].append(tid)
                    # 同步到 Redis
                    app.logger.info(f"Connecting to Redis to save task {tid}...")
                    r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                    app.logger.info(f"Task {tid} successfully written to Redis and starting background worker.")
                    threading.Thread(target=download_worker, args=(tid, t, u, a), daemon=True).start()
                
                if db_values:
                    app.logger.info(f"Batch inserting {len(db_values)} tasks into SQLite...")
                    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", db_values)
            app.logger.info("All tasks processed, preparing to redirect.")
        except Exception as e:
            app.logger.error(f"Error processing POST request: {e}", exc_info=True)
            return f"发生了内部错误: {e}", 500
            
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

@app.route('/reptile/new')
def reptile_new():
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as r_conn:
        r_conn.row_factory = sqlite3.Row
        rows = r_conn.execute("SELECT * FROM new_data ORDER BY id DESC").fetchall()
    return render_template('reptile_new.html', tasks=rows)

@app.route('/reptile/old')
def reptile_old():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    per_page = 50
    offset = (page - 1) * per_page
    
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as r_conn:
        r_conn.row_factory = sqlite3.Row
        
        query = "SELECT * FROM old_data"
        params = []
        if search:
            query += " WHERE title LIKE ? OR author LIKE ?"
            like_val = f"%{search}%"
            params.extend([like_val, like_val])
            
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        
        rows = r_conn.execute(query, params).fetchall()
        
        count_q = "SELECT COUNT(*) FROM old_data"
        c_params = []
        if search:
            count_q += " WHERE title LIKE ? OR author LIKE ?"
            c_params.extend([like_val, like_val])
        total = r_conn.execute(count_q, c_params).fetchone()[0]
        
    total_pages = (total + per_page - 1) // per_page
    return render_template('reptile_old.html', tasks=rows, page=page, total_pages=total_pages, search=search)

@app.route('/api/reptile/export', methods=['GET'])
def export_new_data():
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as r_conn:
        r_conn.row_factory = sqlite3.Row
        rows = r_conn.execute("SELECT * FROM new_data ORDER BY id ASC").fetchall()
        if not rows:
            return "没有新数据", 400
            
        r_conn.execute("INSERT INTO old_data (title, url, author, time_added) SELECT title, url, author, time_added FROM new_data")
        r_conn.execute("DELETE FROM new_data")
        r_conn.commit()
        
    # --- 修复的关键部分：导出TXT格式 ---
    lines = []
    for row in rows:
        lines.append(f"标题: {row['title']}")
        lines.append(f"链接: {row['url']}")
        lines.append(f"作者: {row['author']}")
        lines.append("-" * 30) # 分隔线
    
    # 使用真正的 \n 换行，并确保最后一行也有换行
    content = "\n".join(lines) + "\n"
    
    return Response(
        content,
        mimetype="text/plain",
        headers={
            "Content-disposition": "attachment; filename=reptile_new_data.txt",
            "Content-Type": "text/plain; charset=utf-8"
        }
    )

@app.route('/api/reptile/export_selected', methods=['GET'])
def export_selected():
    ids_str = request.args.get('ids', '')
    source = request.args.get('source', 'new')
    if not ids_str:
        return "未选择任何数据", 400
    
    id_list = [i.strip() for i in ids_str.split(',') if i.strip()]
    table = 'new_data' if source == 'new' else 'old_data'
    
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as r_conn:
        r_conn.row_factory = sqlite3.Row
        placeholders = ','.join(['?'] * len(id_list))
        rows = r_conn.execute(f"SELECT * FROM {table} WHERE id IN ({placeholders})", id_list).fetchall()
        if not rows:
            return "未找到选中的数据", 400
    
    lines = []
    for row in rows:
        lines.append(f"标题: {row['title']}")
        lines.append(f"链接: {row['url']}")
        lines.append(f"作者: {row['author']}")
        lines.append("-" * 30)
    
    content = "\n".join(lines) + "\n"
    
    return Response(
        content,
        mimetype="text/plain",
        headers={
            "Content-disposition": f"attachment; filename=selected_{source}_data.txt",
            "Content-Type": "text/plain; charset=utf-8"
        }
    )

@app.route('/api/reptile/download_selected', methods=['POST'])
def download_selected():
    ids_str = request.form.get('ids', '')
    source = request.form.get('source', 'new')
    if not ids_str:
        return "未选择任何数据", 400
    
    id_list = [i.strip() for i in ids_str.split(',') if i.strip()]
    table = 'new_data' if source == 'new' else 'old_data'
    
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as r_conn:
        r_conn.row_factory = sqlite3.Row
        placeholders = ','.join(['?'] * len(id_list))
        rows = r_conn.execute(f"SELECT * FROM {table} WHERE id IN ({placeholders})", id_list).fetchall()
        if not rows:
            return "未找到选中的数据", 400

    stoken = str(uuid.uuid4())[:12]
    session_db[stoken] = []
    
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            db_values = []
            for row in rows:
                t, u, a = row['title'], row['url'], row['author']
                tid = str(uuid.uuid4())[:12]
                db_values.append((tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                session_db[stoken].append(tid)
                
                r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                threading.Thread(target=download_worker, args=(tid, t, u, a), daemon=True).start()
                
            if db_values:
                conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", db_values)
    except Exception as e:
        app.logger.error(f"Error processing selected download: {e}", exc_info=True)
        return f"发生了内部错误: {e}", 500
        
    return redirect(url_for('status_page', token=stoken))

@app.route('/api/reptile/oneclick', methods=['POST'])
def oneclick_download():
    REPTILE_DB = os.path.join(os.path.dirname(DB_PATH), "reptile.db")
    with sqlite3.connect(REPTILE_DB, timeout=20) as r_conn:
        r_conn.row_factory = sqlite3.Row
        rows = r_conn.execute("SELECT * FROM new_data").fetchall()
        if not rows:
            return "没有新数据", 400
            
        # Move to old_data
        r_conn.execute("INSERT INTO old_data (title, url, author, time_added) SELECT title, url, author, time_added FROM new_data")
        r_conn.execute("DELETE FROM new_data")
        r_conn.commit()

    stoken = str(uuid.uuid4())[:12]
    session_db[stoken] = []
    
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            db_values = []
            for row in rows:
                t, u, a = row['title'], row['url'], row['author']
                tid = str(uuid.uuid4())[:12]
                db_values.append((tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                session_db[stoken].append(tid)
                
                # 同步到 Redis
                r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                threading.Thread(target=download_worker, args=(tid, t, u, a), daemon=True).start()
                
            if db_values:
                conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", db_values)
    except Exception as e:
        app.logger.error(f"Error processing oneclick: {e}", exc_info=True)
        return f"发生了内部错误: {e}", 500
        
    return redirect(url_for('status_page', token=stoken))

if __name__ == '__main__':
    init_db()
    # 启动爬虫后台线程（随主进程一起运行，daemon=True 保证主进程退出时自动结束）
    import reptile
    threading.Thread(target=reptile.main, daemon=True).start()
    logging.info("[Reptile] 爬虫后台线程已启动")
    socketio.run(app, host='0.0.0.0', port=5000)