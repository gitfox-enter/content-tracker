#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线报监控脚本 - GitHub Actions 版
- Playwright 抓取页面内容
- 内置 clawemail HTTP API 发邮件（无需 Node.js SDK）
- hashes.json 持久化到 Git 仓库
"""

import os
import sys
import json
import hashlib
import time
import re
from datetime import datetime
from pathlib import Path

# ====== 配置 ======
SCRIPT_DIR = Path(__file__).parent
SITES_CONFIG = SCRIPT_DIR / "sites.json"
HASH_STORE = SCRIPT_DIR / "hashes.json"

# 邮件配置从环境变量读取（GitHub Secrets）
CLAWEMAIL_API_KEY = os.environ.get("CLAWEMAIL_API_KEY", "")
CLAWEMAIL_USER = os.environ.get("CLAWEMAIL_USER", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")

# Gist 配置（用于手机查看监控状态）
GIST_ID = os.environ.get("GIST_ID", "70d680cef95274df9994a33dd3a65246")
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


def clawemail_send(to_list, subject, body_html):
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
        "isHtml": True,
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
def fetch_with_playwright(url):
    """用 Playwright 渲染页面，返回 HTML"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(20000)
        try:
            page.goto(url, wait_until="networkidle", timeout=20000)
            time.sleep(1.5)
            return page.content()
        except Exception as e:
            print(f"  Playwright 超时/错误: {e}")
            return None
        finally:
            browser.close()


def fetch_with_requests(url):
    """备用：纯 requests 抓取（无法渲染 JS）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        r = req.get(url, timeout=12, headers=headers, allow_redirects=True, verify=False)
        return r.text
    except Exception as e:
        print(f"  requests 错误: {e}")
        return None


# ====== 内容提取 ======
def extract_articles(content, base_url=""):
    """从 HTML 提取文章链接和标题"""
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
        if any(x in href.lower() for x in ['login', 'register', 'about', 'contact', 'javascript', '#', 'mailto']):
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
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 线报监控启动")
    print(f"Python: {sys.version}")

    # 加载站点
    if not SITES_CONFIG.exists():
        print(f"[ERROR] 找不到站点配置: {SITES_CONFIG}")
        sys.exit(1)
    with open(SITES_CONFIG, 'r', encoding='utf-8') as f:
        config = json.load(f)
    sites = config.get("sites", [])
    print(f"共 {len(sites)} 个站点待监控\n")

    # 加载历史 hash
    hashes = load_json(HASH_STORE)
    results = []
    all_new_items = []

    for i, site in enumerate(sites):
        sid = str(site["id"])
        name = site["name"]
        url = site["url"]
        use_js = site.get("js", True)  # 默认用 Playwright
        old_data = hashes.get(sid, {})
        old_articles = old_data.get("articles", [])
        old_titles = {a["title"] for a in old_articles}

        print(f"[{i+1}/{len(sites)}] {name} ({url})", end=" ", flush=True)

        # 抓取
        if use_js:
            content = fetch_with_playwright(url)
        else:
            content = fetch_with_requests(url)

        if not content:
            print("❌ 抓取失败")
            results.append({"name": name, "ok": False})
            time.sleep(1)
            continue

        # 提取文章
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
            all_new_items.extend([{"site": name, **a} for a in new_items[:5]])
        else:
            print(f"⚪ 无新内容 ({len(articles)}篇)")

        results.append({"name": name, "ok": True, "new": len(new_items), "total": len(articles)})
        time.sleep(1.5)

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
        html = build_email_html(all_new_items, ok_count, err_count, len(sites))
        try:
            to = [RECEIVER_EMAIL] if RECEIVER_EMAIL else ["mrjin2004@163.com"]
            clawemail_send(to, f"🔔 线报监控 - {new_count}条新内容 ({datetime.now():%m/%d %H:%M})", html)
            print("✅ 邮件发送成功")
        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
    else:
        print("\n📭 无新内容，不发送邮件")


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


# ====== Gist 更新 ======
def update_gist(results, new_items, ok, err, total):
    """更新 Gist 为最新的监控状态摘要（手机随时可查）"""
    if not GIST_TOKEN:
        print("[INFO] 未配置 GIST_TOKEN，跳过 Gist 更新")
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
