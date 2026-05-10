#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线报监控脚本 - GitHub Actions 版 v2.0
- Playwright 抓取页面内容
- 支持自定义解析器（Epic、Steam、GOG 等）
- 内置 clawemail HTTP API 发邮件
- hashes.json 持久化到 Git 仓库
- 新增：内容去重、失败降频、性能监控、告警机制、历史趋势
"""

import os
import sys
import json
import hashlib
import time
import re
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from difflib import SequenceMatcher

# ====== 配置 ======
SCRIPT_DIR = Path(__file__).parent
SITES_CONFIG = SCRIPT_DIR / "sites.json"
HASH_STORE = SCRIPT_DIR / "hashes.json"
TREND_STORE = SCRIPT_DIR / "trends.json"

# 默认超时时间（毫秒）
DEFAULT_TIMEOUT = 25000

# 清理配置
MAX_ARTICLES_PER_SITE = 30  # 每站点最多保留文章数
MAX_FAIL_COUNT = 3  # 连续失败多少次后降频
ALERT_FAIL_COUNT = 5  # 连续失败多少次后发送告警

# 去重配置
SIMILARITY_THRESHOLD = 0.75  # 标题相似度阈值

# 邮件配置从环境变量读取（GitHub Secrets）
CLAWEMAIL_API_KEY = os.environ.get("CLAWEMAIL_API_KEY", "")
CLAWEMAIL_USER = os.environ.get("CLAWEMAIL_USER", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")

# Gist 配置（用于手机查看监控状态）
GIST_ID = os.environ.get("GIST_ID", "")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")

# clawemail HTTP API
TOKEN_URL = "https://claw.163.com/claw-api-gateway/open/v1/mail/auth/token"
API_BASE = "https://claw.163.com/claw-api-gateway/api/coremail"


# ====== clawemail 纯 HTTP 实现 ======
import requests as req

_cached_token = None
_cached_token_expires = 0

def get_access_token():
    """用 API Key 换 access token"""
    global _cached_token, _cached_token_expires
    if _cached_token and time.time() < _cached_token_expires - 60:
        return _cached_token
    resp = req.post(TOKEN_URL, json={"uid": CLAWEMAIL_USER}, headers={
        "Authorization": f"Bearer {CLAWEMAIL_API_KEY}",
        "Content-Type": "application/json",
    }, timeout=15)
    data = resp.json().get("result", {})
    token = data.get("accessToken")
    expires_in = data.get("expiresIn", 3600)
    if not token:
        raise RuntimeError(f"获取 token 失败: {resp.text[:200]}")
    _cached_token = token
    _cached_token_expires = time.time() + expires_in
    return token


def clawemail_send(to_list, subject, body_html, is_html=True):
    """通过 clawemail HTTP API 发送邮件"""
    if not CLAWEMAIL_API_KEY or not CLAWEMAIL_USER:
        print("[WARN] 未配置 CLAWEMAIL_API_KEY / CLAWEMAIL_USER，跳过邮件发送")
        return False
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    compose_body = {
        "to": to_list,
        "subject": subject,
        "content": body_html,
        "isHtml": is_html,
        "priority": 3,
        "saveSentCopy": True,
    }
    resp1 = req.post(
        f"{API_BASE}/proxy",
        params={"uid": CLAWEMAIL_USER, "func": "mbox:compose"},
        json={"action": "continue", "attrs": compose_body},
        headers=headers, timeout=15,
    )
    data1 = resp1.json()
    if data1.get("code") != "S_OK":
        raise RuntimeError(f"compose continue 失败: {data1}")
    compose_id = data1.get("var")
    if isinstance(compose_id, dict):
        compose_id = compose_id.get("id")
    if not compose_id:
        raise RuntimeError(f"未获取 composeId: {data1}")

    deliver_body = dict(compose_body)
    resp2 = req.post(
        f"{API_BASE}/proxy",
        params={"uid": CLAWEMAIL_USER, "func": "mbox:compose"},
        json={"id": compose_id, "action": "deliver", "attrs": deliver_body},
        headers=headers, timeout=15,
    )
    data2 = resp2.json()
    if data2.get("code") != "S_OK":
        raise RuntimeError(f"compose deliver 失败: {data2}")
    return True


# ====== 页面抓取 ======
def fetch_with_playwright(url, browser=None, timeout_ms=DEFAULT_TIMEOUT, max_retries=2):
    """用 Playwright 渲染页面，返回 HTML。可复用传入的 browser 实例。支持失败重试。"""
    own_browser = browser is None
    if own_browser:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    
    for attempt in range(max_retries):
        page = browser.new_page()
        try:
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            time.sleep(2)
            return page.content()
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  重试 {attempt + 1}/{max_retries}...")
                time.sleep(2)
            else:
                print(f"  Playwright 超时/错误: {e}")
                return None
        finally:
            page.close()
            if own_browser:
                browser.close()
                pw.stop()


def fetch_with_requests(url):
    """备用：纯 requests 抓取（无法渲染 JS）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        r = req.get(url, timeout=15, headers=headers, allow_redirects=True, verify=False)
        return r.text
    except Exception as e:
        print(f"  requests 错误: {e}")
        return None


