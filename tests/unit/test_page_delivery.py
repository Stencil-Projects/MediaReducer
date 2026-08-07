"""How pages reach the browser: compressed, and from this container only.

Two properties that are easy to lose in a later edit and expensive when lost:

  • Nothing renders from a CDN. Bootstrap and the Inter webfont are both
    render-blocking, so a host with no outbound internet — a NAS on an isolated
    LAN, the normal deployment for this app — would sit on a blank page until
    each request timed out. Both are served locally, and an https:// stylesheet
    or script added to a template silently undoes that.
  • The favicon resolves, including the bare /favicon.ico browsers request on
    their own.
  • Responses are gzipped. Flask serves this app directly (no proxy in the
    container to do it), and the pages are large: the Config page is ~470 KB of
    HTML that compresses to ~115 KB.
"""
import atexit
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-delivery.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ── Nothing renders from the internet ────────────────────────────────────────
# Only <link>/<script> matter: a documentation link in body copy is a user
# click, not a render-blocking fetch.
_ASSET = re.compile(r"<(?:link|script)\b[^>]*?(?:href|src)\s*=\s*[\"']([^\"']+)", re.I)
offenders = []
for tpl in sorted((ROOT / "templates").glob("*.html")):
    for url in _ASSET.findall(tpl.read_text()):
        if url.startswith(("http://", "https://", "//")):
            offenders.append(f"{tpl.name}: {url}")
check("no template loads a stylesheet or script from another host",
      not offenders)
if offenders:
    for o in offenders:
        print("      " + o)

for name in ("vendor/bootstrap-5.3.3.min.css", "vendor/bootstrap-5.3.3.bundle.min.js",
             "vendor/inter.css", "vendor/fonts/inter-latin.woff2",
             "favicon.svg", "favicon.ico", "apple-touch-icon.png"):
    check(f"{name} ships with the app", (ROOT / "static" / name).is_file())
check("the vendored font CSS points at local files only",
      "gstatic.com" not in (ROOT / "static/vendor/inter.css").read_text())


# ── Responses are compressed ────────────────────────────────────────────────
A.app.config["TESTING"] = True
client = A.app.test_client()
GZIP = {"Accept-Encoding": "gzip, deflate"}

# ── The tab icon ────────────────────────────────────────────────────────────
# Browsers fetch /favicon.ico from the site root on their own, before any
# markup is parsed and again for bookmarks, so the <link> tags in base.html do
# not cover it. Unserved, it 404s on every fresh page load.
r = client.get("/favicon.ico")
check("the bare /favicon.ico request is served", r.status_code == 200)
check("...as an icon", "icon" in (r.headers.get("Content-Type") or ""))
_head = client.get("/").get_data(as_text=True)
for rel in ('rel="icon" type="image/svg+xml"', 'rel="icon" type="image/x-icon"',
            'rel="apple-touch-icon"'):
    check(f"the page declares {rel}", rel in _head)

# ── The page names the build it is running ──────────────────────────────────
# The welcome guide shows the version so a bug report can quote it. It has to
# render from APP_VERSION: a number typed into the template is a second copy
# that goes stale at the next release with nothing to catch it.
check("the delivered page carries the running version", A.APP_VERSION in _head)
check("...rendered from APP_VERSION, not hardcoded in a template",
      not [t.name for t in (ROOT / "templates").glob("*.html")
           if A.APP_VERSION in t.read_text()])

r = client.get("/", headers=GZIP)
check("the dashboard is gzipped", r.headers.get("Content-Encoding") == "gzip")
body = gzip.decompress(r.get_data())
check("and decompresses to the real page",
      body.startswith(b"<!doctype html>") and b"</html>" in body)
check("Content-Length matches the compressed body",
      int(r.headers["Content-Length"]) == len(r.get_data()))
check("compression is worth doing (at least 2x)", len(body) > 2 * len(r.get_data()))

# All three pages, not just the one. The compression hook is route-agnostic,
# which is exactly why a route that sets its own headers can fall out of it
# without anything noticing: only the dashboard was ever checked.
for path, name in (("/config", "Configuration"), ("/explorer", "Filtering & Scoring")):
    pr = client.get(path, headers=GZIP)
    pbody = gzip.decompress(pr.get_data()) if pr.headers.get("Content-Encoding") == "gzip" else b""
    check(f"{name} is gzipped too", pr.headers.get("Content-Encoding") == "gzip")
    check(f"...and decompresses to the real page",
          pbody.startswith(b"<!doctype html>") and b"</html>" in pbody)
    check(f"...at better than 2x", len(pbody) > 2 * len(pr.get_data()))

plain = client.get("/")
check("a client that didn't ask for gzip gets the page uncompressed",
      "Content-Encoding" not in plain.headers
      and plain.get_data().startswith(b"<!doctype html>"))
check("both answers carry Vary: Accept-Encoding, so a shared cache can't mix them",
      "accept-encoding" in (r.headers.get("Vary") or "").lower()
      and "accept-encoding" in (plain.headers.get("Vary") or "").lower())

api = client.get("/api/status", headers=GZIP)
check("the polled status endpoint is compressed too",
      api.headers.get("Content-Encoding") == "gzip"
      and isinstance(json.loads(gzip.decompress(api.get_data())), dict))


# ── Vendored assets: compressed once, then cached forever ───────────────────
css = client.get("/static/vendor/bootstrap-5.3.3.min.css", headers=GZIP)
check("vendored CSS is gzipped despite being served as a static file",
      css.headers.get("Content-Encoding") == "gzip")
check("and arrives byte-identical to the file on disk",
      gzip.decompress(css.get_data())
      == (ROOT / "static/vendor/bootstrap-5.3.3.min.css").read_bytes())
check("vendored assets are immutable, so a repeat page load doesn't refetch them",
      "immutable" in (css.headers.get("Cache-Control") or ""))

font = client.get("/static/vendor/fonts/inter-latin.woff2", headers=GZIP)
check("woff2 is left alone (already compressed; re-gzipping only costs CPU)",
      "Content-Encoding" not in font.headers)
check("the font is served intact",
      font.get_data() == (ROOT / "static/vendor/fonts/inter-latin.woff2").read_bytes())

tiny = client.get("/api/logs/archived/status", headers=GZIP)
check("a response too small to benefit is not compressed",
      len(tiny.get_data()) < A._COMPRESS_MIN_BYTES
      and "Content-Encoding" not in tiny.headers)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
