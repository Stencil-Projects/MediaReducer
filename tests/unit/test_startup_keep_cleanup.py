"""Automatic Cleanup surviving a restart, and the one thing that still stops it.

A restart demotes Automatic Cleanup to Monitor Only by default, because a
restart is usually an upgrade or a crash and the deletion plan waiting on disk
may no longer describe the library. "Set to Monitor Only at startup" lets that
be turned off — resume where you left off — and it is the only setting in this
app that decides whether deletions can begin without anyone pressing anything.

So the interesting case is not that it works, it is what it refuses to do: an
out-of-date library database demotes anyway, whatever the setting says, because
resuming deletions against a plan nothing has re-checked is precisely what the
demote exists to prevent. Off + stale must land in the same place as on.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-startup-keep.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
# Written BEFORE the import: app does startup work as it loads, and without
# OUTPUT_DIR here that work builds a store in /config, the real data directory.
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


A.library_stats = lambda: {"library_gb": 100.0}

BASE = {
    "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:8096", "JELLYFIN_API_KEY": "k",
    "RUN_MODE": "headroom", "HEADROOM_GB": 50, "MONITOR_DIRS": ["/library/movies"],
    "OUTPUT_DIR": _OUT,
}


def restart(*, keep, db_fresh, mode="headroom"):
    """Write a config and run the startup safety sequence, as boot does."""
    cfg = dict(BASE)
    cfg["RUN_MODE"] = mode
    if keep is not None:
        cfg["PAUSE_CLEANUP_ON_STARTUP"] = not keep
    A._library_db_fresh = lambda c=None: db_fresh
    json.dump(cfg, open(A.CONFIG_PATH, "w"))
    A._config_memo["key"] = None          # bust the load_config memo between cases
    A.force_paused_run_mode_on_startup()
    return json.load(open(A.CONFIG_PATH))


# ── The default: a restart demotes, and says why ─────────────────────────────
out = restart(keep=False, db_fresh=True)
check("on by default, a restart drops Automatic Cleanup to Monitor Only",
      out.get("RUN_MODE") == "paused", out.get("RUN_MODE"))
check("...and records the restart as the reason",
      "after every restart" in (out.get("_RUN_MODE_AUTOPAUSE_REASON") or ""),
      out.get("_RUN_MODE_AUTOPAUSE_REASON"))

# A config that predates the setting must behave like the default, not like
# someone having turned the safety off.
out = restart(keep=None, db_fresh=True)
check("a config without the key still demotes", out.get("RUN_MODE") == "paused",
      out.get("RUN_MODE"))

# ── Turned off, with a database that is still good: stay armed ───────────────
out = restart(keep=True, db_fresh=True)
check("turned off with a current database, Automatic Cleanup survives the restart",
      out.get("RUN_MODE") == "headroom", out.get("RUN_MODE"))
check("...and nothing claims it was paused", not out.get("_RUN_MODE_AUTOPAUSE_REASON"),
      out.get("_RUN_MODE_AUTOPAUSE_REASON"))

# ── Turned off, database out of date: demote anyway ──────────────────────────
# The whole point of the demote. A stale plan is the case where resuming would
# delete against something nothing has re-checked.
out = restart(keep=True, db_fresh=False)
check("an out-of-date database demotes even with the setting off",
      out.get("RUN_MODE") != "headroom", out.get("RUN_MODE"))
check("...and says that is why, not that the restart did it",
      "out of date" in (out.get("_RUN_MODE_AUTOPAUSE_REASON") or ""),
      out.get("_RUN_MODE_AUTOPAUSE_REASON"))

# ── The setting only ever concerns Automatic Cleanup ─────────────────────────
out = restart(keep=True, db_fresh=True, mode="paused")
check("Monitor Only is unaffected by it", out.get("RUN_MODE") == "paused", out.get("RUN_MODE"))
out = restart(keep=True, db_fresh=True, mode="off")
check("a fully-paused scheduler is not promoted by it", out.get("RUN_MODE") == "off",
      out.get("RUN_MODE"))

# ── The stored value is a real boolean, and absent reads as ON ───────────────
A._config_memo["key"] = None
json.dump({**BASE, "PAUSE_CLEANUP_ON_STARTUP": "false"}, open(A.CONFIG_PATH, "w"))
A._config_memo["key"] = None
check("a string value is coerced to a bool",
      A.load_config().get("PAUSE_CLEANUP_ON_STARTUP") is False,
      A.load_config().get("PAUSE_CLEANUP_ON_STARTUP"))
json.dump({k: v for k, v in BASE.items()}, open(A.CONFIG_PATH, "w"))
A._config_memo["key"] = None
# Two things guarantee this and each covers the other, so neither is redundant:
# default_config.json carries the key, and _coerce_bool defaults it on when even
# that is missing. Removing either one alone leaves this passing; removing both
# would let a config file with no key read as the safety turned off.
check("absent reads as ON, never as someone having turned the safety off",
      A.load_config().get("PAUSE_CLEANUP_ON_STARTUP") is True,
      A.load_config().get("PAUSE_CLEANUP_ON_STARTUP"))
check("a non-boolean is rejected by the file validator",
      "PAUSE_CLEANUP_ON_STARTUP" in [i["key"] for i in
                                     A._config_file_issues({**BASE, "PAUSE_CLEANUP_ON_STARTUP": "yes"})])

# ── It ships on ──────────────────────────────────────────────────────────────
_defaults = json.loads((Path(__file__).resolve().parents[2] / "default_config.json").read_text())
check("a fresh install ships with the safety demote on",
      _defaults.get("PAUSE_CLEANUP_ON_STARTUP") is True,
      _defaults.get("PAUSE_CLEANUP_ON_STARTUP"))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
