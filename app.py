import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os, re, time, threading, subprocess, uuid, sqlite3, redis, sys
from flask import Flask, render_template, request, redirect, url_for, Response
from flask_socketio import SocketIO

# --- 加载 .env ---
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip() and not line.startswith('#') and '=' in line:
                key, val = line.strip().split('=', 1)
                os.environ[key.strip()] = val.strip()

IS_WINDOWS = sys.platform == 'win32'

app = Flask(__name__)
app.config['SECRET_KEY'] = 'hjw_redis_secure_key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- 配置 ---
BASE_SAVE_DIR = os.path.join(os.path.dirname(__file__), "downloads") if IS_WINDOWS else "/downloads"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tasks.db") if IS_WINDOWS else "/app/data/tasks.db"
FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"
MAX_THREADS = 3
MIN_DURATION = 300 # 5分钟
HISTORY_TOKEN = "manager_999"

import logging
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log") if IS_WINDOWS else "/app/log"
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding='utf-8'),
    ]
)

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
    
    # 1. 更新 Redis 缓存（网络故障时不影响 WebSocket 广播）
    try:
        if status:
            r.hset(f"task:{task_id}", "status", status)
        if progress:
            r.hset(f"task:{task_id}", "progress", progress)
        if done is not None:
            r.hset(f"task:{task_id}", "done", 1 if done else 0)
    except Exception:
        pass

    if status:
        payload["status"] = status
    if progress:
        payload["progress"] = progress
    if done is not None:
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
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
        if res.stdout.strip() and float(res.stdout.strip()) < MIN_DURATION:
            return True
    except subprocess.TimeoutExpired:
        logging.warning(f"[ffprobe] 超时 30s: {file_path}")
    except:
        pass
    return False


def _log_chromedriver_output():
    """输出 ChromeDriver 日志，用于诊断启动失败"""
    log_path = os.path.join(LOG_DIR, "chromedriver.log") if IS_WINDOWS else "/tmp/chromedriver.log"
    if os.path.exists(log_path):
        try:
            with open(log_path, "r") as f:
                content = f.read()
            if content.strip():
                logging.error(f"[Selenium] ChromeDriver 日志:\n{content[-2000:]}")
            os.remove(log_path)
        except:
            pass

def _get_chromedriver_path():
    """获取 ChromeDriver 路径。
    Docker 环境中 chromedriver 已在构建时预装到 /usr/local/bin/，直接使用；
    Windows 环境通过 webdriver_manager 下载（国内可加镜像环境变量）。
    """
    if not IS_WINDOWS:
        # Docker / Linux: 使用预装的 chromedriver
        path = "/usr/local/bin/chromedriver"
        if os.path.exists(path):
            logging.info(f"[Selenium] 使用预装 ChromeDriver: {path}")
            return path

    # Windows / 回退: 使用 webdriver_manager
    from webdriver_manager.chrome import ChromeDriverManager
    # 优先使用国内镜像
    mirror = os.environ.get("CHROMEDRIVER_MIRROR", "")
    if mirror:
        os.environ.setdefault("WDM_CDN_URL", mirror)
    logging.info("[Selenium] webdriver_manager 开始下载 ChromeDriver...")
    return ChromeDriverManager().install()

