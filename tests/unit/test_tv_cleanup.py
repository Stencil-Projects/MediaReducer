"""The TV season deletion pass — the first code that removes TV files, so what
is pinned here is the SAFETY contract more than the happy path:

  • a mark waits out the deletion delay: the pass that marks a season can
    never be the pass that deletes it;
  • Monitor Only never deletes, even a season whose delay has elapsed;
  • the pass is fail-closed: any configured source failing to answer aborts
    the whole pass with nothing touched;
  • when Sonarr is configured, it must accept the season unmonitor BEFORE the
    first file is removed, and a refusal leaves the season intact and still
    marked — but Sonarr itself is OPTIONAL and cleanup-only: with no Sonarr
    at all the season still deletes and Sonarr is never called;
  • a file path from the media server that escapes the series folder kills
    the season's deletion, not the guard;
  • a season the fresh plan no longer takes is unmarked, never deleted.

The happy path is proved too: files removed and recorded in deleted.log in the
engine's parseable format, the emptied season folder tidied, the sibling
season and series folder untouched, the mark cleared.

Hermetic: a real temp library tree and a real SQLite store; the TV inventory
is a stub (its fetchers have their own test), Jellyfin's episode lister and
Sonarr's API are fakes; no other server is configured, so the strict fetch
passes trivially except where a test injects a failure.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-tv-clean.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

GB = 1_000_000_000
NOW = time.time()

# ── A real library tree ─────────────────────────────────────────────────────
TV = Path(_OUT) / "library" / "TV Shows"
SHOW = TV / "Dead Show"
S1, S2 = SHOW / "Season 01", SHOW / "Season 02"

def reset_files():
    shutil.rmtree(SHOW, ignore_errors=True)
    S1.mkdir(parents=True)
    S2.mkdir(parents=True)
    (S1 / "ep1.mkv").write_bytes(b"x" * 1000)
    (S1 / "ep2.mkv").write_bytes(b"y" * 2000)
    (S2 / "keeper.mkv").write_bytes(b"z" * 3000)

def reset_state():
    A._save_tv_cleanup_state({"marked": {}})

reset_files()

CFG = {
    "OUTPUT_DIR": _OUT, "MONITOR_DIRS": [str(TV)],
    # 100% watch history: with IMDb in play an unrated series is held back
    # (no-IMDb-data, mirroring movies), which is its own test elsewhere.
    "SCORE_BALANCE": 0,
    "TV_CLEANUP_ENABLED": True, "MAX_LIBRARY_GB": 50,
    # Every season eligible: this test's scenarios need both seasons in the
    # plan (the season-eligibility modes are pinned in test_tv_season_plan).
    "TV_SEASON_ELIGIBILITY": "all",
    # Jellyfin supplies the inventory; Sonarr is configured for the optional
    # unmonitor step (tests below also run with it absent).
    "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "jk",
    "SONARR_URL": "http://sonarr.test:8989", "SONARR_API_KEY": "k",
    "DELETE_DELAY_DAYS": 1, "RUN_MODE": "headroom", "DAILY_RUN_TIME": "00:00",
    "USE_PLEX": False,
    "TAUTULLI_URL": "", "TAUTULLI_API_KEY": "", "PLEX_URL": "", "PLEX_TOKEN": "",
}

# The pool: the measured library is 60 GB against the 50 GB Library Size Cap —
# a 10 GB deficit. No movie queue exists here, so the merged order is all
# seasons; disk and safety are stubbed steady so only the cap arithmetic
# drives the target.
A.disk_stats = lambda: {"used_gb": 500.0, "total_gb": 1000.0, "free_gb": 500.0}
A.library_stats = lambda: {"library_gb": 60.0}
A._space_threshold_state = lambda cfg, disk, lib: {"safety_blocked": False}

# Media-server inventory (the fetchers have their own test): S2 was watched
# two days ago (high keep), S1 never — so the 10 GB pool deficit takes S1 and
# only S1. The row's path is in the SERVER's namespace; scope resolution finds
# the real folder under the monitored directory by name.
def tv_rows():
    return [{
        "media_type": "tv", "path": "/tv/Dead Show",
        "tv_episodes": 3, "tv_episodes_watched": 1,
        "tv_seasons": [
            {"n": 1, "eps": 2, "size_bytes": 10 * GB, "eps_watched": 0,
             "last_played": 0, "plays": 0, "users": 0},
            {"n": 2, "eps": 1, "size_bytes": 50 * GB, "eps_watched": 1,
             "last_played": NOW - 2 * 86400, "plays": 1, "users": 1},
        ],
        "title": "Dead Show", "year": 2001, "size_gb": 60.0, "size_bytes": 60 * GB,
        "added_at": 0, "tv_status": "ended", "source_id": None,
        "jf_source_id": "jellyfin:5",
        "plays": 1, "users": 1, "rating": None, "votes": 0,
        "last_played": NOW - 2 * 86400,
    }]

A._tv_inventory_rows = lambda cfg, strict=False: tv_rows()
A._jellyfin_series_watch = lambda url, key, strict=False: ({}, set())
# The marked & eligible window reads the STORED snapshot for the eligible
# season tail; serve the same rows the pass fetches, scope-resolved the way
# the refresh stores them.
def _snapshot_rows():
    rows = tv_rows()
    A._resolve_tv_scope(rows, CFG)
    return rows
A._read_library_snapshot = lambda: ({"movies": _snapshot_rows()}, None)

# Jellyfin's episode lister (the deletion path's file source) and Sonarr's API.
CALLS = []
FAIL_PUT = [False]
JF_EPISODES = [
    {"ParentIndexNumber": 1, "Path": "/tv/Dead Show/Season 01/ep1.mkv",
     "MediaSources": [{"Size": 1000}]},
    {"ParentIndexNumber": 1, "Path": "/tv/Dead Show/Season 01/ep2.mkv",
     "MediaSources": [{"Size": 2000}]},
    {"ParentIndexNumber": 2, "Path": "/tv/Dead Show/Season 02/keeper.mkv",
     "MediaSources": [{"Size": 3000}]},
]
def fake_jellyfin_get(url, key, path, params=None, timeout=15):
    CALLS.append(("JF", path))
    if path == "/Items" and (params or {}).get("Ids") == "5":
        return {"Items": [{"Path": "/tv/Dead Show"}]}
    if path == "/Shows/5/Episodes":
        return {"Items": [dict(e) for e in JF_EPISODES]}
    raise AssertionError("unexpected Jellyfin request: " + path)
A._jellyfin_get = fake_jellyfin_get

# Two Sonarr series share the title (a 1965 original and ours): the year must
# pick ours — a title-only match would unmonitor the wrong show.
SONARR_SERIES = [{"id": 9, "title": "Dead Show", "year": 1965,
                  "seasons": [{"seasonNumber": 1, "monitored": True}]},
                 {"id": 5, "title": "Dead Show", "year": 2001,
                  "seasons": [{"seasonNumber": 1, "monitored": True},
                              {"seasonNumber": 2, "monitored": True}]}]
def fake_json_request(url, headers=None, timeout=15, method=None, payload=None):
    CALLS.append((method or "GET", url))
    if url.endswith("/api/v3/series") and method is None:
        return json.loads(json.dumps(SONARR_SERIES))
    if url.endswith("/api/v3/series/5") and method == "PUT":
        if FAIL_PUT[0]:
            raise RuntimeError("sonarr refused")
        fake_json_request.put_payload = payload
        return payload
    if url.endswith("/api/v3/command"):
        return {}
    raise AssertionError("unexpected request: " + url)
A._json_request = fake_json_request

MARK_KEY = "jellyfin:5|S1"

# ── Pass 1: marking, never same-day deletion ────────────────────────────────
reset_state()
r = A._run_tv_cleanup_pass(CFG, execute=True)
st = A._tv_cleanup_state()
check("the first pass marks exactly the take prefix",
      r["marked_new"] == 1 and list(st["marked"]) == [MARK_KEY], st["marked"])
check("the kept season is never marked", "jellyfin:5|S2" not in st["marked"])
check("a fresh mark is NOT deleted the same day",
      r["held_by_delay"] == 1 and not r["deleted_seasons"]
      and (S1 / "ep1.mkv").exists(), r)
check("no Sonarr write happened while only marking",
      not any(m == "PUT" for m, _ in CALLS), CALLS)
pool = A._pool_marked_entries(CFG)
def _season_row(entries, n):
    return [e for e in entries if e.get("media") == "tv" and e.get("season") == n]
tvrow = _season_row(pool, 1)
check("the marked season appears in the pool's ONE marked window, movie-shaped",
      len(tvrow) == 1 and tvrow[0]["marked"] and tvrow[0]["delete_on"]
      and tvrow[0]["title"] == "Dead Show"
      and tvrow[0]["size_bytes"] == 10 * GB
      and "TV Show · S1" in (tvrow[0].get("line") or "")
      and tvrow[0]["days_remaining"] >= 0, pool)
s2row = _season_row(pool, 2)
check("the UNMARKED season shows as eligible in the same window — order, no dates",
      len(s2row) == 1 and not s2row[0]["marked"] and s2row[0]["delete_on"] is None
      and s2row[0]["size_bytes"] == 50 * GB and s2row[0].get("line"), pool)
check("marked entries lead the window, eligible follow",
      pool.index(tvrow[0]) < pool.index(s2row[0]))
check("with the TV switch off the eligible seasons leave the window",
      not _season_row(A._pool_marked_entries(dict(CFG, TV_CLEANUP_ENABLED=False)), 2))

# ── Monitor Only never deletes, even a due mark ─────────────────────────────
st["marked"][MARK_KEY]["marked_at"] -= 3 * 86400   # now well past its delay
A._save_tv_cleanup_state(st)
r = A._run_tv_cleanup_pass(CFG, execute=False)
check("Monitor Only leaves a due season on disk and marked",
      (S1 / "ep1.mkv").exists() and MARK_KEY in A._tv_cleanup_state()["marked"], r)

# ── Fail-closed: a configured source that fails aborts the pass ─────────────
CFG_STRICT = dict(CFG, USE_PLEX=True,
                  TAUTULLI_URL="http://taut.test", TAUTULLI_API_KEY="t")
_orig_taut = A._tautulli_series_watch
A._tautulli_series_watch = lambda conn: (_ for _ in ()).throw(RuntimeError("down"))
r = A._run_tv_cleanup_pass(CFG_STRICT, execute=True)
A._tautulli_series_watch = _orig_taut
check("a dead configured source aborts the pass fail-closed",
      r["aborted"] and not r["deleted_seasons"] and (S1 / "ep1.mkv").exists(), r)
check("an aborted pass keeps the mark and its old clock",
      MARK_KEY in A._tv_cleanup_state()["marked"])

# ── Sonarr refusing the unmonitor blocks the deletion ───────────────────────
FAIL_PUT[0] = True
r = A._run_tv_cleanup_pass(CFG, execute=True)
FAIL_PUT[0] = False
check("an unmonitor failure skips the season with the files intact",
      (S1 / "ep1.mkv").exists() and r["skipped"]
      and "unmonitor" in r["skipped"][0]["why"], r["skipped"])
check("...and the season stays marked for the next pass",
      MARK_KEY in A._tv_cleanup_state()["marked"])

# ── A hostile path from the media server kills the season, not the guard ────
outside = Path(_OUT) / "library" / "outside.mkv"
outside.write_bytes(b"!" * 100)
JF_EPISODES.insert(0, {"ParentIndexNumber": 1, "Path": "/tv/outside.mkv"})
r = A._run_tv_cleanup_pass(CFG, execute=True)
JF_EPISODES.pop(0)
check("a file outside the series folder is refused before anything is deleted",
      outside.exists() and (S1 / "ep1.mkv").exists()
      and r["skipped"] and "unsafe" in r["skipped"][0]["why"], r["skipped"])

# ── The real deletion — with ep1's server byte count STALE ──────────────────
# The server describes ep1 at different bytes than the disk holds (a quality
# upgrade its DB hasn't caught up with). The file's identity is its path
# under the series folder and the LOCAL stat is the size authority: stale
# metadata never blocks the delete, and the freed accounting (and the
# deleted.log sizes asserted below) carry the ON-DISK bytes — ep1 is 1000 on
# disk however the server misremembers it.
_orig_size = JF_EPISODES[0]["MediaSources"][0]["Size"]
JF_EPISODES[0]["MediaSources"][0]["Size"] = 999_999
CALLS.clear()
r = A._run_tv_cleanup_pass(CFG, execute=True)
JF_EPISODES[0]["MediaSources"][0]["Size"] = _orig_size
check("a due, still-taken season is deleted — stale server bytes never block it",
      len(r["deleted_seasons"]) == 1 and r["deleted_files"] == 2
      and not r["skipped"] and r["freed_bytes"] == 3000, r)
check("its files are gone and the emptied season folder is tidied",
      not S1.exists() and not (S1 / "ep1.mkv").exists())
check("the sibling season and the series folder are untouched",
      (S2 / "keeper.mkv").exists() and SHOW.is_dir())
check("the mark is finished", MARK_KEY not in A._tv_cleanup_state()["marked"])
put = getattr(fake_json_request, "put_payload", None)
check("the season was unmonitored in Sonarr — that season and only that one",
      put and [s for s in put["seasons"] if s["seasonNumber"] == 1][0]["monitored"] is False
      and [s for s in put["seasons"] if s["seasonNumber"] == 2][0]["monitored"] is True, put)
check("...and the YEAR chose our show over the same-title 1965 one",
      put and put.get("year") == 2001
      and ("PUT", "http://sonarr.test:8989/api/v3/series/9") not in CALLS, put)
put_i = CALLS.index(("PUT", "http://sonarr.test:8989/api/v3/series/5"))
files_i = CALLS.index(("JF", "/Shows/5/Episodes"))
check("the unmonitor landed BEFORE the file list was even asked for", put_i < files_i, CALLS)
check("Sonarr is asked to rescan after the deletion",
      ("POST", "http://sonarr.test:8989/api/v3/command") in CALLS, CALLS)

log = (Path(_OUT) / "deleted.log").read_text().strip().splitlines()
check("each removed file is one deleted.log line", len(log) == 2, log)
parsed = [A._DELETED_LOG_RE.match(l) for l in log]
check("...in the engine's parseable format, sizes included",
      all(parsed) and sorted(int(m.group("size_bytes")) for m in parsed) == [1000, 2000], log)
check("...typed as TV with the season, so the history view never calls it a movie",
      all(m.group("media") == "tv" and m.group("season") == "1" for m in parsed), log)
# The share was stamped from PRE-deletion numbers; after deleting, only the
# still-pending share may remain or the engine subtracts the freed bytes twice.
import db as _db  # noqa: E402
with _db.connect(A.db_path()) as _conn:
    _share = _db.get_meta(_conn, "tv_share")
check("the share is re-stamped minus the freed bytes after a deletion",
      _share and int(_share["bytes"]) == 10 * GB - 3000, _share)

# ── Stop means stop, even mid-pass ──────────────────────────────────────────
# The engine-kill can't reach this app-side loop; the stop event must.
reset_files()
reset_state()
A._run_tv_cleanup_pass(CFG, execute=True)               # marks S1
st = A._tv_cleanup_state()
st["marked"][MARK_KEY]["marked_at"] -= 3 * 86400        # due
A._save_tv_cleanup_state(st)
A._run_stop_requested.set()
r = A._run_tv_cleanup_pass(CFG, execute=True)
A._run_stop_requested.clear()
check("a stop request halts the deletion loop with the files intact",
      not r["deleted_seasons"] and (S1 / "ep1.mkv").exists()
      and MARK_KEY in A._tv_cleanup_state()["marked"], r)

# ── Hostile series paths never resolve into scope ───────────────────────────
_hostile = tv_rows()
_hostile[0]["path"] = "/tv/.."
A._resolve_tv_scope(_hostile, CFG)
check("a server path ending in .. is out of scope, not a monitored dir's parent",
      _hostile[0]["tv_in_scope"] == 0 and _hostile[0]["path"] is None, _hostile[0]["path"])
r_root = {"skipped": [], "deleted_seasons": [], "deleted_files": 0, "freed_bytes": 0}
check("a series folder that IS a monitored root is refused outright",
      A._delete_tv_season(CFG, {"sid": "jellyfin:5", "season": 1,
                                "title": "Dead Show", "year": 2001,
                                "path": str(TV)}, r_root) is False
      and "monitored directory itself" in r_root["skipped"][0]["why"], r_root)

# ── A season the fresh plan stops taking is unmarked, never deleted ─────────
reset_files()
reset_state()
r = A._run_tv_cleanup_pass(CFG, execute=True)          # marks S1 again
st = A._tv_cleanup_state()
st["marked"][MARK_KEY]["marked_at"] -= 3 * 86400       # due — but the cap rises
A._save_tv_cleanup_state(st)
r = A._run_tv_cleanup_pass(dict(CFG, MAX_LIBRARY_GB=100), execute=True)
check("a raised cap unmarks the due season instead of deleting it",
      r["unmarked"] == 1 and not r["deleted_seasons"]
      and (S1 / "ep1.mkv").exists()
      and MARK_KEY not in A._tv_cleanup_state()["marked"], r)

# ── Sonarr cleanup as an option ─────────────────────────────────────────────
# With SONARR_CLEANUP_ENABLED off the season still deletes, but Sonarr is not
# touched: no series lookup, no unmonitor PUT — the user manages monitoring.
reset_files()
reset_state()
A._run_tv_cleanup_pass(CFG, execute=True)               # marks S1
st = A._tv_cleanup_state()
st["marked"][MARK_KEY]["marked_at"] -= 3 * 86400
A._save_tv_cleanup_state(st)
CALLS.clear()
r = A._run_tv_cleanup_pass(dict(CFG, SONARR_CLEANUP_ENABLED=False), execute=True)
check("with Sonarr cleanup off the season still deletes",
      len(r["deleted_seasons"]) == 1 and not S1.exists(), r)
check("...and Sonarr's monitoring is never touched",
      not any(m in ("PUT", "GET", "POST") and "sonarr" in u
              for m, u in CALLS), CALLS)

# ── Sonarr entirely absent: optional means optional ─────────────────────────
reset_files()
reset_state()
NO_SONARR = dict(CFG, SONARR_URL="", SONARR_API_KEY="")
A._run_tv_cleanup_pass(NO_SONARR, execute=True)         # marks S1
st = A._tv_cleanup_state()
st["marked"][MARK_KEY]["marked_at"] -= 3 * 86400
A._save_tv_cleanup_state(st)
CALLS.clear()
r = A._run_tv_cleanup_pass(NO_SONARR, execute=True)
check("with NO Sonarr configured the season still deletes",
      len(r["deleted_seasons"]) == 1 and not S1.exists(), r)
check("...and Sonarr is never called at all",
      not any("sonarr" in u for _, u in CALLS), CALLS)

# ── The per-run gate ────────────────────────────────────────────────────────
# The pass fires WITH every engine run; _tv_pass_plan_for_run decides whether
# it fires (None = no) and whether it may delete (True = a real Cleanup).
check("a real Cleanup runs the pass deleting",
      A._tv_pass_plan_for_run(CFG, "headroom") is True)
check("a Simulate runs it marking only",
      A._tv_pass_plan_for_run(CFG, "debug_sim") is False)
check("a Debug Cleanup never lets it delete",
      A._tv_pass_plan_for_run(CFG, "debug_cleanup") is False)
check("Monitor Only's maintenance run marks only",
      A._tv_pass_plan_for_run(CFG, "paused") is False)
check("no pool trigger (no cap, no headroom) → no pass",
      A._tv_pass_plan_for_run(dict(CFG, MAX_LIBRARY_GB=None), "headroom") is None)
check("no media server to supply the inventory → no pass (Sonarr alone is "
      "NOT enough — it is cleanup-only)",
      A._tv_pass_plan_for_run(dict(CFG, USE_JELLYFIN=False), "headroom") is None)
check("the TV switch off → no pass",
      A._tv_pass_plan_for_run(dict(CFG, TV_CLEANUP_ENABLED=False), "headroom") is None)
_orig_breach = A._redline_breached_now
A._redline_breached_now = lambda cfg: True
check("a breached Redline emergency skips the pass — the engine goes first",
      A._tv_pass_plan_for_run(CFG, "headroom") is None)
A._redline_breached_now = _orig_breach

# ── The Dashboard's status line ─────────────────────────────────────────────
st = A._tv_pass_status(CFG)
check("the status line carries the last pass's outcome and the mark count",
      st and st["armed"] and st.get("date") and "deleted_seasons" in st
      and "marked" in st, st)
check("the TV switch off silences the line",
      A._tv_pass_status(dict(CFG, TV_CLEANUP_ENABLED=False)) is None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
