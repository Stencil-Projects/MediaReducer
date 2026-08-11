"""The way out of a config lockout.

A hand-edited config.json with a bad value locks the run buttons and refuses
every save; /api/config/reset-invalid is the targeted way back — it defaults
ONLY the flagged keys and leaves everything else alone, where "Reset
MediaReducer" throws the whole configuration away. If it does not work, the
only remaining fix is editing the file by hand on the host, which is the
situation it exists to avoid.

It had no test at all. Coverage put it at zero lines executed, which is how a
26-line endpoint with a fixed-point loop, a cross-key partner rule, an
unparseable-file branch and a safety re-arm goes unexercised.

Three things it has to get right, in order of what they cost:

  • The safeties are re-applied. They are SKIPPED while the file is invalid,
    so a config.json hand-edited to RUN_MODE cleanup arrives here with the
    startup pause never having run. Coming out of the reset still armed means
    the next tick deletes — from a file the app just told the user was broken.
  • Only the flagged keys move. Everything else is the user's configuration,
    and silently reverting a monitored directory or a threshold they chose is
    a different kind of data loss.
  • It converges. Defaulting one key can flag another (the REDLINE_GB default
    can exceed a valid smaller HEADROOM_GB), so it iterates — and an
    iteration that gives up leaves the lockout exactly where it was.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
OUT = Path(_tmpout.config())
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


A.app.config["TESTING"] = True
client = A.app.test_client()

# The endpoint re-probes connections in a thread and re-applies the startup
# safeties. Both are stubbed to record rather than run: what is under test is
# WHETHER they are called, and a real probe would reach for a media server.
armed = []
A._refresh_connection_health_cache = lambda cfg=None, **kw: {}
A.force_paused_run_mode_on_startup = lambda: armed.append("paused")
A.burn_daily_window_on_startup = lambda reason=None: armed.append("window")
A.burn_todays_maintenance_slot_on_startup = lambda: armed.append("slot")

DEFAULTS = A._CONFIG_DEFAULTS


def write(cfg):
    """Put a config on disk the way a hand edit would, and make the app read it.
    load_config memoizes on (mtime_ns, size), which two writes inside one
    nanosecond-resolution tick can share, so the memo is cleared outright."""
    del armed[:]
    A.CONFIG_PATH.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    A.load_config()


def reset():
    # Every mutating request carries the header; the server rejects writes
    # without it (see _security_headers). A test that forgets it gets a
    # rejection that reads exactly like the endpoint refusing on its own terms.
    r = client.post("/api/config/reset-invalid", headers={"X-MediaReducer": "1"})
    return r.status_code, r.get_json()


def saved():
    return json.loads(A.CONFIG_PATH.read_text())


BASE = {"OUTPUT_DIR": str(OUT), "MONITOR_DIRS": ["movies", "tv"],
        "HEADROOM_GB": 500, "DELETE_DELAY_DAYS": 5, "GRACE_PERIOD_DAYS": 14}

# ── One bad key, and only that key moves ───────────────────────────────────
write({**BASE, "GRACE_PERIOD_DAYS": 2.5})
code, body = reset()
after = saved()
check("a flagged value is reset", code == 200 and body.get("ok") is True, body)
check("...to its default",
      after["GRACE_PERIOD_DAYS"] == DEFAULTS["GRACE_PERIOD_DAYS"], after.get("GRACE_PERIOD_DAYS"))
check("...and the lockout is lifted", not A._CONFIG_FILE_ISSUES, A._CONFIG_FILE_ISSUES)
check("every valid setting the user chose is left alone",
      after["MONITOR_DIRS"] == ["movies", "tv"] and after["HEADROOM_GB"] == 500
      and after["DELETE_DELAY_DAYS"] == 5, after)

# ── The safeties skipped during the lockout are re-applied ─────────────────
# The whole point: startup could not pause a run mode it refused to read.
check("the startup pause is applied on the way out", "paused" in armed, armed)
check("...along with both once-a-day burns",
      "window" in armed and "slot" in armed, armed)

# A live run mode hand-edited into the broken file must not survive the reset
# armed. force_paused_run_mode_on_startup is what disarms it, and it is stubbed
# above — so this checks the ORDER instead: the safeties run only once the file
# is valid, and they do run.
write({**BASE, "RUN_MODE": "cleanup", "DELETE_DELAY_DAYS": 0})
code, body = reset()
check("a hand-edited live mode reaches the reset with the safeties still owed",
      body.get("ok") is True and "paused" in armed, (body, armed))

# ── Several bad keys at once ───────────────────────────────────────────────
write({**BASE, "GRACE_PERIOD_DAYS": 2.5, "DELETE_DELAY_DAYS": 0,
       "MAX_HEADROOM_PCT": 200, "SCORE_BALANCE": -5})
code, body = reset()
after = saved()
check("every flagged key is reset in one pass",
      body.get("ok") is True
      and after["GRACE_PERIOD_DAYS"] == DEFAULTS["GRACE_PERIOD_DAYS"]
      and after["DELETE_DELAY_DAYS"] == DEFAULTS["DELETE_DELAY_DAYS"]
      and after["MAX_HEADROOM_PCT"] == DEFAULTS["MAX_HEADROOM_PCT"]
      and after["SCORE_BALANCE"] == DEFAULTS["SCORE_BALANCE"], after)
check("...and MONITOR_DIRS still survives a multi-key reset",
      after["MONITOR_DIRS"] == ["movies", "tv"], after.get("MONITOR_DIRS"))

# ── The cross-key case the loop exists for ────────────────────────────────
# Redline must sit below Headroom. With a small valid headroom and a bad
# redline, defaulting the redline alone lands ABOVE it and flags again — so
# the partner rule drags HEADROOM_GB along rather than iterating forever.
write({**BASE, "HEADROOM_GB": 50, "REDLINE_GB": -1})
code, body = reset()
after = saved()
check("a reset that would re-flag its partner resolves instead of looping",
      body.get("ok") is True and not A._CONFIG_FILE_ISSUES,
      (body, A._CONFIG_FILE_ISSUES))
check("...leaving a config the app will actually accept",
      not A._config_file_issues(after), A._config_file_issues(after))

# ── A file that is not JSON at all ────────────────────────────────────────
# Nothing can be salvaged key-by-key, so it is replaced wholesale. This is the
# one case where the user's settings are lost, and it is the only case where
# there are no settings left to read.
write("{ this is not json")
code, body = reset()
after = saved()
check("an unparseable file is replaced with the defaults",
      body.get("ok") is True and after.get("HEADROOM_GB") == DEFAULTS["HEADROOM_GB"], body)
check("...and the app is usable again", not A._CONFIG_FILE_ISSUES, A._CONFIG_FILE_ISSUES)

# ── A file with nothing wrong with it ─────────────────────────────────────
write(BASE)
code, body = reset()
after = saved()
check("a valid config is reported fine and left untouched",
      body.get("ok") is True and after["HEADROOM_GB"] == 500
      and after["GRACE_PERIOD_DAYS"] == 14, after)

# ── Never during a run ────────────────────────────────────────────────────
# Same rule as every other config-mutating endpoint: rewriting the config out
# from under a live engine re-applies the startup safeties mid-deletion.
write({**BASE, "GRACE_PERIOD_DAYS": 2.5})
A._run_active = True
try:
    code, body = reset()
finally:
    A._run_active = False
check("a reset is refused while a run is active", body.get("ok") is not True, body)
check("...and the file is not touched", saved()["GRACE_PERIOD_DAYS"] == 2.5, saved())

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
