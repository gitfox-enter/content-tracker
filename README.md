# 线报监控 - GitHub Actions 版

自动监控 26 个线报网站，发现新内容时发送邮件通知。

## 工作原理

1. GitHub Actions 每 30 分钟自动运行
2. 用 Playwright 渲染页面，提取文章链接
3. 对比历史 hashes.json，检测新内容
4. 有新内容时，通过 clawemail API 发邮件通知

## 一次性设置

### 1. 创建 GitHub 私有仓库

```bash
# 在 GitHub 上创建私有仓库，然后：
cd xianbao-github
git init
git add .
git commit -m "🚀 线报监控初始化"
git remote add origin https://github.com/你的用户名/xianbao-monitor.git
git push -u origin main
```

### 2. 配置 GitHub Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名 | 值 | 说明 |
|-----------|-----|------|
| `CLAWEMAIL_API_KEY` | `ck_live_049108b6...` | clawemail API 密钥 |
| `CLAWEMAIL_USER` | `qaidaily@claw.163.com` | 发件邮箱 |
| `RECEIVER_EMAIL` | `mrjin2004@163.com` | 收件邮箱 |

### 3. 测试运行

在仓库 Actions 页面点击 "Run workflow" 手动触发一次。

## 文件说明

- `monitor.py` — 主脚本（Playwright + clawemail HTTP API）
- `sites.json` — 监控站点列表
- `hashes.json` — 历史数据（自动更新）
- `.github/workflows/monitor.yml` — GitHub Actions 定时任务

## 注意事项

- GitHub Actions 服务器在海外，部分国内小站可能访问较慢或失败
- GitHub cron 最小间隔约 5 分钟，实际触发可能有几分钟延迟
- 每月 2000 分钟免费额度，每次运行约 3-5 分钟，绰绰有余
- hashes.json 通过 git commit 持久化，无需额外存储
