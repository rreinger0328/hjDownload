import requests
from bs4 import BeautifulSoup
import time
import os
import sys
import sqlite3
import logging

# --- 配置区 ---

# 加载 .env 环境变量文件，隐藏敏感 Token
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip() and not line.startswith('#') and '=' in line:
                key, val = line.strip().split('=', 1)
                os.environ[key.strip()] = val.strip()

IS_WINDOWS = sys.platform == 'win32'

# 从环境变量读取配置 (只读取环境变量，不设默认值以确保隐秘性)
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")


BASE_URL = "https://www.hjw01.com"
CHECK_INTERVAL = 21600  # 将检查时间修改为 6 小时 (6 * 3600 秒)
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "reptile.db") if IS_WINDOWS else "/app/data/reptile.db"

def setup_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('''CREATE TABLE IF NOT EXISTS new_data 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, author TEXT, time_added TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS old_data 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, author TEXT, time_added TEXT)''')

def is_url_pushed(url):
    """检查数据库中是否已经存有该记录（新、老表都要检查）"""
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        res1 = conn.execute("SELECT 1 FROM new_data WHERE url=?", (url,)).fetchone()
        if res1: return True
        res2 = conn.execute("SELECT 1 FROM old_data WHERE url=?", (url,)).fetchone()
        if res2: return True
    return False

def save_to_db(title, link, author):
    """将新扒取的视频数据存入 new_data 表"""
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        time_str = time.strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("INSERT INTO new_data (title, url, author, time_added) VALUES (?, ?, ?, ?)", (title, link, author, time_str))

def send_tg_notification(title, link, author):
    """发送格式化的消息到 Telegram"""
    if not TOKEN or not CHAT_ID:
        logging.info("未配置 TG_TOKEN 或 TG_CHAT_ID，跳过 TG 推送。")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    # 构造推送给 TG 的文本
    text = (
        f"<b>标题:</b> {title}\n"
        f"<b>作者:</b> {author}\n"
        f"<b>链接:</b> {link}"
    )
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logging.warning(f"TG发送失败: {e}")

def get_total_pages(soup):
    """获取总页数"""
    try:
        total_tag = soup.find("li", id="total")
        if total_tag:
            return int(total_tag.get_text(strip=True))
    except:
        pass
    return 1

def fetch_page(page_num, pushed_urls):
    """抓取页面内容并按格式记录"""
    url = f"{BASE_URL}/page/{page_num}/" if page_num > 1 else BASE_URL
    # 添加时间戳并设置请求头以强制绕过 CDN 和本地缓存
    url += f"?t={int(time.time())}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    logging.info(f"正在读取: 第 {page_num} 页")
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 寻找内容块
        rows = soup.find_all("div", class_="xqbj-list-rows")
        new_count = 0
        
        for row in rows:
            a_tag = row.find("a", href=True)
            if a_tag and a_tag.find("h3"):
                title = a_tag.find("h3").get_text(strip=True)
                link = requests.compat.urljoin(BASE_URL, a_tag['href'])
                
                # 提取作者：寻找特定的 class 容器
                author_tag = row.find("div", class_="xqbj-list-rows-bottom-tags-text")
                author = author_tag.get_text(strip=True) if author_tag else "未知作者"

                # 去重逻辑：以 URL 为准
                if link not in pushed_urls and not is_url_pushed(link):
                    logging.info(f"发现新内容: {title}")
                    
                    # 1. 推送到 TG
                    send_tg_notification(title, link, author)
                    
                    # 2. 保存入 SQLite (替代原本的 TXT)
                    save_to_db(title, link, author)
                    
                    pushed_urls.add(link)
                    new_count += 1
        
        return new_count, soup
    except Exception as e:
        logging.error(f"请求失败: {e}")
        return 0, None

def page_has_new(page_num):
    """快速检查某页是否有未入库的新 URL（不保存、不通知）"""
    url = f"{BASE_URL}/page/{page_num}/" if page_num > 1 else BASE_URL
    url += f"?t={int(time.time())}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.find_all("div", class_="xqbj-list-rows")
        for row in rows:
            a_tag = row.find("a", href=True)
            if a_tag and a_tag.find("h3"):
                link = requests.compat.urljoin(BASE_URL, a_tag['href'])
                if not is_url_pushed(link):
                    return True, soup
        return False, soup
    except Exception as e:
        logging.error(f"page_has_new({page_num}) 请求失败: {e}")
        return False, None

def _find_start_page(total_pages):
    """二分法定位第一条新内容所在的页。
    新内容集中在低页号，旧内容在高页号。返回起始页号。"""
    has_new, soup = page_has_new(1)
    if has_new or soup is None:
        return 1, soup  # 第一页就有新内容，或请求失败则从第 1 页开始

    # 第一页全是旧内容 → 二分搜索 [2, total_pages]
    low, high = 2, total_pages
    first_new_page = total_pages + 1  # 默认无新内容
    while low <= high:
        mid = (low + high) // 2
        has_new, _ = page_has_new(mid)
        if has_new:
            first_new_page = mid
            high = mid - 1  # 继续找更早的页
        else:
            low = mid + 1   # 往后找
        time.sleep(0.5)

    if first_new_page > total_pages:
        logging.info("二分搜索: 所有页面均无新内容")
        return total_pages + 1, None

    logging.info(f"二分搜索: 第一条新内容在 第 {first_new_page} 页 (共 {total_pages} 页)")
    return first_new_page, None

def main():
    setup_db()
    pushed_urls = set()

    while True:
        logging.info("--- 开始新一轮检查 ---")

        # 1) 获取总页数
        has_new, first_soup = page_has_new(1)
        total_pages = get_total_pages(first_soup) if first_soup else 1
        logging.info(f"总页数: {total_pages}")

        # 2) 二分法定位新内容起始页
        start_page, _ = _find_start_page(total_pages)

        # 3) 从起始页开始全量爬取
        empty_pages = 0
        for p in range(start_page, total_pages + 1):
            new_found, _ = fetch_page(p, pushed_urls)
            if new_found == 0:
                empty_pages += 1
                if empty_pages >= 4:  # 连续 4 页无新内容则停止
                    logging.info(f"连续 {empty_pages} 页无新内容，停止翻页")
                    break
            else:
                empty_pages = 0
            time.sleep(1)

        logging.info(f"检查完毕。等待 {CHECK_INTERVAL} 秒...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    main()