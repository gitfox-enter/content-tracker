# Web Content Monitor

A lightweight web content monitoring tool that tracks changes on configured websites and sends notifications when new content is detected.

## Features

- Automated monitoring via GitHub Actions
- Playwright-based rendering for JavaScript-heavy sites
- Content hash comparison for change detection
- Email notifications for new content
- Configurable monitoring targets

## Setup

### 1. Configure Secrets

Add the following secrets in your repository settings (Settings → Secrets and variables → Actions):

| Secret | Description |
|--------|-------------|
| `SITES_CONFIG` | JSON configuration of sites to monitor |
| `CLAWEMAIL_API_KEY` | Email service API key |
| `CLAWEMAIL_USER` | Email sender account |
| `RECEIVER_EMAIL` | Email recipient address |
| `GIST_TOKEN` | GitHub token for Gist updates (optional) |
| `GIST_ID` | Gist ID for status page (optional) |

### 2. SITES_CONFIG Format

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

### 3. Run

The workflow runs automatically on schedule. You can also trigger it manually from the Actions tab.

## Files

- `monitor.py` - Main monitoring script
- `requirements.txt` - Python dependencies
- `.github/workflows/monitor.yml` - GitHub Actions workflow

## Notes

- Sites configuration is stored in GitHub Secrets for privacy
- Content hashes are persisted via git commits
- Supports both static and JavaScript-rendered pages
