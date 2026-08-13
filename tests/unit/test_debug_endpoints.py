"""The diagnostic popups, driven with servers that actually answer.

Every /api/debug/* endpoint mirrors one of the run's own upstream call
sequences and hands back a block of text for the user to read — or to paste
into a bug report. Coverage found the suite only ever reached the "not
configured" line of each: with nothing to talk to, every endpoint returned its
400 and the several hundred lines that format a real answer never ran.

The properties worth holding, in order of what they cost:

  • Nothing in a debug blob is a credential. These exist to be copied into a
    public issue. The blobs quote URLs, echo server replies and interpolate
    exception text, and a Plex URL carries its token in the query string — so
    the leak here would not be a mistake anyone made deliberately, it would be
    an error message with a URL in it. Every credential below is a sentinel
    string, and no response may contain one.
  • An upstream that fails is reported, not raised. The popup is opened
    BECAUSE something is wrong, so the one moment it must not 500 is when the
    server it is asking about is broken.
  • A configured, answering server produces a real answer, not an empty shell.

Hermetic: every upstream helper is stubbed, so nothing reaches a network.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
OUT = Path(_tmpout.config())
import app as A  # noqa: E402
import db        # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


# Distinctive enough that a substring search cannot false-positive, and shaped
# like the real thing so a redactor keyed on length or charset behaves the same.
SECRETS = {
    "TAUTULLI_API_KEY": "taut0SENTINELkey0000000000000000",
    "PLEX_TOKEN": "plex0SENTINELtoken000",
    "JELLYFIN_API_KEY": "jelly0SENTINELkey00000000000000",
    "RADARR_API_KEY": "radarr0SENTINELkey0000000000000",
    "SONARR_API_KEY": "sonarr0SENTINELkey0000000000000",
}
CFG = {
    "OUTPUT_DIR": str(OUT), "MONITOR_DIRS": [str(OUT / "movies")],
    "USE_PLEX": True, "USE_JELLYFIN": True,
    "TAUTULLI_URL": "http://tautulli.test:8181", "PLEX_URL": "http://plex.test:32400",
    "JELLYFIN_URL": "http://jf.test:8096",
    "RADARR_URL": "http://radarr.test:7878", "SONARR_URL": "http://sonarr.test:8989",
    "PROTECTED_COLLECTIONS": ["Keepers"], "JELLYFIN_PROTECTED_COLLECTIONS": ["Keepers"],
    **SECRETS,
}
(OUT / "movies").mkdir(exist_ok=True)
A.CONFIG_PATH.write_text(json.dumps(CFG))
with A._config_memo_lock:
    A._config_memo["key"] = object()

A.app.config["TESTING"] = True
client = A.app.test_client()
HDR = {"X-MediaReducer": "1"}


# ── Servers that answer ─────────────────────────────────────────────────────
# Shaped like the real replies, because the formatting code below reads them:
# an empty dict would take every "nothing came back" branch and prove nothing.
def plex_get(url, token, path, timeout=15):
    if path.rstrip("/").endswith("/library/sections"):
        return {"MediaContainer": {"Directory": [
            {"key": "1", "type": "movie", "title": "Movies",
             "Location": [{"path": "/data/movies"}]}]}}
    if path.endswith("/collections"):
        return {"MediaContainer": {"Directory": [
            {"ratingKey": "77", "title": "Keepers", "type": "collection",
             "childCount": 2}]}}
    return {"MediaContainer": {"Metadata": [
        {"ratingKey": "78", "title": "A Movie", "year": 2020,
         "Media": [{"Part": [{"file": "/data/movies/A Movie (2020)/a.mkv", "size": 1234}]}]}]}}


def jellyfin_get(url, key, path, params=None, timeout=15):
    p = path.strip("/")
    if p == "System/Info":
        return {"ServerName": "AQUILAE", "Version": "10.9.0"}
    if p == "Users":
        return [{"Id": "u1", "Name": "someone"}]
    return {"Items": [
        {"Id": "i1", "Name": "Keepers", "Type": "BoxSet"},
        {"Id": "i2", "Name": "A Movie", "Type": "Movie", "ProductionYear": 2020,
         "Path": "/data/movies/A Movie (2020)/a.mkv",
         "ProviderIds": {"Imdb": "tt1", "Tmdb": "9"},
         "MediaSources": [{"Size": 1234, "Path": "/data/movies/A Movie (2020)/a.mkv"}],
         "UserData": {"PlayCount": 1, "Played": True}}], "TotalRecordCount": 1}


def tautulli(conn, cmd, timeout=8, **params):
    if cmd == "get_libraries":
        return [{"section_id": "1", "section_type": "movie", "section_name": "Movies",
                 "count": "1"}]
    if cmd == "get_library_media_info":
        return {"data": [{"rating_key": "77", "title": "A Movie", "year": "2020",
                          "file_size": "1234", "last_played": None, "play_count": 0,
                          "added_at": "1600000000"}]}
    return {"title": "A Movie", "year": "2020", "rating_key": "77",
            "guids": ["imdb://tt1", "tmdb://9"], "collections": ["Keepers"],
            "media_info": [{"parts": [{"file": "/data/movies/A Movie (2020)/a.mkv",
                                       "file_size": "1234"}]}]}


def json_request(url, headers=None, timeout=15, method=None, payload=None):
    # The collection-name helpers build their own URLs rather than going
    # through _plex_get/_jellyfin_get, so they land here — including the Plex
    # one, whose token rides in the query string. That is the URL an exception
    # would quote.
    if "IncludeItemTypes=BoxSet" in url:
        return {"Items": [{"Id": "i1", "Name": "Keepers", "Type": "BoxSet"}]}
    if "/collections" in url:
        return {"MediaContainer": {"Directory": [
            {"ratingKey": "77", "title": "Keepers", "type": "collection"}]}}
    if "/library/sections" in url:
        return {"MediaContainer": {"Directory": [
            {"key": "1", "type": "movie", "title": "Movies"}]}}
    if "/system/status" in url:
        return {"appName": "Sonarr", "version": "4.0.0"}
    if "/api/v3/series" in url:
        return [{"id": 5, "title": "A Show", "year": 2001, "path": "/tv/A Show",
                 "seasons": [{"seasonNumber": 1, "monitored": True}]}]
    if "/api/v3/rootfolder" in url:
        return [{"path": "/data/movies"}]
    if "/api/v3/movie" in url:
        return [{"id": 3, "title": "A Movie", "year": 2020, "tmdbId": 9,
                 "path": "/data/movies/A Movie (2020)",
                 "movieFile": {"relativePath": "a.mkv", "size": 1234}}]
    return {"version": "5.0.0", "appName": "Radarr"}


A._plex_get = plex_get
A._jellyfin_get = jellyfin_get
A._tautulli_api_request = tautulli
A._json_request = json_request
A._read_library_snapshot = lambda: ({"movies": [
    {"title": "A Movie", "year": 2020, "rating": 6.5, "plays": 0, "protected": True,
     "favorite": False, "path": "/data/movies/A Movie (2020)/a.mkv",
     "size_bytes": 1234, "media_type": "movie"}]}, None)
db.read_cache_dict = lambda p: {"pending": {"entries": {}}, "meta": {"tv_share": {}}}

ENDPOINTS = [
    ("/api/collections/plex/debug", {"names": ["Keepers"]}),
    ("/api/collections/jellyfin/debug", {"names": ["Keepers"]}),
    ("/api/debug/tautulli/movies", {}),
    ("/api/debug/jellyfin/movies", {}),
    ("/api/debug/radarr", {}),
    ("/api/debug/sonarr", {}),
    ("/api/debug/media-paths", {}),
    ("/api/debug/library-snapshot", {}),
    ("/api/debug/cache", {}),
    ("/api/debug/report", {}),
    ("/api/collections", {}),
]


class Raised:
    """A route that let an exception escape, as a response-shaped stand-in.

    app.config["TESTING"] re-raises out of the test client, so an endpoint
    without a handler kills this script instead of failing one check — and the
    endpoint having no handler is exactly what half of these checks are about.
    Catching it here turns that into the named failure it should be."""
    def __init__(self, exc):
        self.status_code, self.exc = 500, exc

    def get_json(self):
        return {"ok": False, "error": f"{type(self.exc).__name__}: {self.exc}"}

    def get_data(self, as_text=False):
        return f"{type(self.exc).__name__}: {self.exc}"


def post(path, payload):
    try:
        return client.post(path, json=payload, headers=HDR)
    except Exception as e:
        return Raised(e)


# ── Every endpoint answers, with something in it ───────────────────────────
bodies = {}
for path, payload in ENDPOINTS:
    r = post(path, payload)
    bodies[path] = r.get_data(as_text=True)
    got = r.get_json() or {}
    # /api/collections answers per server rather than with one top-level ok:
    # one server can be reachable while the other is not.
    answered = (got.get("plex", {}).get("ok") and got.get("jellyfin", {}).get("ok")
                if path == "/api/collections" else got.get("ok") is True)
    check(f"{path} answers rather than raising",
          r.status_code == 200 and answered, (r.status_code, bodies[path][:160]))
    if path == "/api/collections":
        check("...and lists the collections both servers report",
              "Keepers" in (got.get("plex", {}).get("names") or [])
              and "Keepers" in (got.get("jellyfin", {}).get("names") or []), got)
    elif path.endswith("/debug") and "/collections/" in path:
        # These two answer with structure rather than a text block: the popup
        # renders the sections it tried and what each collection resolved to.
        check("...and found the ticked collection", got.get("matched_count") == 1
              and got.get("selected") == ["Keepers"], got)
    else:
        check("...with a real answer, not an empty shell",
              len(got.get("text") or "") > 200, len(got.get("text") or ""))

# ── No credential reaches the page ─────────────────────────────────────────
# The one property that matters after the blob leaves the container.
for path, body in bodies.items():
    leaked = [k for k, v in SECRETS.items() if v in body]
    check(f"{path} carries no credential", not leaked, leaked)


# ── An upstream that is broken is reported, not raised ─────────────────────
# This is the state the popup is opened IN. A 500 here replaces the diagnosis
# with a stack trace in the container log nobody reading the popup can see.
def boom(*a, **k):
    raise RuntimeError("connection refused by " + CFG["PLEX_URL"] + "?X-Plex-Token="
                       + SECRETS["PLEX_TOKEN"])


A._plex_get = A._jellyfin_get = A._tautulli_api_request = A._json_request = boom
A._read_library_snapshot = lambda: (None, "missing")
db.read_cache_dict = boom

for path, payload in ENDPOINTS:
    r = post(path, payload)
    body = r.get_data(as_text=True)
    check(f"{path} survives a dead upstream", r.status_code in (200, 400),
          (r.status_code, body[:160]))
    check(f"...and still leaks nothing while reporting the failure",
          not [k for k, v in SECRETS.items() if v in body],
          [k for k, v in SECRETS.items() if v in body])

# ── The report shows EFFECTIVE URLs and honest media counts ────────────────
# A blank URL field whose credential is set rides a default at connect time;
# the report printed the raw "(blank)" — so the one artifact built for
# diagnosis showed a working server as unconfigured, three lines above its own
# "jellyfin_connected=True". And the snapshot line called every row a movie
# when 257 of them were TV series.
A._read_library_snapshot = lambda: ({"built_at": 1000, "movies": (
    [{"title": f"m{i}", "media_type": "movie", "rating": 7.0} for i in range(3)]
    + [{"title": f"s{i}", "media_type": "tv"} for i in range(2)])}, None)
db.read_cache_dict = lambda *a, **k: {}
A.CONFIG_PATH.write_text(json.dumps({
    **{k: v for k, v in CFG.items() if k != "JELLYFIN_URL"},
    "JELLYFIN_URL": "", "JELLYFIN_API_KEY": SECRETS["JELLYFIN_API_KEY"],
    "_SERVICE_URL_DEFAULTS": {"JELLYFIN_URL": "http://jf-default.test:8096"},
    **SECRETS}))
with A._config_memo_lock:
    A._config_memo["key"] = object()
r = post("/api/debug/report", {})
# The decoded report text, not the JSON envelope — the envelope —-escapes
# the em dash these lines contain.
body = (r.get_json() or {}).get("text") or ""
check("a defaulted URL prints its effective value, not (blank)",
      "JELLYFIN_URL = http://<host>:8096 (default — the saved field is blank)" in body,
      [l for l in body.splitlines() if "JELLYFIN_URL" in l])
check("...still with the hostname scrubbed", "jf-default.test" not in body)
check("...while a genuinely set URL renders as before",
      "PLEX_URL = http://<host>:32400" in body,
      [l for l in body.splitlines() if "PLEX_URL =" in l])
check("the snapshot line splits movies from TV series",
      "library snapshot: 3 movies + 2 TV series" in body,
      [l for l in body.splitlines() if "library snapshot" in l])

# ── Nothing configured: the message says what to do ────────────────────────
A.CONFIG_PATH.write_text(json.dumps({"OUTPUT_DIR": str(OUT)}))
with A._config_memo_lock:
    A._config_memo["key"] = object()
for path, payload in ENDPOINTS:
    r = post(path, payload)
    body = r.get_json() or {}
    check(f"{path} with nothing configured is refused, not crashed",
          r.status_code in (200, 400) and isinstance(body, dict), r.status_code)
    if r.status_code == 400:
        check("...naming what to fill in", len(body.get("error") or "") > 10, body)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
