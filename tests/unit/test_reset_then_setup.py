"""After a config RESET, setting up again and running Simulate must reach
Monitor Only — the same as a fresh install.

Reported as "a fresh install won't switch to Monitor Only after a Simulate",
and the reset is what made it reproducible. The two look identical to a user
and must behave identically, but they do not arrive the same way:

  fresh install  container starts on the shipped defaults, so startup code runs
                 against them
  RESET          /api/config/reset rewrites those same defaults while the app
                 KEEPS RUNNING — no startup code runs afterwards at all

So anything the wake depends on that is stamped at startup simply never exists
after a reset. That is why the state is stored as "the user deliberately chose
Paused" rather than "the scheduler is resting": the ABSENCE of a flag is what a
reset, a fresh install, and a hand-edited config all have in common, so absence
has to mean the wakeable state. Storing the resting state instead left every
one of them permanently stuck at Paused.
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
_OUT = tempfile.mkdtemp(prefix="mr-reset-setup.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


SHIPPED = json.loads((ROOT / "default_config.json").read_text())
SERVER_AND_PATH = {"USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:1",
                   "JELLYFIN_API_KEY": "k", "MONITOR_DIRS": ["/library/movies"]}

check("the shipped defaults start Paused",
      SHIPPED.get("RUN_MODE") == "off")
check("...and carry no deliberate-pause flag, so they are wakeable",
      not SHIPPED.get(A.RUN_MODE_USER_PAUSED_KEY))


# ── The reported journey, driven through the real save handler logic ─────────
# A reset leaves exactly the shipped defaults on disk; the user then saves
# connections, saves a monitored path, and runs Simulate.
_saves = []
_orig = (A.save_config, A._restart_schedule_clock, A._library_db_fresh)
A.save_config = lambda cfg, **k: (_saves.append(dict(cfg)), True)[1]
A._restart_schedule_clock = lambda: None

def save(cfg, saved, *, db_fresh, connected=True):
    """One trip through the config-save mode lifecycle."""
    A._library_db_fresh = lambda c=None: db_fresh
    out = dict(cfg)
    A._apply_setup_mode_lifecycle(out, dict(saved), {"critical_ok": connected})
    return out

after_reset = dict(SHIPPED)
# 1. Save the media server. No paths yet, nothing scanned.
step1 = save({**after_reset, **{"USE_JELLYFIN": True}}, after_reset, db_fresh=False)
check("saving connections after a reset stays Paused", step1["RUN_MODE"] == "off")
check("...and does not record a deliberate pause the user never made",
      not step1.get(A.RUN_MODE_USER_PAUSED_KEY))

# 2. Save the monitored path. Still nothing scanned.
step2 = save({**step1, **SERVER_AND_PATH}, step1, db_fresh=False)
check("saving the monitored path stays Paused", step2["RUN_MODE"] == "off")
check("...still with no deliberate pause recorded",
      not step2.get(A.RUN_MODE_USER_PAUSED_KEY))

# 3. A Simulate completes BEFORE any space threshold is armed. The shipped
#    defaults arm nothing (headroom off, no redline, no cap), and Monitor Only
#    with nothing to monitor is a lie — so the wake must refuse until a
#    trigger exists, however fresh the scan is.
A._library_db_fresh = lambda c=None: True
A.load_config = lambda: dict(step2)
_saves.clear()
A._sync_scheduler_mode_with_db()
check("a completed Simulate with NO armed threshold does not wake Monitor Only",
      _saves == [])

# 4. Arm a threshold (the new onboarding step), then the Simulate's wake fires.
step3 = save({**step2, "HEADROOM_GB": 500, "REDLINE_ONLY_MODE": False},
             step2, db_fresh=False)
check("arming a Headroom target stays Paused until a scan proves the setup",
      step3["RUN_MODE"] == "off")
A._library_db_fresh = lambda c=None: True   # the Simulate's scan just landed
A.load_config = lambda: dict(step3)
_saves.clear()
A._sync_scheduler_mode_with_db()
check("a completed Simulate after a RESET reaches Monitor Only",
      len(_saves) == 1 and _saves[-1]["RUN_MODE"] == "paused")


# ── The requirement this must not break ─────────────────────────────────────
# A pause the user actually chose has to survive the same completed Simulate.
paused_by_user = save({**SERVER_AND_PATH, "RUN_MODE": "off", "HEADROOM_GB": 500},
                      {**SERVER_AND_PATH, "RUN_MODE": "paused", "HEADROOM_GB": 500},
                      db_fresh=False)
check("choosing Paused from Monitor Only records it as deliberate",
      paused_by_user.get(A.RUN_MODE_USER_PAUSED_KEY) is True)
A.load_config = lambda: dict(paused_by_user)
_saves.clear()
A._sync_scheduler_mode_with_db()
check("...and a later completed Simulate leaves it alone", _saves == [])

A.save_config, A._restart_schedule_clock, A._library_db_fresh = _orig

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
