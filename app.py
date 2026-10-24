"""Competitor Monitor: scrape a domain, detect new posts + pricing changes,
enrich with keyword volume, alert via Slack. Own scraper (requests + bs4)."""
import os, re, time, hashlib, sqlite3, datetime as dt
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

DB = os.environ.get("DB_PATH", "monitor.db")
MAX_FETCH = int(os.environ.get("MAX_POSTS", 25))
HINTS = ("blog", "resources", "articles", "article", "news", "insights", "learn", "guides", "stories", "posts")
SKIP = ("tag", "tags", "category", "categories", "page", "author", "authors", "topics", "topic")
STOP = set("a an the of to in for and or on with your you how what why is are be this that from by at as it our we vs".split())
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (compatible; CompetitorMonitorBot/1.0)"
app = Flask(__name__)


class Blocked(Exception):
    pass


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE IF NOT EXISTS posts(url TEXT PRIMARY KEY, domain TEXT, title TEXT, published TEXT,
        description TEXT, keyword TEXT, volume INTEGER, first_seen TEXT, baseline INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS runs(domain TEXT, ts TEXT);
      CREATE TABLE IF NOT EXISTS pricing(domain TEXT, ts TEXT, hash TEXT, prices TEXT);""")
    return c


def get(url, timeout=15):
    try:
        r = S.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise Blocked(f"Could not reach {url} ({e.__class__.__name__})")
    if r.status_code in (401, 403, 429, 503) or (r.status_code != 200 and re.search(r"captcha|cf-chl|just a moment", r.text[:3000], re.I)):
        raise Blocked(f"Site blocked the request (HTTP {r.status_code}). It may use bot protection.")
    return r


def norm(d):
    d = d.strip()
    if not d.startswith("http"):
        d = "https://" + d
    p = urlparse(d)
    return f"{p.scheme}://{p.netloc}"


def iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(s).date().isoformat()
        except Exception:
            return s[:10]


def looks_like_post(url, base):
    p = urlparse(url)
    if p.netloc.replace("www.", "") != urlparse(base).netloc.replace("www.", ""):
        return False
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2 or re.search(r"\.(jpg|jpeg|png|gif|svg|pdf|xml|css|js)$", p.path, re.I):
        return False
    if any(x in SKIP for x in parts) or parts[-1].isdigit():
        return False
    return parts[0] in HINTS or any(x in HINTS for x in parts[:-1])


def sitemap_urls(base):
    seeds = []
    try:
        seeds += re.findall(r"(?im)^sitemap:\s*(\S+)", S.get(base + "/robots.txt", timeout=10).text)
    except requests.RequestException:
        pass
    q, seen, out = list(dict.fromkeys(seeds + [base + "/sitemap.xml", base + "/sitemap_index.xml"])), set(), {}
    while q and len(seen) < 12:
        u = q.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            r = S.get(u, timeout=15)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        x = BeautifulSoup(r.content, "xml")
        for sm in x.find_all("sitemap"):
            loc = sm.find("loc")
            if loc:
                (q.insert if any(h in loc.text.lower() for h in HINTS) else lambda i, v: q.append(v))(0, loc.text.strip())
        for e in x.find_all("url"):
            loc, lm = e.find("loc"), e.find("lastmod")
            if loc:
                out[loc.text.strip()] = iso(lm.text.strip()) if lm else None
    return out


def feed_urls(base, home_soup):
    links = [urljoin(base, l["href"]) for l in home_soup.find_all("link", rel="alternate")
             if l.get("href") and "xml" in (l.get("type") or "")]
    out = {}
    for u in links + [base + "/feed", base + "/rss.xml", base + "/blog/feed", base + "/blog/rss.xml"]:
        try:
            r = S.get(u, timeout=10)
        except requests.RequestException:
            continue
        if r.status_code != 200 or b"<" not in r.content[:50]:
            continue
        x = BeautifulSoup(r.content, "xml")
        for it in x.find_all(["item", "entry"]):
            l = it.find("link")
            href = (l.get("href") or l.text).strip() if l else None
            d = it.find(["pubDate", "published", "updated"])
            if href:
                out[href] = iso(d.text.strip()) if d else None
        if out:
            break
    return out


def anchors(soup, base):
    return {urljoin(base, a["href"]).split("#")[0].split("?")[0] for a in soup.find_all("a", href=True)}


def parse_post(url, lastmod):
    s = BeautifulSoup(get(url).text, "html.parser")

    def meta(*names):
        for n in names:
            t = s.find("meta", attrs={"property": n}) or s.find("meta", attrs={"name": n})
            if t and t.get("content"):
                return t["content"].strip()

    title = meta("og:title", "twitter:title") or (s.h1.get_text(" ", strip=True) if s.h1 else None) \
        or (s.title.get_text(strip=True) if s.title else url)
    date = meta("article:published_time", "datePublished", "date", "pubdate")
    if not date:
        for sc in s.find_all("script", type="application/ld+json"):
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', sc.string or "")
            if m:
                date = m.group(1)
                break
    if not date:
        t = s.find("time", attrs={"datetime": True})
        date = t["datetime"] if t else None
    return dict(url=url, title=title, published=iso(date) or lastmod or "",
                description=meta("description", "og:description") or "", keywords=meta("keywords") or "")


def target_keyword(p):
    if p["keywords"]:
        return p["keywords"].split(",")[0].strip().lower()
    t = re.split(r"\s[|\-–—:]\s", p["title"])[0].lower()
    words = re.findall(r"[a-z0-9']+", t)
    return " ".join(([w for w in words if w not in STOP] or words)[:4])


def volumes(kws):
    """Returns ({keyword: monthly_volume}, error_message_or_None)."""
    kws = list(dict.fromkeys(k for k in kws if k))
    if not kws:
        return {}, None
    try:
        if os.environ.get("KE_API_KEY"):  # Keywords Everywhere
            r = requests.post("https://api.keywordseverywhere.com/v1/get_keyword_data",
                              headers={"Authorization": "Bearer " + os.environ["KE_API_KEY"], "Accept": "application/json"},
                              data={"country": "us", "currency": "USD", "dataSource": "cli", "kw[]": kws}, timeout=30)
            if r.status_code in (401, 402, 403):
                return {}, f"Keywords Everywhere rejected the API key or credits (HTTP {r.status_code})"
            r.raise_for_status()
            return {d["keyword"].lower(): d.get("vol") for d in r.json().get("data", [])}, None
        if os.environ.get("DFS_LOGIN"):  # DataForSEO
            r = requests.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
                              auth=(os.environ["DFS_LOGIN"], os.environ.get("DFS_PASSWORD", "")),
                              json=[{"keywords": kws, "location_code": 2840, "language_code": "en"}], timeout=60)
            if r.status_code in (401, 403):
                return {}, "DataForSEO login or password is invalid"
            r.raise_for_status()
            task = r.json()["tasks"][0]
            if task.get("status_code") != 20000:
                return {}, f"DataForSEO error: {task.get('status_message')}"
            return {x["keyword"].lower(): x.get("search_volume") for x in task.get("result") or []}, None
        return {}, "No keyword API key set (KE_API_KEY or DFS_LOGIN/DFS_PASSWORD). Volumes skipped."
    except Exception as e:
        return {}, f"Keyword API failed ({e.__class__.__name__}). Volumes skipped."


def check_pricing(base, c, host):
    r = None
    for path in ("/pricing", "/plans", "/pricing/"):
        try:
            r = S.get(base + path, timeout=15)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            break
        r = None
    if r is None:
        return None
    s = BeautifulSoup(r.text, "html.parser")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    prices = sorted(set(re.findall(r"[$€£₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?/\s?\w+)?", s.get_text(" ", strip=True))))
    h = hashlib.sha1("|".join(prices).encode()).hexdigest()
    last = c.execute("SELECT hash, prices FROM pricing WHERE domain=? ORDER BY ts DESC LIMIT 1", (host,)).fetchone()
    res = dict(url=r.url, prices=prices, status="baseline" if not last else "unchanged")
    if last and last["hash"] != h:
        old = set(last["prices"].split("|")) if last["prices"] else set()
        res.update(status="changed", added=sorted(set(prices) - old), removed=sorted(old - set(prices)))
    if not last or last["hash"] != h:
        c.execute("INSERT INTO pricing VALUES(?,?,?,?)", (host, dt.datetime.utcnow().isoformat(), h, "|".join(prices)))
    return res


def send_slack(hook, host, new, pricing, first):
    if first:
        text = f"*{host}*: baseline created, {len(new)} posts indexed. Future runs will only flag new posts."
    else:
        text = f"*{host} published {len(new)} new post{'s' * (len(new) != 1)}*"
    for p in new[:10]:
        vol = f"{p['volume']:,}/mo" if p.get("volume") is not None else "n/a"
        text += f"\n• <{p['url']}|{p['title']}> ({p['published'] or 'no date'}) | keyword: _{p['keyword']}_ | volume: {vol}"
    if pricing and pricing["status"] == "changed":
        text += f"\n:moneybag: Pricing changed. Added: {', '.join(pricing['added']) or '-'}; removed: {', '.join(pricing['removed']) or '-'}"
    try:
        r = requests.post(hook, json={"text": text}, timeout=10)
        return "sent" if r.status_code == 200 else f"Slack rejected the webhook (HTTP {r.status_code})"
    except requests.RequestException:
        return "Slack webhook unreachable"


def run_check(raw, hook=None):
    base, c = norm(raw), db()
    host = urlparse(base).netloc
    first = not c.execute("SELECT 1 FROM runs WHERE domain=?", (host,)).fetchone()
    out = dict(domain=host, status="ok", errors=[], new=[], first_run=first, pricing=None, alert=None)
    try:
        home = get(base + "/")
    except Blocked as e:
        return {**out, "status": "blocked", "errors": [str(e)]}
    hs = BeautifulSoup(home.text, "html.parser")

    cands = {u: lm for u, lm in sitemap_urls(base).items() if looks_like_post(u, base)}
    for u, lm in feed_urls(base, hs).items():
        if looks_like_post(u, base) or not cands:
            cands.setdefault(u, lm)
    home_links = anchors(hs, base)
    listings = [u for u in home_links if urlparse(u).netloc == urlparse(base).netloc
                and len([x for x in urlparse(u).path.split("/") if x]) == 1
                and urlparse(u).path.strip("/") in HINTS][:3] or [base + "/blog"]
    for u in list(home_links) + [l for lp in listings for l in _listing(lp, base)]:
        if looks_like_post(u, base):
            cands.setdefault(u, None)

    if not cands:
        out["pricing"] = check_pricing(base, c, host)
        c.execute("INSERT INTO runs VALUES(?,?)", (host, dt.datetime.utcnow().isoformat()))
        c.commit()
        return {**out, "status": "no_blog_found", "errors": ["No blog or article URLs found (no sitemap, feed or listing page)."]}

    known = {r[0] for r in c.execute("SELECT url FROM posts WHERE domain=?", (host,))}
    todo = sorted((u for u in cands if u not in known), key=lambda u: cands[u] or "", reverse=True)
    now = dt.datetime.utcnow().isoformat()
    for u in todo[:MAX_FETCH]:
        try:
            out["new"].append(parse_post(u, cands[u]))
        except Blocked as e:
            out["errors"].append(f"{u}: {e}")
        time.sleep(0.3)
    vols, err = volumes([target_keyword(p) for p in out["new"]])
    if err:
        out["errors"].append(err)
    for p in out["new"]:
        p["keyword"] = target_keyword(p)
        p["volume"] = vols.get(p["keyword"])
        p.pop("keywords", None)
        c.execute("INSERT OR IGNORE INTO posts VALUES(?,?,?,?,?,?,?,?,0)",
                  (p["url"], host, p["title"], p["published"], p["description"], p["keyword"], p["volume"], now))
    if first:  # remember older URLs beyond the fetch cap so run #2 doesn't report them
        for u in todo[MAX_FETCH:]:
            c.execute("INSERT OR IGNORE INTO posts VALUES(?,?,?,?,?,?,?,?,1)",
                      (u, host, u.rstrip("/").split("/")[-1].replace("-", " "), cands[u] or "", "", "", None, now))

    out["pricing"] = check_pricing(base, c, host)
    c.execute("INSERT INTO runs VALUES(?,?)", (host, now))
    c.commit()
    hook = hook or os.environ.get("SLACK_WEBHOOK_URL")
    changed = out["pricing"] and out["pricing"]["status"] == "changed"
    if hook and (out["new"] or changed):
        out["alert"] = send_slack(hook, host, out["new"], out["pricing"], first)
    elif not hook:
        out["alert"] = "No Slack webhook set; alert skipped."
    return out


def _listing(url, base):
    try:
        return anchors(BeautifulSoup(get(url).text, "html.parser"), base)
    except Blocked:
        return set()


@app.post("/api/check")
def api_check():
    j = request.get_json(silent=True) or {}
    if not j.get("domain"):
        return jsonify(status="error", errors=["Enter a domain, e.g. example.com"]), 400
    try:
        return jsonify(run_check(j["domain"], j.get("slack_webhook")))
    except Exception as e:
        return jsonify(status="error", errors=[f"Unexpected error: {e.__class__.__name__}: {e}"]), 500


@app.get("/api/posts")
def api_posts():
    rows = db().execute("SELECT * FROM posts WHERE domain=? ORDER BY published DESC", (norm(request.args.get("domain", "")).split("//")[-1],))
    return jsonify([dict(r) for r in rows])


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Competitor Monitor</title>
<style>
:root{--bg:#f6f7f4;--fg:#1c2321;--mut:#5d6b66;--line:#d5dbd6;--card:#fff;--acc:#0b6e4f;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#141a18;--fg:#e8ede9;--mut:#9aa9a2;--line:#2c3733;--card:#1b2320;--acc:#4cc9a0;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 Georgia,'Times New Roman',serif}
main{max-width:920px;margin:0 auto;padding:40px 20px}h1{font-size:2rem;margin:0 0 4px}p.sub{color:var(--mut);margin:0 0 24px}
form{display:grid;gap:10px;grid-template-columns:1fr auto}input{padding:11px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:4px;font:inherit}
#h{grid-column:1/-1}button{padding:11px 20px;border:0;background:var(--acc);color:var(--bg);font:inherit;font-weight:bold;border-radius:4px;cursor:pointer}
button:disabled{opacity:.6}:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#out{margin-top:28px}.msg{padding:10px 14px;border-left:4px solid var(--bad);background:var(--card);margin:8px 0}.ok{border-color:var(--acc)}
article{padding:14px 0;border-top:1px solid var(--line)}article a{color:var(--acc);font-weight:bold;text-decoration:none;overflow-wrap:anywhere}
.m{color:var(--mut);font-size:.9rem}.d{margin:4px 0 0}
</style><main>
<h1>Competitor Monitor</h1><p class="sub">Enter a domain. Run it again later to see only what has been published since.</p>
<form id="f"><input id="d" placeholder="competitor.com" required aria-label="Competitor domain"><button id="go">Check now</button>
<input id="h" placeholder="Slack webhook URL (optional, overrides server setting)" aria-label="Slack webhook"></form>
<div id="out" aria-live="polite"></div></main>
<script>
const e=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
f.onsubmit=async ev=>{ev.preventDefault();go.disabled=true;out.innerHTML='<p class="m">Scraping. This can take up to a minute.</p>';
try{const r=await fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({domain:d.value,slack_webhook:h.value})});
const j=await r.json();let o='';const st=j.status;
if(st==='blocked')o+='<div class="msg"><b>Blocked.</b> '+e(j.errors[0])+'</div>';
else if(st==='no_blog_found')o+='<div class="msg"><b>No blog found.</b> '+e(j.errors[0])+'</div>';
else if(st==='error')o+='<div class="msg">'+e(j.errors[0])+'</div>';
else{o+='<div class="msg ok"><b>'+(j.first_run?'First run: baseline saved. ':'')+j.new.length+' '+(j.first_run?'posts indexed':'new post'+(j.new.length==1?'':'s'))+' for '+e(j.domain)+'</b></div>';
(j.errors||[]).forEach(x=>o+='<div class="msg">'+e(x)+'</div>');}
if(j.pricing)o+='<div class="msg ok">Pricing page: '+e(j.pricing.status)+(j.pricing.status==='changed'?' (added: '+e((j.pricing.added||[]).join(', ')||'none')+'; removed: '+e((j.pricing.removed||[]).join(', ')||'none')+')':'')+'</div>';
if(j.alert)o+='<div class="msg ok">Alert: '+e(j.alert)+'</div>';
(j.new||[]).forEach(p=>o+='<article><a href="'+e(p.url)+'" target="_blank" rel="noopener">'+e(p.title)+'</a><div class="m">'+e(p.published||'no date')+' | keyword: '+e(p.keyword)+' | volume: '+(p.volume!=null?p.volume.toLocaleString()+'/mo':'n/a')+'</div><p class="d">'+e(p.description)+'</p></article>');
out.innerHTML=o}catch(x){out.innerHTML='<div class="msg">Request failed: '+e(x.message)+'</div>'}go.disabled=false};
</script></html>"""


@app.get("/")
def index():
    return PAGE


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
