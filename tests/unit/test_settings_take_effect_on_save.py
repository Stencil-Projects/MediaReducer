"""Saving a Filtering & Scoring change is enough — the Dashboard's marked and
eligible figures follow it with no run in between.

That promise is kept two different ways, and neither is obvious from the code:

  • MOVIES are a persisted queue. Nothing recomputes it on read, so the save
    launches a reconcile that rebuilds it in place from the stored snapshot
    (_RECONCILE_PURE_KEYS → run_reconcile).
  • SEASONS are not persisted at all. _tv_eligible_entries derives them from
    the snapshot against the CURRENT config every time they are read, so the
    next status poll is already right and a reconcile would be redundant —
    which is why no TV key appears in _RECONCILE_PURE_KEYS.

That absence reads as an oversight, and the obvious "fix" — adding the TV keys
— would spawn a reconcile that filters TV rows straight back out
(reconcile_from_snapshot is movie-only by design) and rebuild the movie queue
for nothing. So both halves of the promise are pinned here, together with the
mechanism each one uses, rather than left to be rediscovered.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-takeeffect.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


NOW = 1_700_000_000
DAY = 86400
GB = 1_000_000_000

# Two ended shows, each with an old fully-watched season and a newer untouched
# one. Season eligibility is the setting under test, so the shape has to make
# "oldest only" and "all seasons" give different answers.
TV_ROWS = [
    {"media_type": "tv", "title": "Dead One", "tv_status": "ended", "tv_in_scope": 1,
     "protected": False, "favorite": False, "users": 1, "last_played": NOW - 300 * DAY,
     "added_at": NOW - 900 * DAY, "rating": None, "votes": 0,
     "tv_episodes": 20, "tv_episodes_watched": 10,
     "tv_seasons": [{"n": 1, "eps": 10, "eps_watched": 10, "size_bytes": 10 * GB,
                     "last_played": NOW - 300 * DAY},
                    {"n": 2, "eps": 10, "eps_watched": 0, "size_bytes": 12 * GB,
                     "last_played": 0}]},
    {"media_type": "tv", "title": "Dead Two", "tv_status": "ended", "tv_in_scope": 1,
     "protected": False, "favorite": False, "users": 1, "last_played": NOW - 400 * DAY,
     "added_at": NOW - 900 * DAY, "rating": None, "votes": 0,
     "tv_episodes": 20, "tv_episodes_watched": 10,
     "tv_seasons": [{"n": 1, "eps": 10, "eps_watched": 10, "size_bytes": 8 * GB,
                     "last_played": NOW - 400 * DAY},
                    {"n": 2, "eps": 10, "eps_watched": 0, "size_bytes": 9 * GB,
                     "last_played": 0}]},
]

BASE = {"OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/tv"],
        "SCORE_BALANCE": 0, "GRACE_PERIOD_DAYS": 0, "MAX_IMDB_RATING": None,
        "MAX_STALENESS_MONTHS": 36, "TV_SERIES_WATCH_BUMP": 10,
        "TV_WATCH_WEIGHT": 100, "SKIP_UNPLAYED_MOVIES": False,
        "PROTECT_JELLYFIN_FAVORITES": False,
        "TV_CLEANUP_ENABLED": True, "TV_SEASON_ELIGIBILITY": "oldest",
        # Seasons are only eligible with a DAILY pool trigger (Redline is a
        # movie-only emergency) and a media server to supply the inventory —
        # see _tv_cleanup_armed. Without both, every list below is empty and
        # this file would be testing nothing.
        "HEADROOM_GB": 1000, "REDLINE_ONLY_MODE": False,
        "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test",
        "JELLYFIN_API_KEY": "k"}

# The snapshot the season plan is derived FROM — stubbed, because this is about
# what a save does to the reported figures, not about scanning.
A._read_library_snapshot = lambda: ({"movies": list(TV_ROWS)}, None)


def save(cfg):
    A.CONFIG_PATH.write_text(json.dumps(cfg))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    return A.load_config()


def eligible_seasons(cfg):
    """What the Dashboard counts as eligible TV, read the way it reads it."""
    return [(e["title"], e["season"]) for e in A._tv_eligible_entries(cfg, set())]


# ── Seasons: the save alone moves the figures, with nothing run ───────────
cfg = save(dict(BASE))
oldest_only = eligible_seasons(cfg)
check("oldest-only offers one season per show",
      len(oldest_only) == 2 and all(s == 1 for _t, s in oldest_only), oldest_only)

cfg = save({**BASE, "TV_SEASON_ELIGIBILITY": "all"})
all_seasons = eligible_seasons(cfg)
check("saving 'all seasons' immediately offers every season — no run, no reconcile",
      len(all_seasons) == 4, all_seasons)

cfg = save({**BASE, "TV_CLEANUP_ENABLED": False})
check("turning TV cleanup off empties the eligible list on the next read",
      eligible_seasons(cfg) == [], eligible_seasons(cfg))

# The scoring knobs reach it the same way — the SCORES are derived on read too,
# not read back off a stored queue. (Scores rather than order: whether a given
# fixture's order flips is a property of the fixture, but the scores moving is
# the thing that proves the setting was applied.)
def scores(cfg):
    return [e["score"] for e in A._tv_eligible_entries(cfg, set())]


# The slider's range is 100..200 (one full season-watch worth one movie watch,
# up to two); 100 IS the floor, so a change has to move off it to mean anything.
cfg = save({**BASE, "TV_SEASON_ELIGIBILITY": "all", "TV_WATCH_WEIGHT": 100})
no_weight = scores(cfg)
cfg = save({**BASE, "TV_SEASON_ELIGIBILITY": "all", "TV_WATCH_WEIGHT": 200})
full_weight = scores(cfg)
check("a watch-weight change re-scores the eligible seasons on read",
      no_weight != full_weight, f"{no_weight} vs {full_weight}")

# ── ...which is exactly why no TV key asks for a reconcile ────────────────
launched = []
_real_run = A.run_reconcile
A.run_reconcile = lambda **kw: launched.append(kw)
try:
    old = dict(BASE)
    for key, new in (("TV_SEASON_ELIGIBILITY", "all"), ("TV_CLEANUP_ENABLED", False),
                     ("TV_WATCH_WEIGHT", 0), ("TV_SERIES_WATCH_BUMP", 0),
                     ("TV_MAX_SEASON_EPISODES", 5)):
        launched.clear()
        r = A._reconcile_after_save(old, {**old, key: new}, source="test")
        check(f"{key} needs no reconcile — its side is derived on read",
              r == "none" and not launched, f"{r} {launched}")

    # The movie half has no such luxury: its queue is stored, so the save has
    # to rebuild it. Both mechanisms, one promise.
    launched.clear()
    r = A._reconcile_after_save(old, {**old, "SCORE_BALANCE": 90}, source="test")
    check("a MOVIE scoring change does launch the rebuild its stored queue needs",
          r == "started" and launched and launched[0]["refetch"] is False,
          f"{r} {launched}")
finally:
    A.run_reconcile = _real_run

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
