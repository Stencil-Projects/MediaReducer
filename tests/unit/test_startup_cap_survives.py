"""A Library Size Cap stays ARMED across a restart, and a cap-only (Headroom off)
setup neither loses its cap nor ends up in the invalid, app-locking state that a
former startup safety produced. Nothing prunes on restart anyway: startup forces
RUN_MODE to paused, so cleanup only runs once the user re-enables it."""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-cap-survive.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
# Written BEFORE the import: app does startup work as it loads, and without
# OUTPUT_DIR here that work builds a store in /config, the real data directory.
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# A large library (bigger than any cap here) — the old safety would have disabled
# the cap on this basis; it must not anymore.
A.library_stats = lambda: {"library_gb": 1000.0}
# Monitor Only also requires an up-to-date library database at startup; these
# cases test the mode transitions themselves, so treat the database as fresh
# (the stale-database rest state gets its own cases at the end).
A._library_db_fresh = lambda cfg=None: True

BASE = {
    "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:8096", "JELLYFIN_API_KEY": "k",
    "RUN_MODE": "headroom", "HEADROOM_GB": 0, "MONITOR_DIRS": ["/library/movies"],
    "OUTPUT_DIR": _OUT,
    "IMDB_RATINGS_URL": "https://datasets.imdbws.com/title.ratings.tsv.gz",
}


def restart(extra):
    """Write a config, then run the startup safety sequence (as boot does)."""
    cfg = dict(BASE); cfg.update(extra)
    json.dump(cfg, open(A.CONFIG_PATH, "w"))
    A._config_memo["key"] = None  # bust the load_config memo between cases
    A.force_paused_run_mode_on_startup()
    out = json.load(open(A.CONFIG_PATH))
    return out, [i["key"] for i in A._config_file_issues(out)]


# The dead safety must be gone, not merely unused.
check("cap-disable startup safety removed", not hasattr(A, "disable_undersized_library_cap_on_startup"))

# 1. Cap-only, Headroom off, cap below the library (the reported case): the cap
#    stays armed and the config stays valid (no lockout). RUN_MODE is paused.
out, issues = restart({"REDLINE_ONLY_MODE": True, "REDLINE_GB": None, "MAX_LIBRARY_GB": 995.0})
check("cap-only: cap stays armed across restart", out.get("MAX_LIBRARY_GB") == 995.0)
check("cap-only: REDLINE_ONLY_MODE preserved", out.get("REDLINE_ONLY_MODE") is True)
check("cap-only: config valid (no lockout)", issues == [])
check("cap-only: RUN_MODE paused on restart", out.get("RUN_MODE") == "paused")

# 2. Cap + Headroom target: both survive, RUN_MODE paused, valid.
out, issues = restart({"REDLINE_ONLY_MODE": False, "HEADROOM_GB": 500.0, "MAX_LIBRARY_GB": 995.0})
check("cap+headroom: cap survives", out.get("MAX_LIBRARY_GB") == 995.0 and issues == [])

# 3. A fully-paused scheduler ("off") is already the safest state — the startup
#    safety keeps it off instead of promoting it to Monitor Only, and it never
#    plants an autopause reason (the user chose this mode).
out, issues = restart({"RUN_MODE": "off", "HEADROOM_GB": 500.0, "REDLINE_ONLY_MODE": False})
check("off: stays fully paused across restart", out.get("RUN_MODE") == "off")
check("off: no autopause reason planted", not out.get("_RUN_MODE_AUTOPAUSE_REASON"))
check("off: config valid (no lockout)", issues == [])

# 4. Monitor Only with a working setup survives restarts as saved (only
#    Automatic Cleanup is demoted).
out, issues = restart({"RUN_MODE": "paused", "HEADROOM_GB": 500.0, "REDLINE_ONLY_MODE": False})
check("Monitor Only survives a restart as saved", out.get("RUN_MODE") == "paused")

# 5. Incomplete setup (missing a monitored path OR a selected media server):
#    the mode rests at fully-Paused until setup completes — a saved Monitor
#    Only is meaningless when the scheduler can't do anything useful.
out, issues = restart({"USE_JELLYFIN": False, "USE_PLEX": False, "MONITOR_DIRS": [],
                       "RUN_MODE": "paused", "HEADROOM_GB": 500.0,
                       "REDLINE_ONLY_MODE": False})
check("pristine install rests at fully-Paused", out.get("RUN_MODE") == "off")
check("pristine: no autopause reason planted", not out.get("_RUN_MODE_AUTOPAUSE_REASON"))
check("pristine: config valid (no lockout)", issues == [])
out, _ = restart({"MONITOR_DIRS": [], "RUN_MODE": "paused", "HEADROOM_GB": 500.0,
                  "REDLINE_ONLY_MODE": False})
check("server selected but no paths: rests at fully-Paused", out.get("RUN_MODE") == "off")
out, _ = restart({"USE_JELLYFIN": False, "USE_PLEX": False, "RUN_MODE": "paused",
                  "HEADROOM_GB": 500.0, "REDLINE_ONLY_MODE": False})
check("paths but no server selected: rests at fully-Paused", out.get("RUN_MODE") == "off")

# 6. Full setup but the library database is missing or stale (never scanned,
#    wiped by an update's store rebuild, or a long-stopped container): the mode
#    rests at fully-Paused, and stays WAKEABLE — the next completed Simulate
#    re-enables Monitor Only on its own. The system parked it, not the user.
A._library_db_fresh = lambda cfg=None: False
out, _ = restart({"RUN_MODE": "paused", "HEADROOM_GB": 500.0, "REDLINE_ONLY_MODE": False})
check("stale database rests Monitor Only at fully-Paused", out.get("RUN_MODE") == "off")
check("stale database leaves it wakeable (no deliberate-pause flag)",
      not out.get(A.RUN_MODE_USER_PAUSED_KEY))
out, _ = restart({"RUN_MODE": "headroom", "HEADROOM_GB": 500.0, "REDLINE_ONLY_MODE": False})
check("Automatic Cleanup + stale database also rests at fully-Paused",
      out.get("RUN_MODE") == "off")
A._library_db_fresh = lambda cfg=None: True

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
