"""Config-save reconcile (reconcile_from_snapshot): rebuild the marked & eligible
queue from the stored library snapshot under new filtering/scoring/threshold config
— NO library walk, and NO media-server fetch unless a protection source changed.

Proves the properties the feature promises:
  • scores + eligibility are recomputed from the snapshot's stored facts, both
    directions (turning a filter off RE-ADMITS movies, on removes them);
  • the marked prefix tracks the current space target;
  • a still-marked movie KEEPS its delay clock (marked_at), a newly-marked one gets
    a fresh clock, and a now-ineligible one drops off;
  • the stored `excluded` flag (an identity mismatch) is never re-admitted;
  • the refetch path updates protected/favorite facts from injected server lookups.

Fully hermetic: a temp DB, a stubbed disk read, and injected fetchers — no network,
no filesystem walk."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
_OUT = tempfile.mkdtemp(prefix="mr-reconcile.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
import db
import engine as E
import _tmpout

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
_tmpout.redirect_engine(E, Path(_OUT))
GB = 1_000_000_000
NOW = 1_700_000_000
SEED_TS = NOW - 5 * 86400          # an old delay clock, to prove preservation

# Deterministic scoring: 100% watch history, no IMDb, wide staleness window, so a
# movie's score is a clean function of its play count (fewer plays → deleted first).
E.SCORE_BALANCE = 0
E.HISTORY_WEIGHT, E.QUALITY_WEIGHT = E.score_balance_weights(0)
E.MAX_STALENESS_MONTHS = 36
E.MAX_IMDB_RATING = None
E.GRACE_PERIOD_DAYS = 0
E.SKIP_UNPLAYED_MOVIES = False
E.PROTECT_JELLYFIN_FAVORITES = False
E.REDLINE_ONLY_MODE = False
E.REDLINE_GB = None
E.MAX_LIBRARY_GB = None
E.NEAR_TIE_PTS = 0.0
E.DELETE_DELAY_DAYS = 3
E.USE_PLEX = True
E.USE_JELLYFIN = False
E.MONITOR_DIRS = ["/lib"]
# Headroom target so the deficit = HEADROOM_GB - free = 506 - 500 = 6 GB → 3 marks.
E.HEADROOM_GB = 506

# Deterministic disk: 1000 GB total, 500 used / 500 free.
DISK = {"total": 1000 * GB, "used": 500 * GB, "free": 500 * GB}
E.get_usage_info = lambda: {
    "total": DISK["total"], "used": DISK["used"], "free": DISK["free"],
    "used_gb": DISK["used"] / GB, "max_gb": DISK["total"] / GB - (E.HEADROOM_GB or 0)}

CHECKSUM = E.code_checksum()   # seed with the running checksum so ensure_code_current is a no-op

def movie(path, plays, *, size=2 * GB, added=1_400_000_000, last=0, users=1,
          rating=6.0, votes=1000, protected=False, favorite=False, excluded=False,
          tmdb=None, section="1", jf=None):
    return {"path": path, "title": Path(path).stem, "year": 2020, "rating": rating,
            "votes": votes, "plays": plays, "users": users, "last_played": last,
            "added_at": added, "size_gb": round(size / 1e9, 2), "size_bytes": size,
            "protected": protected, "favorite": favorite, "excluded": excluded,
            "source_id": (jf or path), "jf_source_id": jf,
            "tmdb_id": tmdb, "section_id": section}

def seed(movies, queue=None):
    doc = {"code_checksum": CHECKSUM,
           "library_snapshot": {"built_at": NOW, "monitor_dirs": E.MONITOR_DIRS, "movies": movies}}
    if queue is not None:
        doc["pending"] = {"schema": 1, "entries": queue}
    _dbstate.seed(E.DB_FILE, doc)

def qentry(marked_at=None, score=0.0):
    return {"title": "m", "score": score, "size_bytes": 2 * GB,
            "marked_at": marked_at, "tmdb_id": None, "section_id": "1"}

def queue_now():
    return db.read_pending_doc(E.DB_FILE).get("entries", {})

def marked(entries):
    return {k for k, e in entries.items() if e.get("marked_at") is not None}

P = [f"/lib/M{n}.mkv" for n in range(5)]

# ══ 1. Basic reconcile: eligible list + target-sized marks + marked_at preserved ══
# 5 movies, 2 GB each, plays 0..4 → deletion order M0,M1,M2,M3,M4 (fewest plays
# first). 6 GB deficit → mark M0,M1,M2. Seed M1 already marked with an OLD clock.
movies = [movie(P[n], plays=n) for n in range(5)]
seed(movies, queue={P[1]: qentry(marked_at=SEED_TS)})
E.reconcile_from_snapshot(trigger="test")
q = queue_now()
check("every eligible movie is in the queue", set(q) == set(P))
check("the target-covering prefix (3 movies) is marked", len(marked(q)) == 3)
check("the marked set is the 3 lowest-scored (fewest plays)",
      marked(q) == {P[0], P[1], P[2]})
check("a still-marked movie KEEPS its delay clock",
      q[P[1]]["marked_at"] == SEED_TS)
check("a newly-marked movie gets a FRESH clock (not the old seed, not None)",
      q[P[0]]["marked_at"] not in (None, SEED_TS)
      and q[P[2]]["marked_at"] not in (None, SEED_TS))
check("movies beyond the target stay eligible-but-unmarked",
      q[P[3]]["marked_at"] is None and q[P[4]]["marked_at"] is None)
check("scores were recomputed onto the queue",
      all(isinstance(q[p]["score"], (int, float)) for p in P))

# ══ 2. Turn a filter ON removes movies; turning it OFF re-admits them ═════════════
# Skip-unplayed ON → the 0-play movie (M0) becomes ineligible and drops off.
E.SKIP_UNPLAYED_MOVIES = True
E.reconcile_from_snapshot(trigger="test")
q = queue_now()
check("turning skip-unplayed ON drops the 0-play movie from the queue", P[0] not in q)
check("the still-eligible movies remain", set(q) == {P[1], P[2], P[3], P[4]})
# Back OFF → the 0-play movie is re-admitted with NO rescan (it was in the snapshot).
E.SKIP_UNPLAYED_MOVIES = False
E.reconcile_from_snapshot(trigger="test")
check("turning skip-unplayed OFF re-admits the 0-play movie", P[0] in queue_now())

# ══ 3. A stored fact excludes a movie: protected / favorite ══════════════════════
prot = [movie(P[0], 0, protected=True), movie(P[1], 1), movie(P[2], 2)]
seed(prot)
E.reconcile_from_snapshot(trigger="test")
check("a protected movie is excluded from the queue", P[0] not in queue_now())

fav = [movie(P[0], 0, favorite=True), movie(P[1], 1)]
seed(fav)
E.PROTECT_JELLYFIN_FAVORITES = True
E.reconcile_from_snapshot(trigger="test")
check("a favorite is excluded when favorites protection is ON", P[0] not in queue_now())
E.PROTECT_JELLYFIN_FAVORITES = False
E.reconcile_from_snapshot(trigger="test")
check("the favorite is eligible again once favorites protection is OFF", P[0] in queue_now())

# ══ 4. The `excluded` flag (identity mismatch) is never re-admitted ══════════════
exc = [movie(P[0], 0, excluded=True), movie(P[1], 1)]
seed(exc)
E.reconcile_from_snapshot(trigger="test")
check("an `excluded` (identity-mismatch) movie is never eligible", P[0] not in queue_now())

# ══ 5. A threshold change re-sizes the marked set (no eligibility change) ═════════
movies = [movie(P[n], plays=n) for n in range(5)]
seed(movies)
E.HEADROOM_GB = 504          # deficit 4 GB → 2 marks
E.reconcile_from_snapshot(trigger="test")
check("a smaller headroom target marks fewer movies", len(marked(queue_now())) == 2)
E.HEADROOM_GB = 508          # deficit 8 GB → 4 marks
E.reconcile_from_snapshot(trigger="test")
check("a larger headroom target marks more movies", len(marked(queue_now())) == 4)
E.HEADROOM_GB = 506

# ══ 6. The refetch path updates protected/favorite facts from server lookups ═════
# The snapshot says nothing is protected; the injected lookup says M0's path is now
# in a protected collection → after a refetch reconcile it's excluded, and the
# refreshed fact is persisted to the snapshot.
movies = [movie(P[0], 0), movie(P[1], 1), movie(P[2], 2)]
seed(movies)
E.fetch_protected_paths = lambda: ({P[0]}, set(), set(), set())
E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
E._jellyfin_favorite_paths = lambda: set()
E.reconcile_from_snapshot(trigger="collections", refetch_protection=True)
check("a refetch reconcile excludes a newly-protected movie", P[0] not in queue_now())
with db.connect(E.DB_FILE) as conn:
    _snap = {m["path"]: m for m in db.read_snapshot(conn)["movies"]}
check("the refreshed protection fact is persisted to the snapshot",
      _snap[P[0]]["protected"] is True)

# ── A monitored-path change is not something a snapshot can be re-read for ──
# The snapshot holds what was on disk under the paths it was SCANNED under. Left
# unchecked, adding a branch produced a plan that could not see it — and then
# write_plan_to_queue stamped that plan with the CURRENT paths, so it read as
# fresh: Cleanup un-ghosted, arming was allowed, and the fast path deleted from a
# plan blind to part of the library.
_saved_dirs = E.MONITOR_DIRS
try:
    seed([movie("/lib/A.mkv", 0), movie("/lib/B.mkv", 0), movie("/lib/C.mkv", 0)],
         queue={})
    E.MONITOR_DIRS = ["/lib", "/lib2"]           # a branch added since that scan
    E.reconcile_from_snapshot(trigger="paths changed")
    _doc = db.read_pending_doc(E.DB_FILE)
    check("a monitored-path change leaves the queue alone", not _doc.get("entries"))
    check("...and does not stamp a plan the snapshot cannot back",
          not _doc.get("monitor_dirs"))
finally:
    E.MONITOR_DIRS = _saved_dirs

# The same reconcile with the paths unchanged still does its job, so the guard
# above is a path check and not an outage.
seed([movie("/lib/A.mkv", 0), movie("/lib/B.mkv", 0), movie("/lib/C.mkv", 0)], queue={})
E.reconcile_from_snapshot(trigger="unchanged paths")
check("unchanged paths still rebuild the queue", len(queue_now()) == 3)

# ══ 7. No snapshot → the reconcile is a safe no-op ═══════════════════════════════
_dbstate.reset(E.DB_FILE)
seed([])                      # empty snapshot
E.reconcile_from_snapshot(trigger="test")
check("an empty snapshot reconciles to an empty queue (no crash)", queue_now() == {})


# ══════════════════════════════════════════════════════════════════════════
# reconcile_from_snapshot must honor the CURRENT MOVIE_EXTENSIONS. The snapshot
# may hold rows scanned under a different extension set (e.g. .mkv removed from the
# config by a hand-edit); since the reconcile re-stamps the plan as current WITHOUT a
# rescan, it must drop a now-ineligible extension — otherwise a Cleanup would delete
# files the new config excludes (a full Simulate skips them as bad_extension).
# MOVIE_EXTENSIONS is a plan key, so stamping it without applying it is the trap.
# ══════════════════════════════════════════════════════════════════════════

E.log = lambda *a, **k: None
E.SCORE_BALANCE = 0
E.HISTORY_WEIGHT, E.QUALITY_WEIGHT = E.score_balance_weights(0)
E.MAX_STALENESS_MONTHS = 36
E.MAX_IMDB_RATING = None
E.GRACE_PERIOD_DAYS = 0
E.SKIP_UNPLAYED_MOVIES = False
E.PROTECT_JELLYFIN_FAVORITES = False
E.REDLINE_ONLY_MODE = False
E.REDLINE_GB = None
E.MAX_LIBRARY_GB = None
E.NEAR_TIE_PTS = 0.0
E.USE_PLEX = True
E.USE_JELLYFIN = False
E.MONITOR_DIRS = ["/lib"]
E.HEADROOM_GB = 506
GB = 1_000_000_000
NOW = 1_700_000_000
DISK = {"total": 1000 * GB, "used": 500 * GB, "free": 500 * GB}
E.get_usage_info = lambda: {
    "total": DISK["total"], "used": DISK["used"], "free": DISK["free"],
    "used_gb": DISK["used"] / GB, "max_gb": DISK["total"] / GB - (E.HEADROOM_GB or 0)}


def movie(path, plays, size=2 * GB):
    return {"path": path, "title": Path(path).stem, "year": 2020, "rating": 6.0,
            "votes": 1000, "plays": plays, "users": 1, "last_played": 0,
            "added_at": 1_400_000_000, "size_gb": round(size / 1e9, 2), "size_bytes": size,
            "protected": False, "favorite": False, "excluded": False,
            "source_id": path, "jf_source_id": None, "tmdb_id": None, "section_id": "1"}


def reconcile_queue(td, exts):
    _tmpout.redirect_engine(E, Path(td))
    E.MOVIE_EXTENSIONS = set(exts)
    movies = [movie("/lib/A.mkv", 0), movie("/lib/B.mp4", 1), movie("/lib/C.mkv", 2)]
    _dbstate.seed(E.DB_FILE, {
        "code_checksum": E.code_checksum(),
        "library_snapshot": {"built_at": NOW, "monitor_dirs": E.MONITOR_DIRS, "movies": movies}})
    E.reconcile_from_snapshot(trigger="test")
    return set(db.read_pending_doc(E.DB_FILE).get("entries", {}))


with tempfile.TemporaryDirectory() as td:
    q = reconcile_queue(td, {".mkv", ".mp4"})
    check("all extensions eligible: every movie is queued",
          q == {"/lib/A.mkv", "/lib/B.mp4", "/lib/C.mkv"})

with tempfile.TemporaryDirectory() as td:
    q = reconcile_queue(td, {".mp4"})   # .mkv removed from the config since the scan
    check("removing .mkv drops the .mkv rows from the reconciled queue", q == {"/lib/B.mp4"})
    check("neither .mkv movie is left deletable by the reconcile",
          "/lib/A.mkv" not in q and "/lib/C.mkv" not in q)


# ══════════════════════════════════════════════════════════════════════════
# _refresh_snapshot_protection (the collections/favorites-change reconcile) must
# keep a genuinely-protected movie protected even when the snapshot's stored path
# (symlink-RESOLVED) differs from the freshly-fetched protection path (as-built) —
# otherwise a symlinked library clears the flag and re-admits the movie to the
# eligible queue. An exact-path compare cannot do that, so this uses the same
# _make_protection_check the deletion paths use.
# ══════════════════════════════════════════════════════════════════════════

E.log = lambda *a, **k: None

with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "lib")
    film_dir = lib / "Movies" / "Film (2020)"; film_dir.mkdir(parents=True)
    (film_dir / "film.mkv").write_bytes(b"\0" * 1024)
    other_dir = lib / "Movies" / "Other (2019)"; other_dir.mkdir(parents=True)
    (other_dir / "other.mkv").write_bytes(b"\0" * 1024)
    (lib / "PlexMovies").symlink_to(lib / "Movies", target_is_directory=True)
    E.LIBRARY_ROOT = lib
    E.MONITOR_DIRS = [str(lib / "Movies")]
    E.DB_FILE = Path(td, "mediareducer.db")

    resolved_row_path = str((lib / "Movies" / "Film (2020)" / "film.mkv"))    # snapshot form (resolved)
    protection_via_symlink = str(lib / "PlexMovies" / "Film (2020)" / "film.mkv")  # fetched form (as-built)
    other_path = str(lib / "Movies" / "Other (2019)" / "other.mkv")

    # Protection fetch reports the file through the symlinked share; favorites off.
    E.USE_PLEX = True
    E.USE_JELLYFIN = False
    E.PROTECTED_COLLECTIONS = ["Keep"]
    E.fetch_protected_paths = lambda: ({protection_via_symlink}, set(), set(), set())
    E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
    E._jellyfin_favorite_paths = lambda: set()

    # Sanity: an exact compare of these two paths clears the flag.
    check("baseline: resolved row path != as-built protection path",
          resolved_row_path not in {protection_via_symlink})

    rows = [
        {"path": resolved_row_path, "protected": 0, "favorite": 0, "title": "Film"},
        {"path": other_path, "protected": 0, "favorite": 0, "title": "Other"},
    ]
    E._refresh_snapshot_protection(rows)

    check("symlinked-protected movie stays protected after the reconcile refresh",
          rows[0]["protected"] is True)
    check("an unprotected movie stays unprotected", rows[1]["protected"] is False)


# ── Cap target judges the ONE measured library figure (never a snapshot sum) ──
# The measured on-disk size (dashboard_stats) is the number the GUI shows and
# every other threshold check uses; the reconcile must judge the user's cap
# against that same figure. With no measurement yet, the cap contributes no
# deficit (the next tick measures first) — it never invents a size from the
# snapshot's summed files.
with tempfile.TemporaryDirectory() as td:
    E.DB_FILE = Path(td, "mediareducer.db")
    E.MAX_LIBRARY_GB = 0.2
    E.HEADROOM_GB = 0
    E.REDLINE_GB = None
    E.REDLINE_ONLY_MODE = False
    E._reconcile_disk_stats = lambda: {"used_gb": 10.0, "total_gb": 100.0, "free_gb": 90.0}
    # No measured stats yet → the cap contributes nothing (no invented size).
    t, rl = E._reconcile_target_bytes()
    check("no measured size: cap contributes no deficit", t == 0 and rl is False)
    # The store's measured library (what the GUI shows) says 0.33 GB → deficit.
    with db.transaction(E.DB_FILE) as conn:
        db.ensure_code_current(conn, E.code_checksum())
        db.set_meta(conn, "dashboard_stats", {"library_gb": 0.33})
    t, rl = E._reconcile_target_bytes()
    check("measured size drives the cap deficit (0.33 - 0.2 GB)",
          abs(t - 130_000_000) < 5_000_000 and rl is False)
    # Headroom still sizes from fresh disk regardless of the library figure.
    E.MAX_LIBRARY_GB = None
    E.HEADROOM_GB = 95.0   # max usage 5 GB, used 10 GB → 5 GB over
    t, rl = E._reconcile_target_bytes()
    check("headroom deficit from fresh disk", abs(t - 5_000_000_000) < 5_000_000 and rl is False)

# ── The two MOVIE_EXTENSIONS defaults must never drift apart ──────────────────
# default_config.json seeds real installs; the engine's module-level set covers
# a config file missing the key. Both must name the same formats, or the size
# walk and the scan could disagree on what a movie file is.
import json as _json
import re as _re
_dflt = set(_json.load(open(Path(__file__).resolve().parents[2] / "default_config.json"))["MOVIE_EXTENSIONS"])
# Compare against the engine SOURCE (E's module value may carry test mutations).
_src = (Path(__file__).resolve().parents[2] / "engine.py").read_text()
_m = _re.search(r"MOVIE_EXTENSIONS = \{([^}]+)\}", _src)
_engine_dflt = set(_re.findall(r'"(\.[a-z0-9]+)"', _m.group(1))) if _m else set()
check("engine default MOVIE_EXTENSIONS matches default_config.json", _engine_dflt == _dflt)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