# ====== 通用解析器基类 ======
class BaseParser:
    """解析器基类，提供通用功能"""
    
    def __init__(self, content, base_url):
        self.content = content
        self.base_url = base_url
    
    def parse(self):
        """子类实现具体解析逻辑"""
        raise NotImplementedError
    
    def extract_by_pattern(self, pattern, max_items=20, url_group=1, title_group=2, 
                          filters=None, seen_urls=None):
        """通用正则提取方法"""
        articles = []
        if not self.content:
            return articles
        
        if seen_urls is None:
            seen_urls = set()
        
        for m in re.finditer(pattern, self.content, re.DOTALL):
            url = m.group(url_group).strip()
            title = m.group(title_group).strip()
            
            # 应用过滤器
            if filters:
                if any(f in title for f in filters.get('title_exclude', [])):
                    continue
                if any(f in url for f in filters.get('url_exclude', [])):
                    continue
            
            if url in seen_urls:
                continue
            if len(title) < 3 or len(title) > 120:
                continue
            
            seen_urls.add(url)
            articles.append({"title": title, "url": url})
            
            if len(articles) >= max_items:
                break
        
        return articles


class EpicParser(BaseParser):
    def parse(self):
        articles = []
        if not self.content:
            return articles
        
        # Epic 页面是 React 渲染，数据在 __NEXT_DATA__ 中
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', self.content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                catalog = data.get("props", {}).get("pageProps", {}).get("catalogOffers", {})
                elements = catalog.get("elements", [])
                for item in elements:
                    title = item.get("title", "")
                    slug = item.get("productSlug", "") or item.get("urlSlug", "")
                    if title and slug:
                        url = f"https://store.epicgames.com/zh-CN/p/{slug}"
                        articles.append({"title": f"[Epic免费] {title}", "url": url})
            except:
                pass
        
        if not articles:
            pattern = r'href="(/zh-CN/p/[^"]+)"[^>]*>.*?<span[^>]*>([^<]{3,50})</span>'
            for m in re.finditer(pattern, self.content, re.DOTALL):
                href, title = m.groups()
                if "free" in title.lower() or "免费" in title:
                    articles.append({"title": title.strip(), "url": f"https://store.epicgames.com{href}"})
        
        return articles[:10]


class SteamParser(BaseParser):
    def parse(self):
        pattern = r'<a[^>]+href="(https://store\.steampowered\.com/app/\d+/[^"]+/)"[^>]*>.*?<span[^>]*class="title"[^>]*>([^<]+)</span>'
        articles = self.extract_by_pattern(pattern, max_items=15)
        
        if not articles:
            pattern2 = r'<a[^>]+href="(https://store\.steampowered\.com/app/\d+[^"]*)"[^>]*>([^<]{3,80})</a>'
            for m in re.finditer(pattern2, self.content, re.DOTALL):
                url, title = m.groups()
                title = title.strip()
                if "免费" in title or "Free" in title.lower() or len(title) < 50:
                    articles.append({"title": title, "url": url})
        
        return [{"title": f"[Steam免费] {a['title']}", "url": a['url']} for a in articles]