def get_video_src(page_url, title=""):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = Options()
    if IS_WINDOWS:
        options.add_argument("--headless")
    else:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-async-dns")
    options.add_argument("--dns-prefetch-disable")
    options.add_argument("--disable-features=AsyncDns,OptimizationHints")
    options.add_argument(f"--user-data-dir=/tmp/chrome-data-{uuid.uuid4().hex[:8]}")
    if not IS_WINDOWS:
        options.add_argument("--remote-debugging-port=0")
    if IS_WINDOWS:
        options.binary_location = r"C:\Users\Administrator\AppData\Local\Google\Chrome\Bin\chrome.exe"
    else:
        options.binary_location = "/usr/bin/google-chrome"

    driver = None
    try:
        # 1) 获取 ChromeDriver
        driver_path = _get_chromedriver_path()
        logging.info(f"[Selenium] ChromeDriver 就绪: {driver_path}")

        # 2) 启动 Chrome
        logging.info("[Selenium] 正在启动 Chrome 浏览器...")
        chromedriver_log = os.path.join(LOG_DIR, "chromedriver.log") if IS_WINDOWS else "/tmp/chromedriver.log"
        service = Service(driver_path, log_output=chromedriver_log)
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
        logging.info("[Selenium] Chrome 浏览器已启动")

        # 3) 访问页面
        logging.info(f"[Selenium] 正在访问页面 (超时 30s): {page_url[:80]}...")
        driver.get(page_url)
        logging.info("[Selenium] 页面加载完成，等待视频元素出现...")
        # 保存页面 HTML 到 log 目录
        if title:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log") if IS_WINDOWS else "/app/log"
            os.makedirs(log_dir, exist_ok=True)
            safe_filename = re.sub(r'[\\/:*?"<>|]', '_', title)[:100] + ".log"
            with open(os.path.join(log_dir, safe_filename), 'w', encoding='utf-8') as f:
                f.write(driver.page_source)
            logging.info(f"[Selenium] 页面 HTML 已保存到 log/{safe_filename}")

        # 4) 等待视频元素，多策略回退
        logging.info("[Selenium] 等待视频元素出现 (超时 25s)...")
        srcs = []
        selectors = [
            "video.dplayer-video-current",
            "video[src*='m3u8']",
            "div.dplayer-video-wrap video",
            ".dplayer-video-wrap video",
        ]
        for sel in selectors:
            try:
                WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                logging.info(f"[Selenium] 选择器 '{sel}' 匹配成功")
            except:
                logging.info(f"[Selenium] 选择器 '{sel}' 未匹配，尝试下一个")
                continue

        # 5) 诊断：输出页面上所有 video 及 iframe 元素
        video_els = driver.find_elements(By.TAG_NAME, "video")
        iframe_els = driver.find_elements(By.TAG_NAME, "iframe")
        logging.info(f"[Selenium] 诊断: 页面共 {len(video_els)} 个 video, {len(iframe_els)} 个 iframe")
        for i, el in enumerate(video_els[:5]):
            src = el.get_attribute("src") or ""
            cid = el.get_attribute("id") or ""
            cls = el.get_attribute("class") or ""
            logging.info(f"[Selenium]   video[{i}] id={cid} class={cls} src[:80]={src[:80]}")

        # 6) 循环扫描提取 m3u8
        for i in range(15):
            # 尝试所有可能的选择器
            for sel in selectors + ["video", "video[src]"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    src = el.get_attribute("src")
                    if src and "m3u8" in src and src not in srcs:
                        srcs.append(src)
            if srcs:
                logging.info(f"[Selenium] 成功提取 {len(srcs)} 个 m3u8 链接")
                return srcs
            if len(srcs) == 0 and i == 0:
                # 第一次没找到就执行 JS 兜底
                try:
                    js_src = driver.execute_script(
                        "var v=document.querySelector('video[src*=\".m3u8\"]'); return v?v.src:'';"
                    )
                    if js_src and "m3u8" in js_src and js_src not in srcs:
                        srcs.append(js_src)
                        logging.info(f"[Selenium] JS 兜底提取到 m3u8: {js_src[:80]}...")
                        return srcs
                except:
                    pass
            time.sleep(1)
        logging.warning(f"[Selenium] {i+1} 次扫描均未获取到 m3u8 链接")

    except TimeoutError:
        logging.error("[Selenium] 超时异常，终止当前解析任务")
        _log_chromedriver_output()
        return []
    except Exception as e:
        msg = str(e)
        if "TimeoutError" in type(e).__name__ or "TimeoutError" in msg or "超时" in msg:
            logging.error(f"[Selenium] 超时异常 (经由 {type(e).__name__}): {msg[-300:]}")
        else:
            logging.error(f"[Selenium] 异常: {type(e).__name__}: {msg[-500:]}")
        import traceback
        logging.error(f"[Selenium] 堆栈:\n{traceback.format_exc()}")
        _log_chromedriver_output()
        return []
    finally:
        if driver:
            logging.info("[Selenium] 正在关闭 Chrome...")
            try:
                driver.quit()
            except:
                pass
            logging.info("[Selenium] Chrome 已关闭")
    return []

m3u8_semaphore = threading.Semaphore(MAX_THREADS)
mp4_semaphore = threading.Semaphore(MAX_THREADS)

def download_worker(task_id, title, url, author, video_type="m3u8"):
    logging.info(f"[Worker] 任务 {task_id} 开始下载 '{title}' (类型: {video_type})")
    
    current_semaphore = m3u8_semaphore if video_type == "m3u8" else mp4_semaphore
    
    with current_semaphore:
        logging.info(f"[Worker] 任务 {task_id} 已获取信号量，开始处理")
        author_folder = re.sub(r'[\\/:*?"<>|]', '_', author).strip() or "未分类"
        target_dir = os.path.join(BASE_SAVE_DIR, author_folder)
        os.makedirs(target_dir, exist_ok=True)
        logging.info(f"[Worker] 任务 {task_id} 目标目录: {target_dir}")

        if video_type == "m3u8":
            update_and_broadcast(task_id, status="解析中...")
            logging.info(f"[Worker] 任务 {task_id} 状态已更新为「解析中...」，即将调用 get_video_src()")
            src_urls = get_video_src(url, title)
            logging.info(f"[Worker] 任务 {task_id} get_video_src() 返回，结果数量: {len(src_urls) if src_urls else 0}")
            if not src_urls:
                logging.error(f"[Worker] 任务 {task_id} 获取 m3u8 失败。")
                update_and_broadcast(task_id, status="解析失败", done=True)
                return
            logging.info(f"[Worker] 任务 {task_id} 提取到 {len(src_urls)} 个 m3u8 链接。")
        else:
            src_urls = [url]
            logging.info(f"[Worker] 任务 {task_id} 使用直链 ({video_type}): {url[:50]}...")

        safe_title = re.sub(r'[\\/:*?<>|]', '_', title)[:80]
        
        all_success = True
        any_success = False
        skipped_count = 0
        
        for idx, src_url in enumerate(src_urls):
            part_suffix = f"-第{idx+1}集" if len(src_urls) > 1 else ""
            status_text = f"下载中({idx+1}/{len(src_urls)})" if len(src_urls) > 1 else "下载中"
            update_and_broadcast(task_id, status=status_text)
            
            out_path = os.path.join(target_dir, f"{safe_title}{part_suffix}.mp4")
            # 从页面 URL 提取 Referer（防盗链需要匹配来源域名）
            ref = url if video_type == "m3u8" else "https://www.hjw01.com/"
            headers = (
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7\r\n"
                "Accept-Language: zh-CN,zh;q=0.9\r\n"
                "Cache-Control: max-age=0\r\n"
                "Priority: u=0, i\r\n"
                f"Referer: {ref}\r\n"
                "Sec-CH-UA: \"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"\r\n"
                "Sec-CH-UA-Mobile: ?0\r\n"
                "Sec-CH-UA-Platform: \"Windows\"\r\n"
                "Sec-Fetch-Dest: document\r\n"
                "Sec-Fetch-Mode: navigate\r\n"
                "Sec-Fetch-Site: cross-site\r\n"
                "Sec-Fetch-User: ?1\r\n"
                "Upgrade-Insecure-Requests: 1\r\n"
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            )
            cmd = [FFMPEG_PATH, '-headers', headers, '-i', src_url, '-c', 'copy', '-y', out_path]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8')
            
            last_broadcast_time = 0
            for line in proc.stdout:
                if "time=" in line:
                    match = re.search(r"time=(\d{2}:\d{2}:\d{2})", line)
                    if match:
                        if time.time() - last_broadcast_time > 0.5:
                            update_and_broadcast(task_id, progress=match.group(1))
                            last_broadcast_time = time.time()
            
            try:
                proc.wait(timeout=1200)  # 最长等待 20 分钟
            except subprocess.TimeoutExpired:
                logging.error(f"[Worker] 任务 {task_id} 第 {idx+1} 部分 FFmpeg 超时 (20min)，强制终止")
                proc.kill()
                proc.wait()
            logging.info(f"[Worker] 任务 {task_id} 第 {idx+1} 部分 FFmpeg 退出码: {proc.returncode}")

            if proc.returncode == 0 and os.path.exists(out_path):
                if is_too_short(out_path):
                    logging.warning(f"[Worker] 任务 {task_id} 第 {idx+1} 部分视频时长不足，已跳过。")
                    os.remove(out_path)
                    skipped_count += 1
                else:
                    any_success = True
            else:
                all_success = False

        if skipped_count == len(src_urls):
            final_status = "已跳过(不足5min)"
        elif any_success and all_success:
            final_status = "已完成"
        elif any_success and not all_success:
            final_status = "部分完成"
        else:
            final_status = "错误"
            
        logging.info(f"[Worker] 任务 {task_id} 结束，状态: {final_status}")
        update_and_broadcast(task_id, status=final_status, done=True)

session_db = {}
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        app.logger.info("收到 POST 请求，开始批量下载")
        video_type = request.form.get('video_type', 'm3u8')
        
        if video_type == 'm3u8_direct':
            content = request.form.get('video_txt_direct', '')
        else:
            content = request.form.get('video_txt', '')
            
        stoken = str(uuid.uuid4())[:12]
        session_db[stoken] = []
        r.sadd("all_tokens", stoken)
        r.set(f"token_time:{stoken}", time.strftime('%Y-%m-%d %H:%M:%S'))
        
        titles = re.findall(r"标题:\s*(.*?)\s*(?:\||$|\n)", content)
        urls = re.findall(r"链接:\s*(https?://[^\s\n|]+)", content)
        authors = re.findall(r"作者:\s*(.*?)\s*(?:\n|$)", content)
        app.logger.info(f"从 POST 输入中解析到 {len(titles)} 个任务")
        
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                app.logger.info("成功连接 SQLite 数据库")
                db_values = []
                for i in range(len(titles)):
                    t = titles[i]
                    u = urls[i] if i < len(urls) else ""
                    a = authors[i] if i < len(authors) else "Unknown"
                    if not u:
                        continue
                    tid = str(uuid.uuid4())[:12]
                    db_values.append((tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                    session_db[stoken].append(tid)
                    r.sadd(f"token:{stoken}:tasks", tid)
                    # 同步到 Redis
                    app.logger.info(f"正在连接 Redis 保存任务 {tid}...")
                    r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                    app.logger.info(f"任务 {tid} 已写入 Redis，正在启动后台工作线程。")
                    threading.Thread(target=download_worker, args=(tid, t, u, a, video_type), daemon=True).start()
                
                if db_values:
                    app.logger.info(f"批量插入 {len(db_values)} 个任务到 SQLite...")
                    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", db_values)
            app.logger.info("所有任务已处理，准备重定向。")
        except Exception as e:
            app.logger.error(f"处理 POST 请求出错: {e}", exc_info=True)
            return f"发生了内部错误: {e}", 500
            
        return redirect(url_for('status_page', token=stoken))
    return render_template('index.html', history_token=HISTORY_TOKEN)

@app.route('/status/<token>')
def status_page(token):
    tids = r.smembers(f"token:{token}:tasks")
    if not tids:
        tids = session_db.get(token, [])
    if not tids: return "已过期或未找到", 404
    tasks = []
    for tid in tids:
        data = r.hgetall(f"task:{tid}")
        if data:
            data['task_id'] = tid
            tasks.append(data)
    return render_template('status.html', tasks=tasks)

@app.route('/tokens')
def tokens_page():
    all_tokens = r.smembers("all_tokens")
    tokens_info = []
    for t in all_tokens:
        tids = r.smembers(f"token:{t}:tasks")
        total = len(tids)
        running = 0
        for tid in tids:
            done = r.hget(f"task:{tid}", "done")
            if str(done) == "0" or done is None:
                running += 1
        add_time = r.get(f"token_time:{t}") or "未知"
        tokens_info.append({
            "token": t,
            "total": total,
            "running": running,
            "time": add_time
        })
    tokens_info.sort(key=lambda x: x["time"], reverse=True)
    return render_template('tokens.html', tokens=tokens_info)

@app.route('/history/<token>')
def history_page(token):
    if token != HISTORY_TOKEN: return "拒绝访问", 403
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
    r.sadd("all_tokens", stoken)
    r.set(f"token_time:{stoken}", time.strftime('%Y-%m-%d %H:%M:%S'))
    
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            db_values = []
            for row in rows:
                t, u, a = row['title'], row['url'], row['author']
                tid = str(uuid.uuid4())[:12]
                db_values.append((tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                session_db[stoken].append(tid)
                r.sadd(f"token:{stoken}:tasks", tid)
                
                r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                threading.Thread(target=download_worker, args=(tid, t, u, a, "m3u8"), daemon=True).start()
                
            if db_values:
                conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", db_values)
    except Exception as e:
        app.logger.error(f"批量下载选中项出错: {e}", exc_info=True)
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
    r.sadd("all_tokens", stoken)
    r.set(f"token_time:{stoken}", time.strftime('%Y-%m-%d %H:%M:%S'))
    
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            db_values = []
            for row in rows:
                t, u, a = row['title'], row['url'], row['author']
                tid = str(uuid.uuid4())[:12]
                db_values.append((tid, t, a, '排队中', '00:00:00', 0, time.strftime('%m-%d %H:%M')))
                session_db[stoken].append(tid)
                r.sadd(f"token:{stoken}:tasks", tid)
                
                # 同步到 Redis
                r.hset(f"task:{tid}", mapping={"author":a, "title":t, "status":"排队中", "progress":"00:00:00", "done":0})
                threading.Thread(target=download_worker, args=(tid, t, u, a, "m3u8"), daemon=True).start()
                
            if db_values:
                conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", db_values)
    except Exception as e:
        app.logger.error(f"一键下载出错: {e}", exc_info=True)
        return f"发生了内部错误: {e}", 500
        
    return redirect(url_for('status_page', token=stoken))

if __name__ == '__main__':
    init_db()
    # 启动爬虫后台线程（随主进程一起运行，daemon=True 保证主进程退出时自动结束）
    import reptile
    threading.Thread(target=reptile.main, daemon=True).start()
    logging.info("[Reptile] 爬虫后台线程已启动")
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)