"""The Marked & Eligible button and the window it opens report one number.

Reported: the button read 2,213 and opening it showed 2,254. The gap was 41
TV seasons — present in the window's list, absent from the button's count,
because the two asked different questions:

  • the count required _tv_cleanup_armed — the TV switch AND a daily pool
    trigger (a Headroom target or a Library Size Cap), since Redline is a
    movie-only emergency;
  • the list required only the switch.

In redline-only mode those part company by exactly the season count, and the
window was the one that was wrong: with no daily trigger nothing in it could
ever be deleted, so it was listing seasons under "Deletions" that no run
would take. One gate now, in _tv_eligible_entries, with the count reporting
what the list returns.

The modes below are the ones that separate the two rules, so a gate that is
simply always shut fails here just as loudly as one that is always open.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-pool.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


NOW, DAY, GB = 1_700_000_000, 86400, 1_000_000_000
SEASONS = 41
MOVIES = 2213

# Ended shows with a watched season each: eligible whenever the gate allows.
TV_ROWS = [{"media_type": "tv", "title": f"Show {i}", "tv_status": "ended",
            "tv_in_scope": 1, "protected": False, "favorite": False, "users": 1,
            "last_played": NOW - 300 * DAY, "added_at": NOW - 900 * DAY,
            "rating": 6.0, "votes": 5000, "tv_episodes": 10, "tv_episodes_watched": 10,
            "tv_seasons": [{"n": 1, "eps": 10, "eps_watched": 10,
                            "size_bytes": (3 + i) * GB, "last_played": NOW - 300 * DAY}]}
           for i in range(SEASONS)]
A._read_library_snapshot = lambda: ({"movies": list(TV_ROWS)}, None)
A.disk_stats = lambda: {"used_gb": 36829.0, "total_gb": 53994.0,
                        "free_gb": 17165.0, "pct_used": 68.2}
A.library_stats = lambda: {"library_gb": 23489.0}
HEALTHY = {"critical_ok": True, "required_tooltip": "", "jellyfin_connected": True,
           "errors": [], "warnings": [], "blocked": False}
A._refresh_connection_health_cache = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_state = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_for_ui = lambda cfg=None: dict(HEALTHY)

# The stored movie queue a redline-only Simulate leaves behind.
QUEUE = {f"/library/Movies HD/m{i}.mkv": {"title": f"Movie {i}", "size_bytes": 2 * GB,
         "marked_at": None, "score": 10.0 + i * 0.01} for i in range(MOVIES)}
A._pending_raw = lambda: dict(QUEUE)

BASE = {"OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/Movies HD", "/library/TV Shows"],
        "RUN_MODE": "off", "REDLINE_GB": 500, "REDLINE_ONLY_MODE": True,
        "HEADROOM_GB": 0, "MAX_LIBRARY_GB": None,
        "SCORE_BALANCE": 70, "GRACE_PERIOD_DAYS": 30, "MAX_IMDB_RATING": 7.5,
        "MAX_STALENESS_MONTHS": 36, "TV_SERIES_WATCH_BUMP": 10, "TV_WATCH_WEIGHT": 100,
        "TV_SEASON_ELIGIBILITY": "oldest", "MOVIE_CLEANUP_ENABLED": True,
        # A media server that can supply the TV inventory.
        "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k"}

A.app.config["TESTING"] = True
client = A.app.test_client()


def numbers(**over):
    A.CONFIG_PATH.write_text(json.dumps({**BASE, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    cfg = A.load_config()
    return (client.get("/api/status").get_json()["marked_count"],   # the button
            len(A._pool_marked_entries(cfg)))                        # the window


# ── Redline-only: seasons are not deletable, so neither surface offers them ──
# Redline is a movie-only emergency and there is no daily pool trigger, so a
# season listed here would be a deletion that never comes.
button, window = numbers(TV_CLEANUP_ENABLED=True)
check("redline-only: the button and the window agree", button == window, f"{button} vs {window}")
check("...and neither counts seasons that can never be deleted",
      button == MOVIES, button)

# ── A daily pool trigger makes them real, on BOTH surfaces ─────────────────
# Or the check above would be satisfied by a gate that is simply always shut.
for label, over in (("a Headroom target", {"HEADROOM_GB": 1000, "REDLINE_ONLY_MODE": False}),
                    ("a Library Size Cap", {"MAX_LIBRARY_GB": 20000})):
    button, window = numbers(TV_CLEANUP_ENABLED=True, **over)
    check(f"{label} puts the seasons in — on both surfaces",
          button == window == MOVIES + SEASONS, f"{button} vs {window}")

# ── The switch still has to be on ──────────────────────────────────────────
for label, over in (("redline-only", {}),
                    ("Headroom armed", {"HEADROOM_GB": 1000, "REDLINE_ONLY_MODE": False}),
                    ("Library Cap armed", {"MAX_LIBRARY_GB": 20000})):
    button, window = numbers(TV_CLEANUP_ENABLED=False, **over)
    check(f"TV cleanup off: no seasons anywhere ({label})",
          button == window == MOVIES, f"{button} vs {window}")

# ── No TV inventory configured: also out of scope ──────────────────────────
button, window = numbers(TV_CLEANUP_ENABLED=True, HEADROOM_GB=1000, REDLINE_ONLY_MODE=False,
                         USE_JELLYFIN=False, JELLYFIN_URL="", JELLYFIN_API_KEY="")
check("with no media server to supply the inventory, both agree on none",
      button == window == MOVIES, f"{button} vs {window}")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
