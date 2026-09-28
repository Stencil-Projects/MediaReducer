"""A manual Cleanup splits the deficit it faces NOW between seasons and movies.

The season pass executes "takes" — the seasons the last full scan's merge gave
the season side — and that merge was sized to the deficit of the scan that
made it. Found in the test lab: the library sat under a 138 GB cap when the
last Simulate ran (so the takes were empty), the cap was lowered to 120 GB and
saved, and Cleanup was pressed. The season pass took nothing, and the movie
side — "free it now" — covered all 12.6 GB: Morbius (25.6), Pacific Rim (47.0)
and Battleship (55.5), while Manifest S1 (33.3) and Riverdale S1 (40.7) sat
below the last two in the very pool order the Filtering page showed. Container
log: "TV cleanup (cleanup): pool deficit 12.6 GB → seasons 0.0 GB, movies
12.6 GB".

Pinned: a manual (immediate) pass merges today's eligible seasons with the
stored movie queue and deletes the seasons inside the covering prefix — here
the two — whatever the stale stamp says; the movie share left for the engine is
what remains; and a scheduled pass still executes the last scan's merge, whose
marks wait out the delay.

Hermetic: the inventory, plan, queue and deleter are stubs.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-manual-split.")
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
# The lab's figures, the bytes as the store holds them.
SEASONS = [
    {"title": "Manifest", "season": 1, "year": 2018, "sid": "jellyfin:manifest",
     "path": "/library/tv/Manifest (2018)", "size_bytes": 3_727_687_680, "score": 33.255},
    {"title": "Riverdale", "season": 1, "year": 2017, "sid": "jellyfin:riverdale",
     "path": "/library/tv/Riverdale (2017)", "size_bytes": 4_408_213_504, "score": 40.701},
    {"title": "Breaking Bad", "season": 1, "year": 2008, "sid": "jellyfin:bb",
     "path": "/library/tv/Breaking Bad (2008)", "size_bytes": 4_287_627_264, "score": 65.216},
]
QUEUE = {
    "/library/movies/Morbius (2022)/Morbius (2022).mkv": {"title": "Morbius", "score": 25.586, "size_bytes": 6_625_951_744},
    "/library/movies/Pacific Rim (2013)/Pacific Rim (2013).mkv": {"title": "Pacific Rim", "score": 47.01, "size_bytes": 3_089_104_896},
    "/library/movies/Battleship (2012)/Battleship (2012).mkv": {"title": "Battleship", "score": 55.53, "size_bytes": 6_295_650_304},
    "/library/movies/Dune (2021)/Dune (2021).mkv": {"title": "Dune", "score": 65.637, "size_bytes": 7_426_015_232},
}
DEFICIT = int(12.6 * GB)   # library 132.6 GB against a 120 GB cap

_state = {"marked": {}}
A._tv_cleanup_state = lambda: _state
A._save_tv_cleanup_state = lambda st: None
A._tv_fresh_rows_strict = lambda cfg: [{"title": s["title"]} for s in SEASONS]
A._tv_season_plan = lambda rows, cfg, now=None: {
    "order": [dict(s, take=False) for s in SEASONS], "excluded": {}}
A._space_threshold_state = lambda *a, **k: {"safety_blocked": False}
A._pool_deletion_target_bytes = lambda cfg: DEFICIT
A._pending_raw = lambda: {k: dict(v) for k, v in QUEUE.items()}
A._stamp_tv_share = lambda b: _stamps.append(b)
A._stamp_tv_season_order = lambda order, run: None
A._refresh_tv_inventory = lambda cfg: None
# The last full scan's merge: the library was under the cap then, so it gave
# the seasons nothing. This is the stamp a manual Cleanup used to execute.
A._engine_takes_for_pass = lambda order, cfg: (
    {}, {"target_bytes": DEFICIT, "tv_share_bytes": 0, "movie_share_bytes": DEFICIT})
_stamps, deleted = [], []


def _delete(cfg, entry, report):
    deleted.append((entry["title"], entry["season"]))
    report["deleted_seasons"].append({"title": entry["title"], "season": entry["season"]})
    report["freed_bytes"] += int(entry["size_bytes"])
    return True


A._delete_tv_season = _delete
CFG = {"OUTPUT_DIR": _OUT, "TV_CLEANUP_ENABLED": True, "DELETE_DELAY_DAYS": 1,
       "NEAR_TIE_PTS": 2}

# ── The manual Cleanup: a fresh split of today's deficit ──────────────────
rep = A._run_tv_cleanup_pass(dict(CFG), execute=True, immediate=True, run_started_at=1000.5)
check("the seasons inside today's covering prefix are deleted",
      deleted == [("Manifest", 1), ("Riverdale", 1)], deleted)
check("...the higher-scored season is left alone", ("Breaking Bad", 1) not in deleted)
check("the pass reports the split it used",
      rep["tv_share_gb"] == 8.1 and rep["movie_share_gb"] == 4.5,
      (rep["tv_share_gb"], rep["movie_share_gb"]))
check("...and after deleting, no season bytes remain claimed from the movie side",
      _stamps and _stamps[-1] == 0, _stamps)

# ── A scheduled pass still executes the last scan's merge ────────────────
deleted.clear(); _state["marked"] = {}
rep = A._run_tv_cleanup_pass(dict(CFG), execute=True, immediate=False, run_started_at=2000.5)
check("a scheduled pass keeps executing the last scan's merge (here: nothing)",
      deleted == [] and rep["tv_share_gb"] == 0, (deleted, rep["tv_share_gb"]))

# ── A Dashboard Simulate is `manual` too, and must not split early ────────
# `immediate` rides on `manual`, which every Dashboard button sets. A dry run's
# pass splitting against the queue it is about to rebuild claimed its own share
# before the scan ran (the scenario suite caught it: the run then logged a
# target the engine's own split disagreed with).
deleted.clear(); _state["marked"] = {}
rep = A._run_tv_cleanup_pass(dict(CFG), execute=False, immediate=True, run_started_at=3000.5)
check("a manual Simulate's pass executes the last scan's merge, not a fresh split",
      deleted == [] and rep["tv_share_gb"] == 0, (deleted, rep["tv_share_gb"]))

# ── The split itself is the engine's, from shared.py ─────────────────────
src = (ROOT / "engine.py").read_text(encoding="utf-8")
check("the engine's merge and the app's are one piece of code",
      "return shared.split_pool(" in src
      and "_least_wasteful_index = shared.least_wasteful_index" in src)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
