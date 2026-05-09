#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线报监控脚本 - GitHub Actions 版
- Playwright 抓取页面内容
- 支持自定义解析器（Epic、Steam、GOG 等）
- 内置 clawemail HTTP API 发邮件
- hashes.json 持久化到 Git 仓库
"""

import os
import sys
import json
import hashlib
import time
import re
import argparse
from datetime import datetime
from pathlib import Path

# ====== 配置 ======
SCRIPT_DIR = Path(__file__).parent
SITES_CONFIG = SCRIPT_DIR / "sites.json"
HASH_STORE = SCRIPT_DIR / "hashes.json"

# 默认超时时间（毫秒）
DEFAULT_TIMEOUT = 25000

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
    # Step 1: compose continue
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

    # Step 2: compose deliver
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


# ====== 专门解析器 ======
def parse_epic(content, base_url):
    """Epic Games 免费游戏解析器"""
    articles = []
    if not content:
        return articles
    
    # Epic 页面是 React 渲染，数据在 __NEXT_DATA__ 中
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
    if match:
        try:
            import json
            data = json.loads(match.group(1))
            # 尝试从各种可能的位置提取免费游戏
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
    
    # 备用：正则提取
    if not articles:
        pattern = r'href="(/zh-CN/p/[^"]+)"[^>]*>.*?<span[^>]*>([^<]{3,50})</span>'
        for m in re.finditer(pattern, content, re.DOTALL):
            href, title = m.groups()
            if "free" in title.lower() or "免费" in title:
                articles.append({"title": title.strip(), "url": f"https://store.epicgames.com{href}"})
    
    return articles[:10]


def parse_steam(content, base_url):
    """Steam 免费游戏解析器"""
    articles = []
    if not content:
        return articles
    
    # Steam 商店页面结构
    pattern = r'<a[^>]+href="(https://store\.steampowered\.com/app/\d+/[^"]+/)"[^>]*>.*?<span[^>]*class="title"[^>]*>([^<]+)</span>'
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        if len(title) >= 2:
            articles.append({"title": f"[Steam免费] {title}", "url": url})
    
    # 备用模式
    if not articles:
        pattern2 = r'<a[^>]+href="(https://store\.steampowered\.com/app/\d+[^"]*)"[^>]*>([^<]{3,80})</a>'
        for m in re.finditer(pattern2, content, re.DOTALL):
            url, title = m.groups()
            title = title.strip()
            if "免费" in title or "Free" in title.lower() or len(title) < 50:
                articles.append({"title": title, "url": url})
    
    return articles[:15]


def parse_gog(content, base_url):
    """GOG 免费游戏解析器"""
    articles = []
    if not content:
        return articles
    
    # GOG 页面结构
    pattern = r'<a[^>]+href="(https://www\.gog\.com/[^"]+)"[^>]*>.*?class="product-tile__title"[^>]*>([^<]+)</span>'
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        if len(title) >= 2:
            articles.append({"title": f"[GOG免费] {title}", "url": url})
    
    # 备用模式
    if not articles:
        pattern2 = r'href="(https://www\.gog\.com/[^"]+game[^"]*)"[^>]*>([^<]{3,60})</a>'
        for m in re.finditer(pattern2, content, re.DOTALL):
            url, title = m.groups()
            articles.append({"title": title.strip(), "url": url})
    
    return articles[:15]


def parse_foxirj(content, base_url):
    """佛系软件解析器"""
    articles = []
    if not content:
        return articles
    
    # 佛系软件页面结构
    pattern = r'<a[^>]+href="(https://foxirj\.com/[^"]+)"[^>]*>.*?<h[23][^>]*>([^<]+)</h[23]>'
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        if len(title) >= 3 and "页面" not in title:
            articles.append({"title": title, "url": url})
    
    # 备用通用提取
    if not articles:
        articles = extract_articles(content, base_url)
    
    return articles[:15]


def parse_haoyangmao(content, base_url):
    """好羊毛解析器 - 过滤 Cloudflare 错误页"""
    articles = []
    if not content:
        return articles
    
    # 检测是否是 Cloudflare 错误页
    if 'cloudflare' in content.lower() and '5xx-error' in content.lower():
        print("  检测到 Cloudflare 错误页，跳过")
        return []
    
    # 提取文章链接
    pattern = r'<a[^>]+href="(https?://www\.haoyangmao123\.com/[^"]+)"[^>]*>([^<]{6,80})</a>'
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        # 过滤导航链接
        if any(x in title for x in ['首页', '登录', '注册', '更多', '关于']):
            continue
        if len(title) >= 6:
            articles.append({"title": title, "url": url})
    
    return articles[:15]


def parse_down423(content, base_url):
    """423Down 解析器 - 提取软件文章"""
    articles = []
    if not content:
        return articles
    
    # 423Down 文章结构 - 提取带日期的文章
    pattern = r'<a[^>]+href="(https://www\.423down\.com/\d+\.html)"[^>]*>([^<]+)</a>'
    seen_urls = set()
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        # 只保留文章链接（包含数字ID）
        if '/system.html' in url or '/win11' in url or '/win10' in url or '/win7' in url:
            continue
        if url in seen_urls:
            continue
        if len(title) >= 3 and len(title) <= 80:
            seen_urls.add(url)
            articles.append({"title": title, "url": url})
    
    return articles[:20]


def parse_ghxi(content, base_url):
    """果核剥壳解析器 - 从分类页提取"""
    articles = []
    if not content:
        return articles
    
    # 果核剥壳文章结构
    pattern = r'<a[^>]+href="(https://www\.ghxi\.com/[^"]+\.html)"[^>]*>([^<]+)</a>'
    seen_urls = set()
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        # 过滤非文章链接
        if any(x in url for x in ['/category/', '/tag/', '/page/', '/author/']):
            continue
        if url in seen_urls:
            continue
        if len(title) >= 3 and len(title) <= 80:
            seen_urls.add(url)
            articles.append({"title": title, "url": url})
    
    return articles[:20]


def parse_baicaio(content, base_url):
    """白菜哦解析器"""
    articles = []
    if not content:
        return articles
    
    # 白菜哦文章结构
    pattern = r'<a[^>]+href="(https://www\.baicaio\.com/[^"]+\.html)"[^>]*>([^<]+)</a>'
    seen_urls = set()
    for m in re.finditer(pattern, content, re.DOTALL):
        url, title = m.groups()
        title = title.strip()
        if url in seen_urls:
            continue
        if len(title) >= 6 and len(title) <= 80:
            seen_urls.add(url)
            articles.append({"title": title, "url": url})
    
    return articles[:20]


def parse_indiegame(content, base_url):
    """IndieGamePlus 解析器 - 提取独立游戏"""
    articles = []
    if not content:
        return articles
    
    # 提取游戏相关链接
    patterns = [
        r'<a[^>]+href="(https://indiegameplus\.com/[^"]+)"[^>]*>([^<]{5,80})</a>',
        r'<a[^>]+href="(https://[^"]*game[^"]*)"[^>]*>([^<]{5,80})</a>',
    ]
    
    seen_urls = set()
    for pattern in patterns:
        for m in re.finditer(pattern, content, re.DOTALL):
            url, title = m.groups()
            title = title.strip()
            # 过滤无关链接
            if any(x in url.lower() for x in ['wix.com', 'template', 'facebook', 'twitter', 'instagram']):
                continue
            if url in seen_urls:
                continue
            if len(title) >= 5 and len(title) <= 80:
                seen_urls.add(url)
                articles.append({"title": title, "url": url})
    
    return articles[:15]


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
        # 过滤无关链接
        if any(x in href.lower() for x in ['login', 'register', 'about', 'contact', 'javascript', '#', 'mailto', 'search', 'tag/', 'category/', 'page/', 'author/']):
            continue
        # 过滤无关标题
        if any(x in title for x in ['登录', '注册', '搜索', '更多', '返回', '首页', '下一页', '上一页', '加载', '展开']):
            continue
        # 补全相对链接
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


def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='线报监控脚本')
    parser.add_argument('--batch', type=int, choices=[1, 2], default=None,
                        help='批次号 (1 或 2)，不指定则跑全部站点')
    args = parser.parse_args()
    
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 线报监控启动")
    print(f"Python: {sys.version}")
    if args.batch:
        print(f"批次: {args.batch}")

    # 加载站点配置（优先从环境变量，其次从文件）
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
        print(f"[ERROR] 未找到站点配置（环境变量 SITES_CONFIG 或文件 {SITES_CONFIG}）")
        sys.exit(1)
    
    # 分批处理：每批约一半站点
    if args.batch:
        batch_size = (len(all_sites) + 1) // 2  # 向上取整
        if args.batch == 1:
            sites = all_sites[:batch_size]
        else:
            sites = all_sites[batch_size:]
        print(f"共 {len(all_sites)} 个站点，本批次 {len(sites)} 个\n")
    else:
        sites = all_sites
        print(f"共 {len(sites)} 个站点待监控\n")

    # 加载历史 hash
    hashes = load_json(HASH_STORE)
    results = []
    all_new_items = []

    # 启动共享浏览器实例
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    browser_restart_interval = 10  # 每 10 个站点重启浏览器
    sites_processed = 0
    
    try:
        for i, site in enumerate(sites):
            # 每 N 个站点重启浏览器，防止内存泄漏
            if sites_processed > 0 and sites_processed % browser_restart_interval == 0:
                print(f"\n🔄 已处理 {sites_processed} 个站点，重启浏览器...")
                browser.close()
                browser = pw.chromium.launch(headless=True)
            
            sid = str(site["id"])
            name = site["name"]
            url = site["url"]
            use_js = site.get("js", True)
            parser = site.get("parser", "")  # 自定义解析器
            timeout = site.get("timeout", DEFAULT_TIMEOUT)  # 自定义超时
            old_data = hashes.get(sid, {})
            old_articles = old_data.get("articles", [])
            old_titles = {a["title"] for a in old_articles}

            print(f"[{i+1}/{len(sites)}] {name} ({url})", end=" ", flush=True)

            # 抓取
            if use_js:
                content = fetch_with_playwright(url, browser=browser, timeout_ms=timeout)
            else:
                content = fetch_with_requests(url)
            
            sites_processed += 1

            if not content:
                print("❌ 抓取失败")
                results.append({"name": name, "ok": False})
                time.sleep(1)
                continue

            # 根据解析器类型提取文章
            if parser == "epic":
                articles = parse_epic(content, url)
            elif parser == "steam":
                articles = parse_steam(content, url)
            elif parser == "gog":
                articles = parse_gog(content, url)
            elif parser == "foxirj":
                articles = parse_foxirj(content, url)
            elif parser == "haoyangmao":
                articles = parse_haoyangmao(content, url)
            elif parser == "down423":
                articles = parse_down423(content, url)
            elif parser == "ghxi":
                articles = parse_ghxi(content, url)
            elif parser == "baicaio":
                articles = parse_baicaio(content, url)
            elif parser == "indiegame":
                articles = parse_indiegame(content, url)
            else:
                articles = extract_articles(content, url)[:20]
            
            new_items = [a for a in articles if a["title"] not in old_titles]

            # 更新 hash 存储
            new_hash = content_hash(content)
            hashes[sid] = {
                "hash": new_hash,
                "articles": articles[:20],
                "time": datetime.now().isoformat(),
            }

            if new_items:
                print(f"✅ {len(new_items)} 条新内容")
                for a in new_items[:3]:
                    print(f"    - {a['title']}")
                all_new_items.extend([{"site": name, **a} for a in new_items[:20]])
            else:
                print(f"⚪ 无新内容 ({len(articles)}篇)")

            results.append({"name": name, "ok": True, "new": len(new_items), "total": len(articles)})
            time.sleep(1.5)
    finally:
        # 关闭浏览器
        browser.close()
        pw.stop()

    # 保存 hash
    save_json(HASH_STORE, hashes)

    # 汇总
    ok_count = sum(1 for r in results if r.get("ok"))
    err_count = sum(1 for r in results if not r.get("ok"))
    new_count = sum(r.get("new", 0) for r in results)
    changed = [r["name"] for r in results if r.get("ok") and r.get("new", 0) > 0]

    summary = f"\n{'='*50}\n汇总: {ok_count}/{len(sites)} 成功, {err_count} 失败, {new_count} 条新内容"
    if changed:
        summary += f"\n有更新: {', '.join(changed)}"
    else:
        summary += "\n所有站点无新内容"
    print(summary)

    # 更新 Gist（每次运行都更新，让手机随时可查最新状态）
    update_gist(results, all_new_items, ok_count, err_count, len(sites))

    # 发邮件（仅有新内容时）
    if all_new_items:
        print("\n📧 准备发送邮件通知...")
        # 纯文本格式（用户偏好格式）
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
    """生成纯文本格式邮件，按站点分组，每站点最多20条"""
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
        # 站点总链接（取第一个的域名作站点首页）
        if items:
            import re as _re
            m = _re.match(r'https?://([^/]+)', items[0]['url'])
            if m:
                lines.append(f" 站点: https://{m.group(1)}/")
        lines.append("")

    lines.append(f"{'='*40}")
    lines.append("由线报监控系统自动发送")
    return "\n".join(lines)


def build_email_html(items, ok, err, total):
    """生成邮件 HTML"""
    rows = ""
    for item in items:
        rows += f"""
        <tr>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:#666;">{item['site']}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;">
                <a href="{item['url']}" style="color:#1a73e8;text-decoration:none;">{item['title']}</a>
            </td>
        </tr>"""
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;">
        <div style="background:#1a73e8;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;font-size:18px;">🔔 线报监控通知</h2>
        </div>
        <div style="background:#f8f9fa;padding:12px 20px;border-left:1px solid #ddd;border-right:1px solid #ddd;">
            <span style="color:#333;">✅ {ok}/{total} 成功</span> &nbsp;
            <span style="color:#999;">❌ {err} 失败</span> &nbsp;
            <span style="color:#1a73e8;font-weight:bold;">🔥 {len(items)} 条新内容</span>
        </div>
        <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #ddd;">
            <tr style="background:#f0f0f0;">
                <th style="padding:8px 10px;text-align:left;font-size:13px;color:#555;">站点</th>
                <th style="padding:8px 10px;text-align:left;font-size:13px;color:#555;">标题</th>
            </tr>
            {rows}
        </table>
        <div style="color:#999;font-size:12px;padding:10px 20px;text-align:center;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;">
            🤖 GitHub Actions 自动监控 · {datetime.now():%Y-%m-%d %H:%M}<br>
            📱 <a href="https://gist.github.com/gitfox-enter/70d680cef95274df9994a33dd3a65246" style="color:#1a73e8;">点击查看完整监控状态</a> · 回复本邮件可查最近历史
        </div>
    </div>"""


def group_items_by_site(results, all_new_items):
    """按站点分组，返回 [(site_name, [items]), ...] 按有更新的站点顺序"""
    changed_names = {r["name"] for r in results if r.get("ok") and r.get("new", 0) > 0}
    site_items = {}
    for item in all_new_items:
        sn = item["site"]
        if sn not in site_items:
            site_items[sn] = []
        site_items[sn].append(item)
    return [(name, site_items.get(name, [])) for name in changed_names]



# ====== Gist 更新 ======
def update_gist(results, new_items, ok, err, total):
    """更新 Gist 为最新的监控状态摘要（手机随时可查）"""
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
        f"",
    ]

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
    lines.append(f"| 站点 | 状态 | 新内容 | 文章数 |")
    lines.append(f"|------|------|--------|--------|")
    for r in results:
        status = "✅" if r.get("ok") else "❌"
        new = r.get("new", 0)
        total_articles = r.get("total", "-")
        lines.append(f"| {r['name']} | {status} | {new} | {total_articles} |")
    lines.append("")
    lines.append(f"---")
    lines.append(f"_🤖 GitHub Actions 自动监控_")

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
