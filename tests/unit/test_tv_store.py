"""The TV inventory in the library store, refreshed from the media servers.

The contracts pinned here are the store's, not the fetchers' (those live in
test_tv_inventory.py):

  * Each scan owns its type. A movie Simulate must not wipe the TV inventory,
    and a TV refresh must not wipe the movie snapshot — replace_movies and
    replace_tv_series each delete only their own rows.
  * A server blip leaves the stored rows alone. Returning None and writing
    nothing is what keeps a flaky container from emptying the TV view it
    filled yesterday — while a real empty answer clears it.
  * Monitored paths are TV's deletion allow-list, resolved by folder name
    (the server names the series in ITS namespace).
  * A code-change wipe takes the TV rows too, and the refresh a completed
    run fires rebuilds them.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-tv-store.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402
import db  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


CFG = {"USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:8096",
       "JELLYFIN_API_KEY": "k", "USE_PLEX": False,
       "TAUTULLI_URL": "", "TAUTULLI_API_KEY": "",
       "PLEX_URL": "", "PLEX_TOKEN": "", "OUTPUT_DIR": _OUT}
STORE = A.db_path()

# read_snapshot answers only once a scan has stamped snapshot_built_at, and a
# TV refresh deliberately never stamps it: that meta also feeds
# _library_db_fresh, the freshness gate the startup demote reads, and a media
# server fetch is not a library scan. Seed the stamp the way a real install
# has it.
with db.transaction(STORE) as conn:
    db.set_meta(conn, "snapshot_built_at", 1_700_000_000)

def _season(n, eps, size):
    return {"n": n, "eps": eps, "size_bytes": size,
            "eps_watched": 0, "last_played": 0, "plays": 0, "users": 0}

def server_rows():
    return [
        A._new_tv_row(title="Big Show", year=2019, path="/data/shows/Big Show",
                      seasons=[_season(2, 10, 22_000_000_000),
                               _season(1, 8, 18_000_000_000)],
                      added_at=1_614_800_000, status="continuing",
                      jf_source_id="jellyfin:b1"),
        A._new_tv_row(title="Old Show", year=2011, path="/data/unmanaged/Old Show",
                      seasons=[_season(1, 6, 9_000_000_000)],
                      added_at=1_420_000_000, status="ended",
                      jf_source_id="jellyfin:o1"),
    ]

A._jellyfin_series_inventory = lambda url, key: server_rows()
A._jellyfin_series_watch = lambda url, key, strict=False: ({}, set())

def tv_rows():
    with db.connect(STORE) as conn:
        return [r for r in (db.read_snapshot(conn) or {}).get("movies", [])
                if r.get("media_type") == "tv"]

def movie_rows():
    with db.connect(STORE) as conn:
        return [r for r in (db.read_snapshot(conn) or {}).get("movies", [])
                if r.get("media_type") == "movie"]


# ── The refresh lands the rows, seasons round-tripping through the store ────
n = A._refresh_tv_inventory(CFG)
rows = tv_rows()
check("both series land as tv rows", n == 2 and len(rows) == 2, (n, len(rows)))
big = next((r for r in rows if r["title"] == "Big Show"), {})
check("a row carries size, year, status and the media-server id",
      big.get("size_bytes") == 40_000_000_000 and big.get("year") == 2019
      and big.get("tv_status") == "continuing"
      and big.get("jf_source_id") == "jellyfin:b1", big)
seasons = big.get("tv_seasons") or []
check("the season blob round-trips ordered by number, plays/users included",
      [s["n"] for s in seasons] == [1, 2] and seasons[1]["eps"] == 10
      and seasons[0].get("plays") == 0 and seasons[0].get("users") == 0, seasons)

# ── Each scan owns its type, in both directions ─────────────────────────────
MOVIE = {"media_type": "movie", "path": "/lib/m.mkv", "title": "A Movie",
         "size_bytes": 2_000_000_000, "size_gb": 2.0, "plays": 1, "users": 1}
with db.transaction(STORE) as conn:
    db.replace_movies(conn, [MOVIE])
check("a movie write leaves the tv inventory standing", len(tv_rows()) == 2)
check("...and lands its movie", len(movie_rows()) == 1)
A._refresh_tv_inventory(CFG)
check("a tv refresh leaves the movie snapshot standing", len(movie_rows()) == 1)
check("...and the tv rows are replaced, not duplicated", len(tv_rows()) == 2)

# ── A blip leaves the stored rows alone ─────────────────────────────────────
A._jellyfin_series_inventory = lambda url, key: None
check("a fetch failure reports None", A._refresh_tv_inventory(CFG) is None)
check("...and the stored inventory survives it", len(tv_rows()) == 2)
check("no media server configured fetches nothing — Sonarr alone is not one",
      A._refresh_tv_inventory({"OUTPUT_DIR": _OUT, "SONARR_URL": "http://s",
                               "SONARR_API_KEY": "k"}) is None)

# ── An emptied server is a real answer, not a blip ──────────────────────────
A._jellyfin_series_inventory = lambda url, key: []
check("an empty series list clears the inventory",
      A._refresh_tv_inventory(CFG) == 0 and len(tv_rows()) == 0)
check("...without touching the movies", len(movie_rows()) == 1)

# ── Monitored paths are TV's deletion allow-list ────────────────────────────
# The server names the series in ITS namespace; the folder NAME is what both
# filesystems share, so scope = "does a monitored dir hold that folder".
_TVDIR = Path(_OUT, "tvlib")
(_TVDIR / "Big Show").mkdir(parents=True)
A._jellyfin_series_inventory = lambda url, key: server_rows()
A._refresh_tv_inventory({**CFG, "MONITOR_DIRS": [str(_TVDIR)]})
_by_title = {r["title"]: r for r in tv_rows()}
check("a series whose folder sits under a monitored dir is in scope",
      _by_title["Big Show"]["tv_in_scope"] == 1
      and _by_title["Big Show"]["path"] == str(_TVDIR / "Big Show"), _by_title.get("Big Show"))
check("a series kept elsewhere is inventory but OUT of scope",
      _by_title["Old Show"]["tv_in_scope"] == 0 and _by_title["Old Show"]["path"] is None,
      _by_title.get("Old Show"))
check("no monitored dirs means nothing is in scope",
      (A._refresh_tv_inventory(CFG) or 0) == 2
      and all(r["tv_in_scope"] == 0 for r in tv_rows()))
# Nested layouts resolve too — the same trailing-run rule the movie side
# uses, so /shows/Anime/Big Show under a monitored dir is found, not just a
# folder sitting directly under it.
(_TVDIR / "Anime" / "Nested Show").mkdir(parents=True)
_nested = [A._new_tv_row(title="Nested Show", year=2020,
                         path="/data/shows/Anime/Nested Show",
                         seasons=[_season(1, 4, 2_000_000_000)],
                         added_at=1_600_000_000, status="ended",
                         jf_source_id="jellyfin:n1")]
A._resolve_tv_scope(_nested, {**CFG, "MONITOR_DIRS": [str(_TVDIR)]})
check("a series nested below the monitored dir resolves by its trailing run",
      _nested[0]["tv_in_scope"] == 1
      and _nested[0]["path"] == str(_TVDIR / "Anime" / "Nested Show"), _nested[0]["path"])

# A name is not an identity. Two monitored libraries of the same shows — a
# 1080p tree beside a 4K one, an archive beside a live one — carry the same
# folder name under both, and the media server's own path is in its container
# namespace, so the name is all the resolver has to go on. Taking whichever
# monitored dir came first would hand the deletion pass the copy the server was
# NOT talking about: seasons removed from one library, scored on the other's
# plays and sizes. Ambiguous resolves to nothing, and says so on the row.
_ALT = Path(_OUT) / "library" / "TV-4K"
(_ALT / "Twin Show").mkdir(parents=True)
(_TVDIR / "Twin Show").mkdir(parents=True)
# Rebuilt per resolve: the resolver REPLACES row["path"] with what it found
# (or None), exactly as a refresh does, so a re-resolve of the same object
# would be judging its own previous answer.
def _twin_row():
    return [A._new_tv_row(title="Twin Show", year=2020, path="/data/tv-4k/Twin Show",
                          seasons=[_season(1, 4, 2_000_000_000)],
                          added_at=1_600_000_000, status="ended",
                          jf_source_id="jellyfin:t1")]
_twin = _twin_row()
A._resolve_tv_scope(_twin, {**CFG, "MONITOR_DIRS": [str(_TVDIR), str(_ALT)]})
check("a folder name found under two monitored dirs resolves to neither",
      _twin[0]["tv_in_scope"] == 0 and _twin[0]["path"] is None, _twin[0]["path"])
check("...and the row says it was a conflict, not an unmonitored show",
      len(_twin[0].get("tv_scope_conflict") or []) == 2, _twin[0].get("tv_scope_conflict"))
# Unique again once only one library holds it — the guard is about ambiguity,
# not about having more than one monitored directory.
shutil.rmtree(_TVDIR / "Twin Show")
_twin = _twin_row()
A._resolve_tv_scope(_twin, {**CFG, "MONITOR_DIRS": [str(_TVDIR), str(_ALT)]})
check("...and it resolves once the name is unambiguous again",
      _twin[0]["tv_in_scope"] == 1 and _twin[0]["path"] == str(_ALT / "Twin Show"),
      _twin[0]["path"])
# The same folder reached through two OVERLAPPING monitored entries is one
# place, not a conflict — otherwise a nested monitored dir would disable TV.
_twin = _twin_row()
A._resolve_tv_scope(_twin, {**CFG, "MONITOR_DIRS": [str(_ALT), str(_ALT.parent)]})
check("the same folder via overlapping monitored dirs is not a conflict",
      _twin[0]["tv_in_scope"] == 1, _twin[0])

# ── The deploy sequence: a code-change wipe takes TV rows too ───────────────
# ensure_code_current clears every snapshot row on an engine-checksum change,
# TV included. The Simulate that rebuilds the movie rows triggers a TV refresh
# from its run-end path, so the repair is the same gesture that exposes it.
A._refresh_tv_inventory(CFG)
check("(seed) inventory present before the wipe", len(tv_rows()) == 2)
with db.transaction(STORE) as conn:
    db.ensure_code_current(conn, "a-changed-engine-checksum")
check("a code-change wipe clears the tv rows with everything else",
      len(tv_rows()) == 0)
# The wipe also drops snapshot_built_at; the Simulate that follows re-stamps
# it while rebuilding the movie rows, and THEN the run-end refresh fires.
with db.transaction(STORE) as conn:
    db.set_meta(conn, "snapshot_built_at", 1_700_000_500)
check("the refresh a completed run now fires rebuilds them",
      A._refresh_tv_inventory(CFG) == 2 and len(tv_rows()) == 2)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
