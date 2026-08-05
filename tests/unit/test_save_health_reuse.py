"""A Config save probes the media servers only when the answer could have moved.

Connections can fail without any field changing, but a save is the wrong place
to catch that, and probing on every save protects nothing:

  • /api/run re-probes and REFUSES TO START when a required server is down;
  • the scheduler tick re-probes before any automatic cleanup;
  • the engine fails closed on any API error mid-run.

Saving a notification toggle or a scoring curve cannot make a media server
unreachable, so a probe there is pure latency on the Save button — and the
Config page that displays the result reuses this same cache without probing.

What must still hold: anything that could genuinely change the verdict re-probes.
That means every connection-relevant edit, and — importantly — a save made while
the last probe FAILED, since the user is most likely saving because they are
fixing it and recovery has to be noticed without waiting for a tick.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-save-health.")
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


CFG = {"USE_JELLYFIN": True, "JELLYFIN_URL": "http://jelly:8096", "JELLYFIN_API_KEY": "j",
       "OUTPUT_DIR": _OUT, "NOTIFY_ON_ERROR": True, "HEADROOM_GB": 5}

probes = {"n": 0}
HEALTHY = {"n": True}

def fake_state(cfg=None, *, probe=False):
    if probe:
        probes["n"] += 1
    return {"ok": HEALTHY["n"], "critical_ok": HEALTHY["n"], "severity": "ok",
            "summary": "stub", "errors": [] if HEALTHY["n"] else ["Jellyfin did not connect."],
            "warnings": [], "highlights": [], "mount_highlights": [],
            "has_visible_issues": not HEALTHY["n"], "probed": probe,
            "appdata": {}, "required_tooltip": "", "invalid_config": []}

A._connection_health_state = fake_state

def save(cfg):
    """One trip through the save's health decision; returns probes issued."""
    before = probes["n"]
    health = A._health_for_config_save(dict(cfg))
    return probes["n"] - before, health


# ── The common case: nothing connection-relevant changed ────────────────────
n, _ = save(CFG)
check("the first save probes (nothing cached yet)", n == 1)
n, _ = save(CFG)
check("an identical save reuses the cached probe", n == 0)
n, _ = save({**CFG, "NOTIFY_ON_ERROR": False})
check("a notification toggle does not touch the network", n == 0)
n, _ = save({**CFG, "SCORE_BALANCE": 60})
check("a scoring change does not touch the network", n == 0)
n, _ = save({**CFG, "HEADROOM_GB": 40})
check("a threshold change does not touch the network", n == 0)
n, _ = save({**CFG, "MONITOR_DIRS": ["/library/movies"]})
check("a monitored-path change does not touch the network either",
      n == 0)   # paths change what a RUN measures, not whether a server answers


# ── Anything that could change the verdict re-probes ────────────────────────
for label, edit in (
    ("an API key",        {"JELLYFIN_API_KEY": "different"}),
    ("a server URL",      {"JELLYFIN_URL": "http://elsewhere:8096"}),
    ("selecting a server", {"USE_PLEX": True, "TAUTULLI_URL": "http://t:8181",
                            "TAUTULLI_API_KEY": "k"}),
    ("deselecting one",   {"USE_JELLYFIN": False}),
    ("a protected collection", {"JELLYFIN_PROTECTED_COLLECTIONS": ["Keep"]}),
):
    save(CFG)                       # re-prime the cache for the base config
    n, _ = save({**CFG, **edit})
    check(f"changing {label} re-probes", n == 1)


# ── A failed probe keeps re-probing, so recovery needs no tick ──────────────
HEALTHY["n"] = False
save(CFG)                                   # observe the outage
n, h = save(CFG)
check("while the last probe FAILED, an unrelated save re-probes",
      n == 1 and h["critical_ok"] is False)
n, _ = save({**CFG, "NOTIFY_ON_ERROR": False})
check("...and keeps re-probing for as long as it stays broken", n == 1)
HEALTHY["n"] = True
n, h = save(CFG)
check("the save that follows a recovery reports it immediately",
      n == 1 and h["critical_ok"] is True)
n, _ = save(CFG)
check("...then settles back to reusing the cache", n == 0)


# ── The reuse window is bounded by the tick that would refresh it anyway ────
check("the cache is trusted for no longer than one scheduler tick",
      A._HEALTH_REPROBE_AFTER_SECONDS == A.SCHEDULE_INTERVAL_MINUTES * 60)
save(CFG)
with A._connection_health_cache_lock:
    A._connection_health_cache["checked_at"] = time.time() - A._HEALTH_REPROBE_AFTER_SECONDS - 1
n, _ = save(CFG)
check("a cached probe older than that is refreshed", n == 1)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
