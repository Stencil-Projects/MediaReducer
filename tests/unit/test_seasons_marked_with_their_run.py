"""The seasons a run's merge takes are marked by that run, and count in the plan.

The engine's full-scan merge decides which seasons cover part of the pool's
deficit ("takes"). They used to be marked only by the NEXT run's season pass.
Found in the test lab: after lowering the cap below the library, a Simulate
whose plan was all seasons ended "Dry run — space limits are satisfied, nothing
marked", the dashboard said "Over space limits — run Simulate to mark the
~6.6 GB deletion plan", and only a second Simulate put the two seasons in the
Marked list. The next-deletion figures counted movies alone, so even marked
seasons left it saying there was no plan. In Automatic Cleanup the late mark
cost a day: seasons deleted a day after the films split with them.

Pinned: _mark_engine_takes marks THIS run's takes when the run ends (a mark it
keeps keeps its clock, one it no longer takes is dropped, another run's merge
is ignored), and the forecast's next deletion counts marked seasons, split by
type for the wording.

Hermetic: temp store, stubbed plan.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-season-mark.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT, "DELETE_DELAY_DAYS": 1}))
import app as A  # noqa: E402
import db       # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


RUN = 1_790_482_900.5
ORDER = [
    {"title": "Manifest", "season": 1, "year": 2018, "sid": "jellyfin:m", "score": 33.3,
     "path": "/library/tv/Manifest (2018)", "size_bytes": 3_727_687_680},
    {"title": "Riverdale", "season": 1, "year": 2017, "sid": "jellyfin:r", "score": 40.7,
     "path": "/library/tv/Riverdale (2017)", "size_bytes": 4_408_213_504},
    {"title": "Breaking Bad", "season": 1, "year": 2008, "sid": "jellyfin:b", "score": 65.2,
     "path": "/library/tv/Breaking Bad (2008)", "size_bytes": 4_287_627_264},
]
KEY = {e["title"]: A._tv_mark_key(e) for e in ORDER}


def stamp_takes(run, titles):
    with db.transaction(A.db_path()) as conn:
        db.set_meta(conn, "tv_takes", {"run_started_at": run, "at": time.time(),
                                       "entries": [{"key": KEY[t], "size_bytes": 1} for t in titles]})


def marks():
    return A._tv_cleanup_state().get("marked") or {}


A._save_tv_cleanup_state({"marked": {}, "last_pass": {"run_started_at": RUN, "marked_new": 0,
                                                      "unmarked": 0}})
A._TV_PASS_PLAN.clear()
A._TV_PASS_PLAN.update(run=RUN, order=[dict(e) for e in ORDER])

# ── The run's own merge is marked as it ends ──────────────────────────────
stamp_takes(RUN, ["Manifest", "Riverdale"])
report = {"marked_new": 0, "unmarked": 0}
n = A._mark_engine_takes(RUN, report)
m = marks()
check("the seasons this run's merge took are marked by this run",
      n == 2 and set(m) == {KEY["Manifest"], KEY["Riverdale"]}, (n, list(m)))
check("...each with the facts the Marked list and the pass need",
      m[KEY["Manifest"]]["title"] == "Manifest" and m[KEY["Manifest"]]["size_bytes"] == 3_727_687_680
      and m[KEY["Manifest"]]["delay_days"] == 1 and m[KEY["Manifest"]]["marked_at"] > 0, m)
check("...and the run's report says it marked them",
      report["marked_new"] == 2 and A._tv_cleanup_state()["last_pass"]["marked_new"] == 2,
      (report, A._tv_cleanup_state().get("last_pass")))

# ── A later run keeps what it still takes, clock and all; drops the rest ──
first_clock = m[KEY["Riverdale"]]["marked_at"]
RUN2 = RUN + 86400
A._TV_PASS_PLAN.update(run=RUN2, order=[dict(e) for e in ORDER])
stamp_takes(RUN2, ["Riverdale"])
A._mark_engine_takes(RUN2, None)
m = marks()
check("a season the next merge still takes keeps its delay clock",
      set(m) == {KEY["Riverdale"]} and m[KEY["Riverdale"]]["marked_at"] == first_clock, m)
check("...and one it no longer takes is unmarked", KEY["Manifest"] not in m)

# ── Another run's merge is never applied ──────────────────────────────────
stamp_takes(RUN2 + 999, ["Breaking Bad"])
check("a merge from a different run marks nothing",
      A._mark_engine_takes(RUN2, None) == 0 and KEY["Breaking Bad"] not in marks())
A._TV_PASS_PLAN.update(run=RUN2 + 5, order=[])
stamp_takes(RUN2, ["Breaking Bad"])
check("...nor does a merge whose plan is not this run's",
      A._mark_engine_takes(RUN2, None) == 0 and KEY["Breaking Bad"] not in marks())

# ── The next deletion counts marked seasons, by type ─────────────────────
A._save_tv_cleanup_state({"marked": {
    KEY["Manifest"]: {"marked_at": time.time() - 3 * 86400, "delay_days": 1, "title": "Manifest",
                      "season": 1, "path": "/library/tv/Manifest (2018)", "size_bytes": 3_727_687_680,
                      "score": 33.3},
    KEY["Riverdale"]: {"marked_at": time.time() - 3 * 86400, "delay_days": 1, "title": "Riverdale",
                       "season": 1, "path": "/library/tv/Riverdale (2017)", "size_bytes": 4_408_213_504,
                       "score": 40.7}}})
fc = A.pending_delete_forecast({"OUTPUT_DIR": _OUT, "DELETE_DELAY_DAYS": 1, "REDLINE_ONLY_MODE": False})
check("two ripe marked seasons are the next deletion",
      fc["event_count"] == 2 and fc["event_seasons"] == 2 and fc["event_movies"] == 0
      and fc["event_bytes"] == 3_727_687_680 + 4_408_213_504, fc)
check("...while the queue and movie-marked counts stay movie-only",
      fc["count"] == 0 and fc["marked"] == 0, fc)

js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
check("the dashboard words the batch by type",
      "evSeasons ? `${evSeasons} season${evSeasons === 1 ? '' : 's'}` : ''" in js
      and "movies: d.marked_event_movies, seasons: d.marked_event_seasons" in js)
src = (ROOT / "engine.py").read_text(encoding="utf-8")
check("a dry run's closing line names the seasons its merge took",
      'f"Dry run — marked {_what} for deletion; nothing was deleted."' in src
      and "_take_n, _take_b = _season_takes_this_run()" in src)
check("a scheduled Cleanup's closing line counts the seasons it marks as it ends",
      "_tk = 0 if _manual_cleanup else _season_takes_this_run()[0]" in src)
app_src = (ROOT / "app.py").read_text(encoding="utf-8")
check("the run marks its merge when it ends — every run but a manual Cleanup",
      "if not (manual and _is_cleanup_mode(_effective_mode)):" in app_src
      and "_mark_engine_takes(_run_started_at, _tv_report)" in app_src)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