class GogParser(BaseParser):
    def parse(self):
        pattern = r'<a[^>]+href="(https://www\.gog\.com/[^"]+)"[^>]*>.*?class="product-tile__title"[^>]*>([^<]+)</span>'
        articles = self.extract_by_pattern(pattern, max_items=15)
        
        if not articles:
            pattern2 = r'href="(https://www\.gog\.com/[^"]+game[^"]*)"[^>]*>([^<]{3,60})</a>'
            articles = self.extract_by_pattern(pattern2, max_items=15)
        
        return [{"title": f"[GOG免费] {a['title']}", "url": a['url']} for a in articles]


class GenericLinkParser(BaseParser):
    """通用链接解析器，支持自定义配置"""
    
    def __init__(self, content, base_url, url_pattern=None, title_min=6, title_max=80,
                 url_excludes=None, title_excludes=None):
        super().__init__(content, base_url)
        self.url_pattern = url_pattern
        self.title_min = title_min
        self.title_max = title_max
        self.url_excludes = url_excludes or []
        self.title_excludes = title_excludes or []
    
    def parse(self):
        if not self.content:
            return []
        
        articles = []
        seen_urls = set()
        
        if self.url_pattern:
            pattern = self.url_pattern
        else:
            pattern = r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
        
        for m in re.finditer(pattern, self.content, re.DOTALL):
            url = m.group(1).strip()
            title = m.group(2).strip()
            
            # 过滤
            if any(x in url for x in self.url_excludes):
                continue
            if any(x in title for x in self.title_excludes):
                continue
            if url in seen_urls:
                continue
            if len(title) < self.title_min or len(title) > self.title_max:
                continue
            
            seen_urls.add(url)
            articles.append({"title": title, "url": url})
        
        return articles[:20]


# ====== 解析器工厂 ======
PARSERS = {
    "epic": EpicParser,
    "steam": SteamParser,
    "gog": GogParser,
    "foxirj": lambda c, u: GenericLinkParser(c, u, 
        url_pattern=r'<a[^>]+href="(https://foxirj\.com/[^"]+)"[^>]*>.*?<h[23][^>]*>([^<]+)</h[23]>',
        title_min=3, title_excludes=["页面"]).parse(),
    "haoyangmao": lambda c, u: (GenericLinkParser(c, u,
        url_pattern=r'<a[^>]+href="(https?://www\.haoyangmao123\.com/[^"]+)"[^>]*>([^<]+)</a>',
        title_min=6, title_excludes=['首页', '登录', '注册', '更多', '关于']).parse() 
        if not ('cloudflare' in c.lower() and '5xx-error' in c.lower()) else []),
    "down423": lambda c, u: GenericLinkParser(c, u,
        url_pattern=r'<a[^>]+href="(https://www\.423down\.com/\d+\.html)"[^>]*>([^<]+)</a>',
        url_excludes=['/system.html', '/win11', '/win10', '/win7']).parse(),
    "ghxi": lambda c, u: GenericLinkParser(c, u,
        url_pattern=r'<a[^>]+href="(https://www\.ghxi\.com/[^"]+\.html)"[^>]*>([^<]+)</a>',
        url_excludes=['/category/', '/tag/', '/page/', '/author/']).parse(),
    "baicaio": lambda c, u: GenericLinkParser(c, u,
        url_pattern=r'<a[^>]+href="(https://www\.baicaio\.com/[^"]+\.html)"[^>]*>([^<]+)</a>',
        title_min=6).parse(),
    "indiegame": lambda c, u: GenericLinkParser(c, u,
        url_pattern=r'<a[^>]+href="(https://[^"]+)"[^>]*>([^<]{5,80})</a>',
        url_excludes=['wix.com', 'template', 'facebook', 'twitter', 'instagram'],
        title_min=5).parse(),
}


