"""A config-save reconcile splits the pool with the seasons, as a full scan does.

The reconcile rebuilds the movie plan from the stored snapshot after a
threshold, scoring or filter save. It sized the movie marks to the deficit less
the LAST full scan's season share. Found in the test lab: the library sat under
a 138 GB cap at the last Simulate (season share 0), the cap was lowered to 120 GB
and saved, and the rebuilt plan marked Morbius, Pacific Rim (47.0) and Battleship
(55.5) — while Manifest S1 (33.3) and Riverdale S1 (40.7) sat below the last two
in the pool order and stayed unmarked. The dashboard's next deletion read "3
MOVIES, 16.0 GB".

Pinned: with this reconcile's season order stamped (the app does that from the
snapshot), the engine's reconcile merges it with the re-scored movies, marks
movies only for the remainder, and stamps the season takes; without one it
stays movie-only. And the app's reconcile worker stamps the order under an
identity of its own, hands that identity to the engine, and marks the takes
when the engine is done.

Hermetic: temp store, stubbed disk, no subprocess.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate  # noqa: E402
_OUT = tempfile.mkdtemp(prefix="mr-reconcile-split.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import db       # noqa: E402
import engine as E  # noqa: E402
import _tmpout  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


E.log = lambda *a, **k: None
_tmpout.redirect_engine(E, Path(_OUT))
GB = 1_000_000_000
NOW = 1_700_000_000
E.MOVIE_CLEANUP_ENABLED = True
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
E.HEADROOM_GB = 506   # deficit = 506 - 500 free = 6 GB
DISK = {"total": 1000 * GB, "used": 500 * GB, "free": 500 * GB}
E.get_usage_info = lambda: {
    "total": DISK["total"], "used": DISK["used"], "free": DISK["free"],
    "used_gb": DISK["used"] / GB, "max_gb": DISK["total"] / GB - (E.HEADROOM_GB or 0)}
CHECKSUM = E.code_checksum()


def movie(path, plays):
    return {"path": path, "title": Path(path).stem, "year": 2020, "rating": 6.0,
            "votes": 1000, "plays": plays, "users": 1, "last_played": 0,
            "added_at": 1_400_000_000, "size_gb": 2.0, "size_bytes": 2 * GB,
            "protected": False, "favorite": False, "excluded": False,
            "source_id": path, "jf_source_id": None, "tmdb_id": None, "section_id": "1"}


P = [f"/lib/M{n}.mkv" for n in range(5)]


def seed():
    _dbstate.seed(E.DB_FILE, {"code_checksum": CHECKSUM, "library_snapshot": {
        "built_at": NOW, "monitor_dirs": E.MONITOR_DIRS,
        "movies": [movie(P[n], plays=n) for n in range(5)]}})


def marked():
    q = db.read_pending_doc(E.DB_FILE).get("entries", {})
    return {k for k, e in q.items() if e.get("marked_at") is not None}


def stamp_order(run, entries):
    with db.transaction(E.DB_FILE) as conn:
        db.set_meta(conn, "tv_season_order", {"run_started_at": run, "at": time.time(),
                                              "entries": entries})
        db.set_meta(conn, "tv_share", {"bytes": 0, "at": time.time()})   # the last scan's: none


RID = 1_790_000_000.25
# A season that scores below every movie, 4 GB: of the 6 GB deficit it covers
# 4, and the movies owe the remaining 2 — one 2 GB movie, not three.
SEASON = {"key": "jellyfin:low|S1", "score": 0.0, "size_bytes": 4 * GB}

# ── Without this reconcile's season order: movie-only, as before ─────────
seed()
os.environ["MEDIAREDUCER_RUN_STARTED_AT"] = repr(RID)
stamp_order(RID + 50, [SEASON])   # another run's order — must not be used
E.reconcile_from_snapshot(trigger="test")
check("with no season order of its own, the reconcile marks movies for the whole deficit",
      len(marked()) == 3, marked())

# ── With it: the pool is split, movies cover only the remainder ──────────
seed()
stamp_order(RID, [SEASON])
E.reconcile_from_snapshot(trigger="test")
check("with this reconcile's season order, movies are marked only for the remainder",
      marked() == {P[0]}, marked())
with db.connect(E.DB_FILE) as conn:
    takes = db.get_meta(conn, "tv_takes") or {}
    share = db.get_meta(conn, "tv_share") or {}
check("...and the season's take is stamped for the app to mark",
      [t.get("key") for t in takes.get("entries") or []] == [SEASON["key"]]
      and E.shared.same_run(takes.get("run_started_at"), RID), takes)
check("...with its share, so the upkeep leaves those bytes to the season",
      share.get("bytes") == 4 * GB, share)

# ── The app's worker: stamps, hands over the identity, marks afterwards ──
import app as A  # noqa: E402
ORDER = [{"title": "Low Show", "season": 1, "year": 2001, "sid": "jellyfin:low", "score": 0.0,
          "path": "/lib/tv/Low Show", "size_bytes": 4 * GB}]
A._reconcile_season_order = lambda cfg: [dict(e) for e in ORDER]
got = {}


def fake_engine(config_path, *, refetch, trigger, timeout=600, run_started_at=None):
    got["rid"] = run_started_at
    with db.connect(A.db_path()) as conn:
        got["order"] = db.get_meta(conn, "tv_season_order")
    with db.transaction(A.db_path()) as conn:   # what the engine's split stamps
        db.set_meta(conn, "tv_takes", {"run_started_at": run_started_at, "at": time.time(),
                                       "entries": [{"key": "jellyfin:low|S1", "size_bytes": 4 * GB}]})
    return True


A._run_reconcile_subprocess = fake_engine
A._save_tv_cleanup_state({"marked": {}})
A._reconcile_worker(False, "test")
check("the worker stamps this reconcile's season order under its own identity",
      got.get("rid") and E.shared.same_run((got.get("order") or {}).get("run_started_at"), got["rid"])
      and [e["key"] for e in got["order"]["entries"]] == ["jellyfin:low|S1"], got)
check("...and marks the seasons the engine's split took once it is done",
      set(A._tv_cleanup_state().get("marked") or {}) == {"jellyfin:low|S1"},
      A._tv_cleanup_state().get("marked"))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
