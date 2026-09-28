"""A season this app deleted stays out of the plan until the servers stop listing it.

Plex and Jellyfin keep a deleted season in their listings, at its old size,
until they rescan, and the TV inventory is what they list. Found in the test
lab: a Cleanup deleted Manifest S1 and Riverdale S1, and a settings save a
minute later marked both again — "a run would delete 2 seasons, 8.1 GB" for
seasons already gone. A run gave such a season a share of the deficit, then
found it vanished at deletion, freeing nothing.

Pinned: the deletion records the season and its files; the inventory leaves it
out while it is recorded, still listed and still gone (so the next season is
the oldest one), recomputing the show's size; it counts again when a recorded
file is back on disk or the server lists it as added after the deletion; the
record is forgotten once a complete fetch no longer lists the season, and kept
through a partial one; and the stored-row planners (the settings-save rebuild,
the Marked list) leave it out too.

Hermetic: temp store and library, the media server's inventory stubbed.
"""
import atexit
import copy
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-gone-seasons.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402
import db       # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


GB = 1_000_000_000
T0 = time.time() - 30 * 86400          # when the servers first listed the seasons
LIB = Path(_OUT) / "library" / "tv"
SHOW = LIB / "Manifest (2018)"
(SHOW / "Season 01").mkdir(parents=True)
(SHOW / "Season 02").mkdir(parents=True)
EP = SHOW / "Season 01" / "Manifest S01E01.mkv"

LISTING = [{
    "media_type": "tv", "title": "Manifest", "year": 2018, "jf_source_id": "jellyfin:m",
    "imdb_id": "tt8421350", "path": "/data/tv/Manifest (2018)", "tv_status": "ended",
    "tv_seasons": [{"n": 1, "size_bytes": 4 * GB, "eps": 3, "added_at": T0},
                   {"n": 2, "size_bytes": 5 * GB, "eps": 3, "added_at": T0}],
    "size_bytes": 9 * GB, "size_gb": 9.0, "tv_episodes": 6}]
FAIL_JF = [False]
A._jellyfin_series_inventory = lambda url, key: (None if FAIL_JF[0] else copy.deepcopy(LISTING))
A._plex_series_inventory = lambda conn: copy.deepcopy(LISTING_PLEX)
LISTING_PLEX = []
CFG = {"OUTPUT_DIR": _OUT, "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test",
       "JELLYFIN_API_KEY": "k", "USE_PLEX": False, "MONITOR_DIRS": [str(LIB)]}
ENTRY = {"sid": "jellyfin:m", "title": "Manifest", "season": 1, "year": 2018,
         "path": str(SHOW), "size_bytes": 4 * GB}


def record():
    with db.connect(A.db_path()) as conn:
        return db.get_meta(conn, A._TV_GONE_KEY) or {}


def seasons(rows, title="Manifest"):
    return [s["n"] for r in rows if r["title"] == title for s in r["tv_seasons"]]


# ── The deletion writes the record ────────────────────────────────────────
EP.write_bytes(b"x" * 100)
A._tv_season_relpaths = lambda cfg, sid, n, name="": (
    [(PurePosixPath("Season 01/Manifest S01E01.mkv"), 100)], None)
A._tv_log_deleted = lambda *a, **k: None
rep = {"skipped": [], "deleted_seasons": [], "deleted_files": 0, "freed_bytes": 0,
       "vanished_seasons": 0, "vanished_bytes": 0}
done = A._delete_tv_season(dict(CFG), dict(ENTRY), rep)
rec = record().get("jellyfin:m|S1") or {}
check("deleting a season records it, with the files it removed",
      done and not EP.exists() and rec.get("files") == [str(EP)]
      and rec.get("folder") == "Manifest (2018)" and rec.get("season") == 1, (done, rec))

# ── While the server still lists it, the inventory leaves it out ──────────
rows = A._tv_inventory_rows(CFG)
check("the season a server still lists is left out of the inventory",
      seasons(rows) == [2], seasons(rows))
check("...with the show's size and episodes recomputed without it",
      rows[0]["size_bytes"] == 5 * GB and rows[0]["tv_episodes"] == 3, rows[0])