# ====== 通用内容提取 ======
def extract_articles(content, base_url=""):
    """从 HTML 提取文章链接和标题（通用规则）"""
    if not content:
        return []
    articles = []
    seen = set()
    pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([^<]{6,120})\s*</a>'
    for m in re.finditer(pattern, content, re.I | re.S):
        href = m.group(1).strip()
        title = re.sub(r'\s+', ' ', m.group(2).strip())
        if len(title) < 6 or len(title) > 120 or title in seen:
            continue
        if any(x in href.lower() for x in ['login', 'register', 'about', 'contact', 'javascript', '#', 'mailto', 'search', 'tag/', 'category/', 'page/', 'author/']):
            continue
        if any(x in title for x in ['登录', '注册', '搜索', '更多', '返回', '首页', '下一页', '上一页', '加载', '展开']):
            continue
        if href.startswith('//'):
            href = 'https:' + href
        elif href.startswith('/'):
            parsed = re.match(r'(https?://[^/]+)', base_url)
            if parsed:
                href = parsed.group(1) + href
        elif not href.startswith('http'):
            continue
        seen.add(title)
        articles.append({"title": title, "url": href})
    return articles


def content_hash(content):
    """对去噪后的 HTML 算 hash"""
    if not content:
        return None
    c = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
    c = re.sub(r'<style[^>]*>.*?</style>', '', c, flags=re.DOTALL | re.IGNORECASE)
    c = re.sub(r'\s+', ' ', c)
    return hashlib.md5(c.encode()).hexdigest()


# ====== 内容去重 ======
def title_similarity(t1, t2):
    """计算两个标题的相似度"""
    return SequenceMatcher(None, t1, t2).ratio()


def deduplicate_items(items, threshold=SIMILARITY_THRESHOLD):
    """基于标题相似度去重"""
    if not items:
        return items
    
    unique = []
    for item in items:
        is_dup = False
        for existing in unique:
            if title_similarity(item['title'], existing['title']) >= threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(item)
    
    return unique


# ====== 主逻辑 ======
def load_json(path):
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cleanup_hashes(hashes):
    """清理 hashes.json，限制每个站点的文章数量"""
    for sid, data in hashes.items():
        if "articles" in data and len(data["articles"]) > MAX_ARTICLES_PER_SITE:
            data["articles"] = data["articles"][-MAX_ARTICLES_PER_SITE:]
    return hashes


def should_skip_site(site, hashes):
    """判断是否应该跳过该站点（失败降频）"""
    sid = str(site["id"])
    old_data = hashes.get(sid, {})
    fail_count = old_data.get("fail_count", 0)
    
    if fail_count >= MAX_FAIL_COUNT:
        # 检查上次检查时间
        last_time_str = old_data.get("time", "")
        if last_time_str:
            try:
                last_time = datetime.fromisoformat(last_time_str)
                # 降频：每 2 小时检查一次
                if datetime.now() - last_time < timedelta(hours=2):
                    return True
            except:
                pass
    return False


def update_fail_count(hashes, sid, success):
    """更新失败计数"""
    sid = str(sid)
    if sid not in hashes:
        hashes[sid] = {}
    
    if success:
        hashes[sid]["fail_count"] = 0
    else:
        hashes[sid]["fail_count"] = hashes[sid].get("fail_count", 0) + 1
    
    return hashes[sid].get("fail_count", 0)


def get_alert_sites(hashes):
    """获取需要发送告警的站点"""
    alert_sites = []
    for sid, data in hashes.items():
        fail_count = data.get("fail_count", 0)
        # 刚好达到告警阈值时发送
        if fail_count == ALERT_FAIL_COUNT:
            alert_sites.append({
                "id": sid,
                "fail_count": fail_count,
                "last_time": data.get("time", "")
            })
    return alert_sites


