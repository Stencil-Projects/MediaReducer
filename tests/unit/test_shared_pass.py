"""shared.py — the pass mechanics both executors import so the movie pass
(engine) and the TV season pass (app) cannot drift: the pool deficit
arithmetic, the delay clock, the deleted.log line, and the shared eligibility
rungs.

Two layers pinned here. The helpers' own contracts (boundaries, junk inputs,
fail directions). And the WIRING: monkeypatching one shared helper moves BOTH
executors' answers — the live proof each side actually delegates instead of
keeping a private copy that could drift.

Hermetic: temp store, stubbed disk/library measurements.
"""
import atexit
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-shared.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A      # noqa: E402
import engine as E   # noqa: E402
import shared as S   # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

GB = 1_000_000_000

# ── The pool deficit ────────────────────────────────────────────────────────
check("headroom overage alone", S.pool_deficit_gb(600, 550, None, None) == 50.0)
check("cap overage alone", S.pool_deficit_gb(600, None, 700, 500) == 200.0)
check("the LARGER overage wins, never the sum",
      S.pool_deficit_gb(600, 550, 700, 500) == 200.0)
check("no thresholds means no deficit", S.pool_deficit_gb(600, None, 700, None) == 0.0)
check("an unmeasured library never trips the cap",
      S.pool_deficit_gb(600, None, 0, 500) == 0.0 and S.pool_deficit_gb(600, None, None, 500) == 0.0)
check("junk numbers read as absent, not as zero-limit",
      S.pool_deficit_gb("x", "y", "z", "w") == 0.0)

# ── The delay clock ─────────────────────────────────────────────────────────
# Calendar days, not 24h blocks: 23:59 and 00:01 marks age the same.
_jan1_late = time.mktime((2026, 1, 1, 23, 59, 0, 0, 0, -1))
_jan2_early = time.mktime((2026, 1, 2, 0, 1, 0, 0, 0, -1))
check("whole calendar days", S.delete_on_date(_jan1_late, 1) == datetime.date(2026, 1, 2)
      and S.delete_on_date(_jan2_early, 1) == datetime.date(2026, 1, 3))
check("the delay never drops below one day",
      S.delete_on_date(_jan1_late, 0) == datetime.date(2026, 1, 2)
      and S.delete_on_date(_jan1_late, -3) == datetime.date(2026, 1, 2))
check("an unusable epoch is None / empty, never a crash",
      S.delete_on_date(None, 7) is None and S.delete_on_date(1e18, 7) is None
      and S.delete_on_str("junk", 7) == "")
check("the string form is the ISO date both sides compare against today",
      S.delete_on_str(_jan1_late, 7) == "2026-01-08")

# Both executors run on THIS clock. The engine's mark date is the shared
# string verbatim; a TV mark's due date is the same call with the same stamp.
check("engine _mark_delete_on IS the shared clock",
      E._mark_delete_on(_jan1_late, 7) == S.delete_on_str(_jan1_late, 7))

# ── The deleted.log line ────────────────────────────────────────────────────
# One builder, one parser: a movie-shaped and a TV-shaped line must both
# round-trip through the app's history regex with every field intact.
movie_line = S.deleted_log_line("Movie A", "/lib/movies/A/A.mkv", 5 * GB,
                                score=7.51, plays=3, last_played_text="2024-03-01 20:15:33")
tv_line = S.deleted_log_line("Show B", "/lib/tv/B/Season 01/e1.mkv", 2 * GB,
                             score=1.234, media="tv", season=1)
m1, m2 = A._DELETED_LOG_RE.match(movie_line.strip()), A._DELETED_LOG_RE.match(tv_line.strip())
check("a movie-shaped line parses with the why intact",
      m1 and m1.group("size_bytes") == str(5 * GB) and m1.group("score") == "7.5"
      and m1.group("plays") == "3" and m1.group("media") is None, movie_line)
check("a TV-shaped line parses with the type fields intact",
      m2 and m2.group("media") == "tv" and m2.group("season") == "1"
      and m2.group("size_bytes") == str(2 * GB), tv_line)
check("junk rationale still lands the deletion record",
      A._DELETED_LOG_RE.match(S.deleted_log_line("T", "/p", "junk", plays="junk").strip()))

# ── The shared eligibility rungs ────────────────────────────────────────────
check("IMDb in use + no rating = not enough data to judge",
      S.imdb_rung(None, None, in_use=True, require_votes=False, cutoff=None) == "no_imdb_data")
check("movies also need votes; series (dataset ratings) do not",
      S.imdb_rung(6.0, 0, in_use=True, require_votes=True, cutoff=None) == "no_imdb_data"
      and S.imdb_rung(6.0, 0, in_use=True, require_votes=False, cutoff=None) is None)
