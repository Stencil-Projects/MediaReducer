"""Why Monitor Only is still locked has to be answerable.

Monitor Only unlocks on a COMPLETED scan, so a Simulate that aborts leaves the
scheduler resting at Paused — correct, but indistinguishable from never having
run one. The UI kept saying "Run Simulate first" to someone who had just done
exactly that, and the only visible symptom was a mode that "won't switch".

A first-run abort is easy to hit: the IMDb dataset downloads before the first
scan can score anything, so any network that blocks it fails every Simulate.

The lock reason is worded server-side (_scan_lock_reason) precisely so the
config page's mode card and the dashboard's paused line can never describe the
same state differently.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-scanlock.")
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


# An armed trigger is part of the working set now — arm one so each case
# below still isolates the scan-state reason it is about.
SET_UP = {"USE_JELLYFIN": True, "MONITOR_DIRS": ["/library/movies"],
          "HEADROOM_GB": 500}

_orig = (A._last_scan_failed, A._simulate_evidence, A._full_scan_overdue)
def state(*, failed=False, evidence=False, overdue=False):
    A._last_scan_failed = lambda: failed
    A._simulate_evidence = lambda cfg: evidence
    A._full_scan_overdue = lambda: overdue

# ── Each blocked state names itself ─────────────────────────────────────────
state()
check("no server and no paths asks for setup, not a Simulate",
      "Connect a media server" in A._scan_lock_reason({}))

check("set up but never scanned asks for the first Simulate",
      A._scan_lock_reason(SET_UP).startswith("Run Simulate first"))

state(failed=True)
_r = A._scan_lock_reason(SET_UP)
check("a FAILED run says so instead of asking again for the same Simulate",
      "didn't finish" in _r and "Run Simulate first" not in _r)

state(evidence=True, overdue=True)
_r = A._scan_lock_reason(SET_UP)
check("a scan that aged out says it is stale, not missing",
      "over two days old" in _r and "Run Simulate first" not in _r)

# ── A completed, current scan is not a locked state at all ──────────────────
state(evidence=True)
check("a current scan reports no reason (Monitor Only unlocks)",
      A._scan_lock_reason(SET_UP) == "")

# Setup completeness outranks scan state: without a server there is nothing to
# scan, so asking for a Simulate would be a dead end.
state(failed=True)
check("an incomplete setup is reported even when a run also failed",
      "Connect a media server" in A._scan_lock_reason({"MONITOR_DIRS": []}))
check("set up but NO armed space threshold asks for one, before any scan talk",
      "Arm a space threshold" in A._scan_lock_reason(
          {"USE_JELLYFIN": True, "MONITOR_DIRS": ["/library/movies"],
           "HEADROOM_GB": 0, "REDLINE_GB": None, "MAX_LIBRARY_GB": None}))

A._last_scan_failed, A._simulate_evidence, A._full_scan_overdue = _orig


# ── The failure signal itself ───────────────────────────────────────────────
# _last_scan_failed reads the terminal progress marker the run worker writes.
def write_progress(**kw):
    A.progress_path().write_text(json.dumps({"schema": 1, **kw}))

write_progress(status="error", message="ABORT: IMDb ratings dataset is missing")
check("an errored run reads as failed", A._last_scan_failed() is True)
write_progress(status="done", message="Simulation complete")
check("a completed run does not", A._last_scan_failed() is False)
write_progress(status="stopped", message="Run stopped.")
check("a user-initiated stop is not a failure (they know why it ended)",
      A._last_scan_failed() is False)
A.progress_path().write_text("{ this is not json")
check("an unreadable progress file is not reported as a failure",
      A._last_scan_failed() is False)
A.progress_path().unlink(missing_ok=True)
check("no progress file at all (fresh install) is not a failure",
      A._last_scan_failed() is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
