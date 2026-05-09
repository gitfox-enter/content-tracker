# Content Tracker - Project Documentation

## Overview

A web content monitoring tool that tracks changes on configured websites and sends email notifications when new content is detected.

## Repository

- **URL**: https://github.com/gitfox-enter/content-tracker
- **Visibility**: Public (GitHub Actions unlimited free minutes)
- **Local Path**: `C:\Users\ASUS1\.qclaw\workspace\xianbao-monitor\`

## Architecture

```
├── monitor.py           # Main monitoring script
├── requirements.txt     # Python dependencies
├── hashes.json          # Historical content hashes (auto-updated)
├── .github/workflows/
│   └── monitor.yml      # GitHub Actions workflow
└── PROJECT.md           # This file
```

## How It Works

1. GitHub Actions runs every 30 minutes (batch 1 + batch 2)
2. Playwright renders JavaScript-heavy pages
3. Content hash comparison detects new articles
4. Email notification via clawemail API
5. Gist page for mobile status viewing

## GitHub Secrets Configuration

Required secrets (Settings → Secrets and variables → Actions):

| Secret | Description |
|--------|-------------|
| `SITES_CONFIG` | JSON array of websites to monitor |
| `CLAWEMAIL_API_KEY` | Email service API key |
| `CLAWEMAIL_USER` | Sender email account |
| `RECEIVER_EMAIL` | Recipient email address |
| `GIST_TOKEN` | GitHub token for Gist updates |
| `GIST_ID` | Gist ID for status page |

## SITES_CONFIG Format

```json
{
  "sites": [
    {
      "id": 1,
      "name": "Site Name",
      "url": "https://example.com",
      "js": true,
      "parser": "custom_parser_name",
      "timeout": 25000
    }
  ]
}
```

Fields:
- `id`: Unique identifier
- `name`: Display name
- `url`: Website URL
- `js`: Whether to use Playwright (true) or requests (false)
- `parser`: Optional custom parser (epic, steam, gog, foxirj, etc.)
- `timeout`: Custom timeout in milliseconds (default: 25000)

## Custom Parsers

Built-in parsers for specific sites:
- `epic` - Epic Games free games
- `steam` - Steam free games
- `gog` - GOG free games
- `foxirj` - 佛系软件
- `down423` - 423Down
- `ghxi` - 果核剥壳
- `baicaio` - 白菜哦
- `indiegame` - IndieGamePlus
- `haoyangmao` - 好羊毛

## Optimizations

- **Retry mechanism**: Failed fetches retry up to 2 times
- **Browser restart**: Browser restarts every 10 sites to prevent memory leaks
- **Batch processing**: Sites split into 2 batches for reliability
- **Shared browser**: All sites share one browser instance per batch

## Workflow Schedule

```yaml
schedule:
  - cron: '0,30 * * * *'  # Every 30 minutes
```

Each run executes:
- Batch 1: First half of sites
- Batch 2: Second half of sites

## Manual Trigger

Go to Actions → "Content Monitor" → "Run workflow" → Select batch or run both

## Email Format

Plain text, grouped by site, includes:
- Site name
- Article titles
- Article URLs

## Gist Status Page

A public Gist page showing:
- Last update time
- Success/failure count
- Sites with new content
- Latest articles

## Notes

- Sites config is stored in Secrets for privacy
- Public repo = unlimited GitHub Actions minutes
- No sensitive data in repository