check("above the cutoff is shielded, at it is not",
      S.imdb_rung(8.1, 900, in_use=True, require_votes=True, cutoff=8.0) == "high_rated"
      and S.imdb_rung(8.0, 900, in_use=True, require_votes=True, cutoff=8.0) is None)
check("IMDb off means no data rung at all",
      S.imdb_rung(None, None, in_use=False, require_votes=True, cutoff=None) is None)

_now = time.time()
check("inside the grace period holds, outside releases",
      S.grace_rung(_now - 5 * 86400, 30, _now) is True
      and S.grace_rung(_now - 40 * 86400, 30, _now) is False)
check("no grace setting or no added date never holds",
      S.grace_rung(_now - 5 * 86400, 0, _now) is False
      and S.grace_rung(0, 30, _now) is False
      and S.grace_rung("junk", 30, _now) is False)

check("unplayed skip holds only unplayed items and only when on",
      S.unplayed_rung(0, True) is True and S.unplayed_rung(3, True) is False
      and S.unplayed_rung(0, False) is False)

# ── The wiring: one helper, both executors ──────────────────────────────────
# The same numbers through both sides' entry points must agree exactly:
# used 600 of 1000 GB with a 450 GB headroom target (limit 550 → 50 over),
# library 700 GB against a 500 GB cap (200 over) — deficit 200 GB.
A.disk_stats = lambda: {"used_gb": 600.0, "total_gb": 1000.0, "free_gb": 400.0}
A.library_stats = lambda: {"library_gb": 700.0}
CFG = {"HEADROOM_GB": 450, "MAX_LIBRARY_GB": 500}
E.MAX_LIBRARY_GB = 500
E._tv_share_bytes = lambda: 0
check("both executors size the same deficit from the same numbers",
      A._pool_deletion_target_bytes(CFG) == 200 * GB
      and E._daily_deficit_bytes(600.0, 550.0, 700.0) == 200 * GB,
      (A._pool_deletion_target_bytes(CFG), E._daily_deficit_bytes(600.0, 550.0, 700.0)))

# Break the shared helper and BOTH sides must move — the proof neither keeps a
# private copy of the arithmetic that could silently drift.
_real = S.pool_deficit_gb
try:
    S.pool_deficit_gb = lambda *a, **k: 123.0
    check("bending the one shared deficit bends the APP side",
          A._pool_deletion_target_bytes(CFG) == 123 * GB, A._pool_deletion_target_bytes(CFG))
    check("...and the ENGINE side, in the same breath",
          E._daily_deficit_bytes(600.0, 550.0, 700.0) == 123 * GB,
          E._daily_deficit_bytes(600.0, 550.0, 700.0))
finally:
    S.pool_deficit_gb = _real
check("restored", A._pool_deletion_target_bytes(CFG) == 200 * GB)

# Same drift-proof for the delay clock: both mark-date paths ride one function.
_real_str = S.delete_on_str
try:
    S.delete_on_str = lambda *a, **k: "9999-12-31"
    check("bending the one clock bends the engine's mark dates",
          E._mark_delete_on(_jan1_late, 7) == "9999-12-31")
finally:
    S.delete_on_str = _real_str

# And for the rungs: the engine's hard-filter ladder must reach its grace and
# unplayed decisions THROUGH shared, not through a private copy. (The TV
# ladder's delegation is pinned by test_tv_season_plan — its grace/unplayed
# checks run through these same helpers.)
E.MOVIE_CLEANUP_ENABLED = True
E.PROTECT_JELLYFIN_FAVORITES = False
E.MAX_IMDB_RATING = None
E.GRACE_PERIOD_DAYS = 30
E.SKIP_UNPLAYED_MOVIES = True
E.imdb_dataset_needed = lambda: False
def _movie(added_days_ago, plays):
    return E._hard_filter_reason(protected=False, favorite=False,
                                 imdb_rating=None, imdb_votes=None,
                                 added_at=_now - added_days_ago * 86400,
                                 play_count=plays, last_played=0, now=_now)
check("the engine ladder holds a fresh add and an unplayed movie",
      _movie(5, 3) == "recently_added" and _movie(40, 0) == "unplayed"
      and _movie(40, 3) is None)
_real_grace = S.grace_rung
try:
    S.grace_rung = lambda *a, **k: True
    check("bending the one grace rung bends the engine ladder",
          _movie(4000, 3) == "recently_added")
finally:
    S.grace_rung = _real_grace
check("rung restored", _movie(4000, 3) is None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
