# Web Content Monitor

A lightweight web content monitoring tool that tracks changes on configured websites and sends notifications when new content is detected.

## Features

- Automated monitoring via GitHub Actions
- Playwright-based rendering for JavaScript-heavy sites
- Content hash comparison for change detection
- Email notifications for new content
- Configurable monitoring targets

## Setup

### 1. Configure Sites

Edit `sites.json` in the repository root to add or modify monitoring targets:

```json
{
  "sites": [
    {
      "id": 1,
      "name": "Site Name",
      "url": "https://example.com",
      "js": true,
      "parser": "custom_parser_name"
    }
  ]
}
```

**Fields:**
- `id`: Unique identifier (integer)
- `name`: Display name
- `url`: Website URL
- `js`: Use Playwright for JavaScript rendering (true/false)
- `parser`: Optional custom parser name
- `timeout`: Optional timeout in milliseconds (default: 25000)

### 2. Configure Secrets

Add the following secrets in your repository settings (Settings → Secrets and variables → Actions):

| Secret | Description |
|--------|-------------|
| `CLAWEMAIL_API_KEY` | Email service API key |
| `CLAWEMAIL_USER` | Email sender account |
| `RECEIVER_EMAIL` | Email recipient address |
| `GIST_TOKEN` | GitHub token for Gist updates (optional) |
| `GIST_ID` | Gist ID for status page (optional) |

### 3. Run

The workflow runs automatically on schedule. You can also trigger it manually from the Actions tab.

## Files

- `monitor.py` - Main monitoring script
- `sites.json` - Site configuration (45 sites)
- `requirements.txt` - Python dependencies
- `.github/workflows/monitor.yml` - GitHub Actions workflow

## Notes

- Sites configuration is stored in `sites.json` file in the repository
- Content hashes are persisted via git commits
- Supports both static and JavaScript-rendered pages
- Monitors 45 websites across 2 batches
- Runs every 30 minutes via GitHub Actions