def update_trends(trends, results, new_count):
    """更新趋势数据"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    if today not in trends:
        trends[today] = {
            "new_count": 0,
            "success": 0,
            "failed": 0
        }
    
    trends[today]["new_count"] += new_count
    trends[today]["success"] += sum(1 for r in results if r.get("ok"))
    trends[today]["failed"] += sum(1 for r in results if not r.get("ok"))
    
    # 只保留最近 7 天
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    trends = {k: v for k, v in trends.items() if k >= cutoff}
    
    return trends


def main():
    parser = argparse.ArgumentParser(description='线报监控脚本 v2.0')
    parser.add_argument('--batch', type=int, choices=[1, 2], default=None,
                        help='批次号 (1 或 2)，不指定则跑全部站点')
    args = parser.parse_args()
    
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 线报监控启动 v2.0")
    print(f"Python: {sys.version}")
    if args.batch:
        print(f"批次: {args.batch}")

    # 加载站点配置
    sites_env = os.environ.get("SITES_CONFIG", "")
    if sites_env:
        try:
            config = json.loads(sites_env)
            all_sites = config.get("sites", [])
            print(f"从环境变量加载 {len(all_sites)} 个站点")
        except json.JSONDecodeError as e:
            print(f"[ERROR] 环境变量 SITES_CONFIG 格式错误: {e}")
            sys.exit(1)
    elif SITES_CONFIG.exists():
        with open(SITES_CONFIG, 'r', encoding='utf-8') as f:
            config = json.load(f)
        all_sites = config.get("sites", [])
        print(f"从文件加载 {len(all_sites)} 个站点")
    else:
        print(f"[ERROR] 未找到站点配置")
        sys.exit(1)
    
    # 分批处理
    if args.batch:
        batch_size = (len(all_sites) + 1) // 2
        if args.batch == 1:
            sites = all_sites[:batch_size]
        else:
            sites = all_sites[batch_size:]
        print(f"共 {len(all_sites)} 个站点，本批次 {len(sites)} 个\n")
    else:
        sites = all_sites
        print(f"共 {len(sites)} 个站点待监控\n")

    # 加载历史数据
    hashes = load_json(HASH_STORE)
    trends = load_json(TREND_STORE)
    
    # 清理历史数据
    hashes = cleanup_hashes(hashes)
    
    results = []
    all_new_items = []
    skipped_sites = []
    performance_stats = []

    # 启动浏览器
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    browser_restart_interval = 10
    sites_processed = 0
    
    try:
        for i, site in enumerate(sites):
            # 浏览器重启
            if sites_processed > 0 and sites_processed % browser_restart_interval == 0:
                print(f"\n🔄 已处理 {sites_processed} 个站点，重启浏览器...")
                browser.close()
                browser = pw.chromium.launch(headless=True)
            
            sid = str(site["id"])
            name = site["name"]
            url = site["url"]
            use_js = site.get("js", True)
            parser_name = site.get("parser", "")
            timeout = site.get("timeout", DEFAULT_TIMEOUT)
            
            # 检查是否应该跳过（失败降频）
            if should_skip_site(site, hashes):
                print(f"[{i+1}/{len(sites)}] {name} ⏭️ 跳过（降频中）")
                skipped_sites.append(name)
                continue
            
            old_data = hashes.get(sid, {})
            old_articles = old_data.get("articles", [])
            old_titles = {a["title"] for a in old_articles}

            print(f"[{i+1}/{len(sites)}] {name} ({url})", end=" ", flush=True)

            # 记录开始时间
            start_time = time.time()
            
            # 抓取
            if use_js:
                content = fetch_with_playwright(url, browser=browser, timeout_ms=timeout)
            else:
                content = fetch_with_requests(url)
            
            sites_processed += 1
            fetch_time = time.time() - start_time

            if not content:
                print("❌ 抓取失败")
                fail_count = update_fail_count(hashes, sid, False)
                results.append({"name": name, "ok": False, "fail_count": fail_count})
                performance_stats.append({"name": name, "time": fetch_time, "ok": False})
                time.sleep(1)
                continue

            # 解析
            if parser_name in PARSERS:
                parser = PARSERS[parser_name]
                if callable(parser):
                    articles = parser(content, url)
                else:
                    articles = parser(content, url).parse()
            else:
                articles = extract_articles(content, url)[:20]
            
            # 去重
            articles = deduplicate_items(articles)
            
            new_items = [a for a in articles if a["title"] not in old_titles]

            # 更新 hash 存储
            new_hash = content_hash(content)
            fail_count = update_fail_count(hashes, sid, True)
            hashes[sid] = {
                "hash": new_hash,
                "articles": articles[:MAX_ARTICLES_PER_SITE],
                "time": datetime.now().isoformat(),
                "fail_count": fail_count,
            }

            if new_items:
                print(f"✅ {len(new_items)} 条新内容 ({fetch_time:.1f}s)")
                for a in new_items[:3]:
                    print(f"    - {a['title']}")
                all_new_items.extend([{"site": name, **a} for a in new_items[:20]])
            else:
                print(f"⚪ 无新内容 ({len(articles)}篇, {fetch_time:.1f}s)")

            results.append({
                "name": name, 
                "ok": True, 
                "new": len(new_items), 
                "total": len(articles),
                "time": fetch_time
            })
            performance_stats.append({"name": name, "time": fetch_time, "ok": True})
            time.sleep(1.5)
    finally:
        browser.close()
        pw.stop()

    # 保存数据
    save_json(HASH_STORE, hashes)
    
    # 更新趋势
    new_count = sum(r.get("new", 0) for r in results)
    trends = update_trends(trends, results, new_count)
    save_json(TREND_STORE, trends)

    # 汇总
    ok_count = sum(1 for r in results if r.get("ok"))
    err_count = sum(1 for r in results if not r.get("ok"))
    changed = [r["name"] for r in results if r.get("ok") and r.get("new", 0) > 0]

    # 性能统计
    slow_sites = sorted([s for s in performance_stats if s["ok"]], 
                       key=lambda x: x["time"], reverse=True)[:3]

    summary = f"\n{'='*50}\n汇总: {ok_count}/{len(results)} 成功, {err_count} 失败, {new_count} 条新内容"
    if skipped_sites:
        summary += f"\n跳过（降频）: {len(skipped_sites)} 个站点"
    if changed:
        summary += f"\n有更新: {', '.join(changed)}"
    else:
        summary += "\n所有站点无新内容"
    if slow_sites:
        slow_info = ", ".join([f"{s['name']}({s['time']:.1f}s)" for s in slow_sites])
        summary += f"\n最慢站点: {slow_info}"
    print(summary)

    # 检查告警
    alert_sites = get_alert_sites(hashes)
    if alert_sites:
        print(f"\n⚠️ 告警: {len(alert_sites)} 个站点连续失败 {ALERT_FAIL_COUNT} 次")
        alert_body = f"以下站点连续失败 {ALERT_FAIL_COUNT} 次，请检查：\n\n"
        for s in alert_sites:
            alert_body += f"- 站点ID: {s['id']}, 失败次数: {s['fail_count']}\n"
        alert_body += f"\n时间: {datetime.now():%Y-%m-%d %H:%M:%S}"
        try:
            to = [RECEIVER_EMAIL] if RECEIVER_EMAIL else ["mrjin2004@163.com"]
            clawemail_send(to, f"⚠️ 线报监控告警 - {len(alert_sites)}个站点异常", alert_body, is_html=False)
            print("✅ 告警邮件已发送")
        except Exception as e:
            print(f"❌ 告警邮件发送失败: {e}")

    # 更新 Gist
    update_gist(results, all_new_items, ok_count, err_count, len(results), trends, slow_sites, skipped_sites)

    # 发送邮件
    if all_new_items:
        print("\n📧 准备发送邮件通知...")
        grouped = group_items_by_site(results, all_new_items)
        text_body = build_email_text(grouped, changed)
        try:
            to = [RECEIVER_EMAIL] if RECEIVER_EMAIL else ["mrjin2004@163.com"]
            clawemail_send(to, f"🔔 线报监控 - {new_count}条新内容 ({datetime.now():%m/%d %H:%M})", text_body, is_html=False)
            print("✅ 邮件发送成功")
        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
    else:
        print("\n📭 无新内容，不发送邮件")


def build_email_text(all_items_by_site, changed_sites):
    """生成纯文本格式邮件"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"线报监控报告 ({now_str})",
        f"共 {len(changed_sites)} 个站点有更新",
        f"{'='*40}",
        "",
    ]
    for site_name, items in all_items_by_site:
        count = len(items)
        lines.append(f"【{site_name}】 {count} 条新内容")
        for i, item in enumerate(items[:20], 1):
            lines.append(f" {i}. {item['title']}")
            lines.append(f"    链接: {item['url']}")
        if items:
            m = re.match(r'https?://([^/]+)', items[0]['url'])
            if m:
                lines.append(f" 站点: https://{m.group(1)}/")
        lines.append("")

    lines.append(f"{'='*40}")
    lines.append("由线报监控系统自动发送")
    return "\n".join(lines)