check("...and the record kept while the server lists it", "jellyfin:m|S1" in record())
check("...without touching what the servers sent", len(LISTING[0]["tv_seasons"]) == 2)

rows[0].update(tv_in_scope=True, rating=8.0, votes=1000)
plan = A._tv_season_plan(rows, {"OUTPUT_DIR": _OUT})
check("the next season is the oldest one now, and eligible",
      [(e["title"], e["season"]) for e in plan["order"]] == [("Manifest", 2)], plan["order"])

# ── The stored-row planners leave it out too ──────────────────────────────
stored = copy.deepcopy(LISTING)
stored[0].update(tv_in_scope=True, rating=8.0, votes=1000, path=str(SHOW))
with db.transaction(A.db_path()) as conn:
    db.replace_tv_series(conn, stored)          # a refresh that failed after the deletion
    db.set_meta(conn, "snapshot_built_at", int(time.time()))
A._tv_cleanup_armed = lambda cfg: True
order = A._reconcile_season_order({"OUTPUT_DIR": _OUT})
check("a settings save's rebuild does not plan it from stored rows",
      [(e["title"], e["season"]) for e in order or []] == [("Manifest", 2)], order)
listed = A._tv_eligible_entries({"OUTPUT_DIR": _OUT}, set())
check("...nor does the Marked list's eligible order",
      [(e["title"], e["season"]) for e in listed] == [("Manifest", 2)], listed)
with A.app.test_request_context():
    snap = A.api_library_snapshot().get_json()
check("...nor the Filtering & Scoring page",
      [s["n"] for m in snap["movies"] if m.get("title") == "Manifest" for s in m["tv_seasons"]] == [2],
      snap.get("movies"))

# ── A partial fetch keeps the record ───────────────────────────────────────
LISTING_PLEX[:] = [dict(copy.deepcopy(LISTING[0]), jf_source_id=None, source_id="plex:m",
                        tv_seasons=[{"n": 2, "size_bytes": 5 * GB, "eps": 3, "added_at": T0}])]
FAIL_JF[0] = True
A._tv_inventory_rows(dict(CFG, USE_PLEX=True, PLEX_URL="http://px.test", PLEX_TOKEN="t"))
check("a fetch a configured server did not answer forgets nothing", "jellyfin:m|S1" in record())
FAIL_JF[0] = False
LISTING_PLEX[:] = []

# ── It counts again once it is real ───────────────────────────────────────
EP.parent.mkdir(parents=True, exist_ok=True)       # restored by hand
EP.write_bytes(b"x" * 100)
rows = A._tv_inventory_rows(CFG)
check("a season whose file is back on disk counts again",
      seasons(rows) == [1, 2] and "jellyfin:m|S1" not in record(), (seasons(rows), record()))
EP.unlink()

A._record_tv_season_gone(dict(ENTRY), [EP])
LISTING[0]["tv_seasons"][0]["added_at"] = time.time() + 5   # re-added by the server
rows = A._tv_inventory_rows(CFG)
check("a season the server lists as added after the deletion counts again",
      seasons(rows) == [1, 2], seasons(rows))
LISTING[0]["tv_seasons"][0]["added_at"] = T0

# ── Forgotten once the server has caught up ───────────────────────────────
A._record_tv_season_gone(dict(ENTRY), [EP])
del LISTING[0]["tv_seasons"][0]                    # the server rescanned
rows = A._tv_inventory_rows(CFG)
check("the record is forgotten once the server no longer lists the season",
      seasons(rows) == [2] and record() == {}, record())

# ── Found by folder when the server's id has changed ──────────────────────
LISTING[0]["tv_seasons"].insert(0, {"n": 1, "size_bytes": 4 * GB, "eps": 3, "added_at": T0})
A._record_tv_season_gone(dict(ENTRY), [EP])
LISTING[0]["jf_source_id"] = "jellyfin:new-id"
check("a recorded season is still recognised by its series folder",
      seasons(A._tv_inventory_rows(CFG)) == [2])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
