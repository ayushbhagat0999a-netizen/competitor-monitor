# Competitor Monitor

Enter a domain, get new blog posts (title, URL, date, meta description), target keyword + search volume, pricing-change detection, and a Slack alert. Scraper is custom (requests + BeautifulSoup); no scraping API.

## How it works
1. **Discovery**: robots.txt/sitemap(s) (recursive), RSS/Atom feeds, homepage and `/blog`-style listing pages.
2. **Parsing**: each post page → title (og/h1/title), date (og, JSON-LD, `<time>`, sitemap/feed fallback), meta description.
3. **Dedupe**: SQLite (`posts.url` is unique). Run 1 stores a baseline; later runs report only unseen URLs.
4. **Enrichment**: target keyword from meta keywords or cleaned title; volume from Keywords Everywhere or DataForSEO.
5. **Pricing**: hashes the set of prices found on `/pricing` or `/plans`; reports added/removed prices.
6. **Alert**: Slack incoming webhook.

## Run locally
    pip install -r requirements.txt
    export KE_API_KEY=...            # or DFS_LOGIN=... DFS_PASSWORD=...
    export SLACK_WEBHOOK_URL=...     # optional; can also be pasted in the UI
    python app.py                    # http://localhost:5000

## Env vars
KE_API_KEY | DFS_LOGIN, DFS_PASSWORD | SLACK_WEBHOOK_URL | DB_PATH (default monitor.db) | MAX_POSTS (default 25 fetched per run)

## Deploy (Render)
New Web Service from the repo, build `pip install -r requirements.txt`, start `gunicorn app:app --timeout 120`. Add a persistent disk and set `DB_PATH=/data/monitor.db`, otherwise the DB resets on redeploy and run #2 repeats everything.

## Failure handling
- Blocked (403/429/captcha) → status `blocked`, clear message, nothing stored.
- No blog → status `no_blog_found`.
- Bad/missing keyword API key → posts still saved and alerted, volume shown as n/a with the error.
- Bad Slack webhook → reported in the response, scrape result unaffected.

## API
`POST /api/check {"domain": "example.com", "slack_webhook": "..."}` | `GET /api/posts?domain=example.com`
Call `/api/check` from cron (or GitHub Actions) for weekly monitoring.

## Known limits
JS-rendered blogs/pricing pages with no sitemap or feed won't be found (a headless browser such as Playwright would be the next step). Keyword is a heuristic, not the page's true ranking keyword.