def group_items_by_site(results, all_new_items):
    """按站点分组"""
    changed_names = {r["name"] for r in results if r.get("ok") and r.get("new", 0) > 0}
    site_items = {}
    for item in all_new_items:
        sn = item["site"]
        if sn not in site_items:
            site_items[sn] = []
        site_items[sn].append(item)
    return [(name, site_items.get(name, [])) for name in changed_names]


# ====== Gist 更新 ======
def update_gist(results, new_items, ok, err, total, trends, slow_sites, skipped_sites):
    """更新 Gist 为最新的监控状态摘要"""
    if not GIST_TOKEN or not GIST_ID:
        print("[INFO] 未配置 GIST_TOKEN 或 GIST_ID，跳过 Gist 更新")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = [r["name"] for r in results if r.get("ok") and r.get("new", 0) > 0]
    new_count = sum(r.get("new", 0) for r in results)

    lines = [
        f"# 🔔 线报监控状态",
        f"",
        f"⏰ 更新时间: {now}",
        f"",
        f"---",
        f"",
        f"## 📊 汇总",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 监控站点 | {total} |",
        f"| 成功 | {ok} |",
        f"| 失败 | {err} |",
        f"| 新内容 | {new_count} |",
    ]
    
    if skipped_sites:
        lines.append(f"| 跳过（降频） | {len(skipped_sites)} |")
    
    lines.append("")

    # 7 天趋势
    if trends:
        lines.append(f"## 📈 近 7 天趋势")
        lines.append("")
        lines.append(f"| 日期 | 新内容 | 成功 | 失败 |")
        lines.append(f"|------|--------|------|------|")
        for date in sorted(trends.keys(), reverse=True)[:7]:
            d = trends[date]
            lines.append(f"| {date} | {d.get('new_count', 0)} | {d.get('success', 0)} | {d.get('failed', 0)} |")
        lines.append("")

    if changed:
        lines.append(f"## 🔥 有更新的站点")
        lines.append("")
        for name in changed:
            lines.append(f"- **{name}**")
        lines.append("")

    if new_items:
        lines.append(f"## 📰 最新内容")
        lines.append("")
        for item in new_items[:20]:
            lines.append(f"- [{item['title']}]({item['url']})  _{item['site']}_")
        lines.append("")

    # 各站点状态
    lines.append(f"## 📋 各站点状态")
    lines.append("")
    lines.append(f"| 站点 | 状态 | 新内容 | 文章数 | 耗时 |")
    lines.append(f"|------|------|--------|--------|------|")
    for r in results:
        status = "✅" if r.get("ok") else "❌"
        new = r.get("new", 0)
        total_articles = r.get("total", "-")
        time_str = f"{r.get('time', 0):.1f}s" if r.get("ok") else "-"
        lines.append(f"| {r['name']} | {status} | {new} | {total_articles} | {time_str} |")
    lines.append("")
    
    # 最慢站点
    if slow_sites:
        lines.append(f"## 🐢 最慢站点")
        lines.append("")
        for s in slow_sites:
            lines.append(f"- {s['name']}: {s['time']:.1f}s")
        lines.append("")

    lines.append(f"---")
    lines.append(f"_🤖 GitHub Actions 自动监控 v2.0_")

    content = "\n".join(lines)

    try:
        resp = req.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"Bearer {GIST_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "description": f"线报监控状态 - {now}",
                "files": {
                    "xianbao-report.md": {"content": content}
                },
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            gist_url = f"https://gist.github.com/{GIST_ID}"
            print(f"✅ Gist 更新成功: {gist_url}")
        else:
            print(f"❌ Gist 更新失败: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Gist 更新异常: {e}")


if __name__ == "__main__":
    main()
