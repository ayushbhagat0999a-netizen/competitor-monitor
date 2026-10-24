"""Demo helper: removes the 2 newest saved posts of a domain from monitor.db,
so the next check shows them as "new". Usage:
    python delete_posts.py ahrefs.com
    python delete_posts.py            (asks for the domain)
"""
import sqlite3, sys, os
from urllib.parse import urlparse

DB = os.environ.get("DB_PATH", "monitor.db")
HOW_MANY = 2

raw = sys.argv[1] if len(sys.argv) > 1 else input("Domain (e.g. ahrefs.com): ")
raw = raw.strip()
domain = urlparse(raw if raw.startswith("http") else "https://" + raw).netloc

if not os.path.exists(DB):
    sys.exit(f"{DB} not found. Run the tool once on this domain first, and run this script from the same folder as app.py.")

c = sqlite3.connect(DB)
try:
    cur = c.execute(
        """DELETE FROM posts WHERE url IN (
             SELECT url FROM posts WHERE domain=? AND baseline=0
             ORDER BY published DESC LIMIT ?)""",
        (domain, HOW_MANY),
    )
except sqlite3.OperationalError as e:
    sys.exit(f"Database error: {e}. Run the tool once first.")
c.commit()

if cur.rowcount == 0:
    print(f"No saved posts found for '{domain}'. Check the domain spelling (it must match what you typed in the tool).")
else:
    print(f"Deleted {cur.rowcount} posts for {domain}. Now click 'Check now' again in the tool.")
