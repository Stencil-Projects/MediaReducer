"""The scheduler-mode round trip through the REAL save handler.

The mode lifecycle promises two things a user relies on without reading code:

  1. removing a piece of the working set — the last armed space threshold, the
     last selected server — rests the scheduler at Paused on that same save;
  2. putting the piece back wakes it into Monitor Only again, automatically,
     with no extra ceremony (the library scan is still fresh, so there is
     nothing new to prove).

And one promise pointing the other way: a Paused the user CHOSE — selecting the
radio while Monitor Only was available — is deliberate, and no amount of
"criteria met again" may wake it.

test_daily_maintenance_sim covers the lifecycle function in isolation; this
drives /api/config end to end, because that is where the pieces meet: the
save-side flag derivation, the forced-off cascade, the auto-enable, and the
deliberate-pause flag are all decided in one request, and a regression in their
ORDER only shows up here.

Connection health is stubbed healthy INCLUDING the per-service flags: the save
handler auto-deselects a selected server whose probe failed, so a stub that
says only critical_ok=True quietly unticks Jellyfin mid-save and every wake
assertion fails for the wrong reason. (Found the hard way.)
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-roundtrip.")
atexit.register(shutil.rmtree, _OUT, True)
_LIB = tempfile.mkdtemp(prefix="mr-roundtrip-lib.")
atexit.register(shutil.rmtree, _LIB, True)
(Path(_LIB) / "movies").mkdir(parents=True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ["MEDIAREDUCER_LIBRARY"] = _LIB
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


_state = {"cfg": {
    "RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
    "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:1", "JELLYFIN_API_KEY": "k",
    "HEADROOM_GB": 500, "REDLINE_GB": None, "MAX_LIBRARY_GB": None,
    "REDLINE_ONLY_MODE": False, "MAX_HEADROOM_PCT": 15, "OUTPUT_DIR": _OUT,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
}}
A.load_config = lambda: dict(_state["cfg"])
A.save_config = lambda cfg, **k: _state.update(cfg=dict(cfg)) or True
A.run_summary = lambda *a, **k: (False, "skip")
A._invalid_config_response = lambda: None
_HEALTH = {"critical_ok": True, "probed": True, "errors": [], "warnings": [],
           "severity": "", "required_tooltip": "",
           "jellyfin_connected": True, "tautulli_connected": False,
           "radarr_connected": False}
A._health_for_config_save = lambda cfg: dict(_HEALTH)
A._refresh_connection_health_cache = lambda cfg, probe=True: dict(_HEALTH)
A._library_db_fresh = lambda c=None: True   # the scan stays fresh throughout
A._full_scan_overdue = lambda: False
A._pending_plan_current = lambda *a, **k: True
A._deletion_limits_exceeded = lambda *a, **k: False
A._simulate_evidence = lambda cfg: True
A._pending_raw = lambda: {}
_client = A.app.test_client()


def post(**over):
    payload = dict(_state["cfg"])
    payload.pop("OUTPUT_DIR", None)
    payload.update({"TAUTULLI_URL": "", "TAUTULLI_API_KEY": "", "PLEX_URL": "",
                    "PLEX_TOKEN": "", "RADARR_URL": "", "RADARR_API_KEY": "",
                    "RADARR_OVERSEERR_SECTION_ID": None, "USE_PLEX": False})
    payload.update(over)
    r = _client.post("/api/config", json=payload, headers={"X-MediaReducer": "1"})
    return r.status_code, (r.get_json() or {})


# ── Leg 1: disarm the last space trigger while in Monitor Only ───────────────
code, body = post(RUN_MODE="paused", HEADROOM_GB=0, REDLINE_ONLY_MODE=True)
check("disarming the last trigger rests at Paused on that save",
      code == 200 and _state["cfg"].get("RUN_MODE") == "off"
      and body.get("run_mode_forced_off") is True)
check("...as a SYSTEM pause, so it stays wakeable",
      not _state["cfg"].get(A.RUN_MODE_USER_PAUSED_KEY))

# ── Leg 2: re-arm it — the wake needs no other ceremony ─────────────────────
code, body = post(RUN_MODE="off", HEADROOM_GB=500, REDLINE_ONLY_MODE=False)
check("re-arming the trigger wakes Monitor Only on the save itself",
      code == 200 and _state["cfg"].get("RUN_MODE") == "paused"
      and body.get("run_mode_auto_enabled") is True)

# ── Leg 3: the same trip through the server selection ────────────────────────
code, body = post(RUN_MODE="paused", USE_JELLYFIN=False)
check("deselecting the last server rests at Paused on that save",
      code == 200 and _state["cfg"].get("RUN_MODE") == "off"
      and body.get("run_mode_forced_off") is True)
code, body = post(RUN_MODE="off", USE_JELLYFIN=True)
check("re-selecting it wakes Monitor Only again",
      code == 200 and _state["cfg"].get("RUN_MODE") == "paused"
      and body.get("run_mode_auto_enabled") is True)

# ── Leg 4: a Paused the user CHOSE is deliberate and never auto-wakes ────────
code, body = post(RUN_MODE="off")
check("choosing Paused from Monitor Only records the deliberate flag",
      code == 200 and _state["cfg"].get("RUN_MODE") == "off"
      and _state["cfg"].get(A.RUN_MODE_USER_PAUSED_KEY) is True)
code, body = post(RUN_MODE="off", HEADROOM_GB=800)
check("...and later saves that keep every criterion met do NOT wake it",
      code == 200 and _state["cfg"].get("RUN_MODE") == "off"
      and not body.get("run_mode_auto_enabled"))
code, body = post(RUN_MODE="paused")
check("only picking Monitor Only again clears the deliberate flag",
      code == 200 and _state["cfg"].get("RUN_MODE") == "paused"
      and not _state["cfg"].get(A.RUN_MODE_USER_PAUSED_KEY))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
