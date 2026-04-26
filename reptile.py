import requests
from bs4 import BeautifulSoup
import time
import os
import sqlite3

# --- 配置区 ---

# 加载 .env 环境变量文件，隐藏敏感 Token
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip() and not line.startswith('#') and '=' in line:
                key, val = line.strip().split('=', 1)
                os.environ[key.strip()] = val.strip()

# 从环境变量读取配置 (只读取环境变量，不设默认值以确保隐秘性)
TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")


BASE_URL = "https://www.hjw01.com"
CHECK_INTERVAL = 21600  # 将检查时间修改为 6 小时 (6 * 3600 秒)
DB_PATH = "/app/data/reptile.db"  # 与 app.py 中对应的路径保持一致

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
        print("未配置 TG_TOKEN 或 TG_CHAT_ID，跳过 TG 推送。")
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
        print(f"TG发送失败: {e}")

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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    print(f"正在读取: 第 {page_num} 页")
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
                    print(f"发现新内容: {title}")
                    
                    # 1. 推送到 TG
                    send_tg_notification(title, link, author)
                    
                    # 2. 保存入 SQLite (替代原本的 TXT)
                    save_to_db(title, link, author)
                    
                    pushed_urls.add(link)
                    new_count += 1
        
        return new_count, soup
    except Exception as e:
        print(f"请求失败: {e}")
        return 0, None

def main():
    # 初始化创建数据表
    setup_db()
    
    pushed_urls = set()
    
    while True:
        print("--- 开始新一轮检查 ---")
        # 爬取第一页并解析总页数
        new_on_page, first_soup = fetch_page(1, pushed_urls)
        
        if first_soup:
            total_pages = get_total_pages(first_soup)
            
            # 如果第一页有更新，则尝试翻页查看是否有更早的未推送内容
            for p in range(2, total_pages + 1):
                new_found, _ = fetch_page(p, pushed_urls)
                # 如果某一页完全没有新东西，就停止翻页
                if new_found == 0:
                    break
                time.sleep(1) # 翻页小延迟，避免频率过高
        
        print(f"检查完毕。等待 {CHECK_INTERVAL} 秒...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()