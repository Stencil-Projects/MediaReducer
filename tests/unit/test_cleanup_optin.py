"""Deleting files is opt-in, and an armed Automatic Cleanup must have a type.

Two rules, one idea. This app deletes files, so a fresh install should scan,
score and show you everything while deleting nothing until you have said which
media types it may touch.

  • MOVIE_CLEANUP_ENABLED and TV_CLEANUP_ENABLED ship OFF, and a config that
    never mentions them reads as off too. Absent must not mean on: a config
    written before the keys existed would then be deleting a type nobody named.

  • With both off, Automatic Cleanup cannot arm. Nothing is ever eligible, so
    the mode would wake on schedule, scan, and delete nothing — a scheduler
    that reads as deleting while it cannot. The block rides ok_for_cleanup,
    which is the gate the mode radio, the save that arms it, the manual
    Cleanup endpoint and the scheduler's next-run check all already consult.

The engine keeps its own copy of the movie switch (it runs as a subprocess and
re-reads the config itself), so its default is checked against the app's rather
than assumed to match.
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
_OUT = tempfile.mkdtemp(prefix="mr-optin.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402
import engine as E  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


# ── Shipped off ─────────────────────────────────────────────────────────────
shipped = json.loads((ROOT / "default_config.json").read_text())
check("the shipped defaults have movie cleanup off",
      shipped["MOVIE_CLEANUP_ENABLED"] is False, shipped["MOVIE_CLEANUP_ENABLED"])
check("...and TV cleanup off", shipped["TV_CLEANUP_ENABLED"] is False,
      shipped["TV_CLEANUP_ENABLED"])

# A config that never names them. Read through the real loader, so this is the
# value every caller sees rather than one key's fallback.
cfg = A.load_config()
check("a config with neither key reads as movie cleanup off",
      cfg["MOVIE_CLEANUP_ENABLED"] is False, cfg["MOVIE_CLEANUP_ENABLED"])
check("...and TV cleanup off", cfg["TV_CLEANUP_ENABLED"] is False,
      cfg["TV_CLEANUP_ENABLED"])
# The engine re-reads the config in its own process; a default of True there
# would delete movies on a config the app considers opted out.
check("the engine's own default agrees with the app's",
      E.MOVIE_CLEANUP_ENABLED is False, E.MOVIE_CLEANUP_ENABLED)

# Explicitly on is still on — the switch has to be a switch.
on = dict(cfg, MOVIE_CLEANUP_ENABLED=True)
check("an explicit true still means on", bool(on["MOVIE_CLEANUP_ENABLED"]))


# ── Automatic Cleanup needs a type ──────────────────────────────────────────
# Everything else about this config is fine: a real headroom target, a
# monitored path, room under the safety cap. The ONLY thing in question is
# whether a type is allowed.
DISK = {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
BASE = {"RUN_MODE": "paused", "HEADROOM_GB": 400, "REDLINE_GB": 200,
        "REDLINE_ONLY_MODE": False, "MAX_LIBRARY_GB": None,
        "MAX_HEADROOM_PCT": 60, "MONITOR_DIRS": ["/library/movies"],
        "OUTPUT_DIR": _OUT}

def gate(**over):
    return A._space_threshold_state(dict(BASE, **over), DISK, library_gb=100.0)

neither = gate(MOVIE_CLEANUP_ENABLED=False, TV_CLEANUP_ENABLED=False)
check("both types off blocks Automatic Cleanup",
      neither["ok_for_cleanup"] is False, neither["cleanup_tooltip"])
check("...and says which page to fix it on",
      "Filtering & Scoring" in neither["cleanup_tooltip"], neither["cleanup_tooltip"])
# Simulate is a preview. It has to stay open, or there is no way to look at
# what cleanup WOULD do before allowing a type.
check("...while Simulate stays available", neither["ok_for_simulate"] is True, neither)

for label, over in (("movies on", {"MOVIE_CLEANUP_ENABLED": True, "TV_CLEANUP_ENABLED": False}),
                    ("TV on", {"MOVIE_CLEANUP_ENABLED": False, "TV_CLEANUP_ENABLED": True}),
                    ("both on", {"MOVIE_CLEANUP_ENABLED": True, "TV_CLEANUP_ENABLED": True})):
    st = gate(**over)
    check(f"{label}: Automatic Cleanup is allowed", st["ok_for_cleanup"] is True,
          st["cleanup_tooltip"])

# The block is ADDITIVE, not a replacement: a config that is also missing a
# space target still says so. Losing the older reason would send someone to the
# wrong page.
both_wrong = gate(MOVIE_CLEANUP_ENABLED=False, TV_CLEANUP_ENABLED=False,
                  HEADROOM_GB=0, REDLINE_GB=None, MAX_LIBRARY_GB=None)
check("a config missing BOTH still reports both reasons",
      "Filtering & Scoring" in both_wrong["cleanup_tooltip"]
      and "Headroom target" in both_wrong["cleanup_tooltip"],
      both_wrong["cleanup_tooltip"])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
