"""Automatic Cleanup cannot be armed until a Simulate has shown what it would remove.

This is the switch that deletes files on a schedule with nobody watching, and
the only thing between selecting it and that happening is a deletion plan. So
the gate has one job, and it has to do it in both directions:

  • with no plan, the radio is ghosted and says why;
  • when the gate cannot TELL whether there is a plan, it holds. It used to
    do the opposite — every failure inside it was swallowed into
    "no Simulate needed", which left ok_for_cleanup true and the switch live
    on an empty store. That is the one answer this gate must never give, and
    the only symptom was a radio button that should have been grey, on a page
    with nothing to suggest looking in a log.

The last check goes all the way to the rendered markup. The server value being
right is not the property anyone cares about; the property is that the control
is actually disabled, and those are two different things joined by a template.
"""
import json
import os
import re
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


LIB = OUT / "library"
(LIB / "movies").mkdir(parents=True, exist_ok=True)
# The shape this was reported in: Redline armed on its own, headroom and cap
# off, scheduler fully paused, nothing scanned yet.
CFG = {"OUTPUT_DIR": str(OUT), "MONITOR_DIRS": [str(LIB / "movies")],
       "RUN_MODE": "off", "HEADROOM_GB": 0, "REDLINE_GB": 500,
       "MAX_LIBRARY_GB": None, "REDLINE_ONLY_MODE": True,
       "MOVIE_CLEANUP_ENABLED": True, "USE_JELLYFIN": True,
       "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k"}
A.CONFIG_PATH.write_text(json.dumps(CFG))
with A._config_memo_lock:
    A._config_memo["key"] = object()

DISK = {"used_gb": 36811.7, "total_gb": 53994.2, "free_gb": 17182.5, "pct_used": 68.2}
A.disk_stats = lambda: dict(DISK)
A.library_stats = lambda: {"library_gb": 23472.0}
HEALTHY = {"critical_ok": True, "required_tooltip": "", "jellyfin_connected": True,
           "errors": [], "warnings": [], "blocked": False}
A._refresh_connection_health_cache = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_state = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_for_ui = lambda cfg=None: dict(HEALTHY)

cfg = A.load_config()


def state():
    return A._space_threshold_state(cfg, dict(DISK), 23472.0)


def armable(st):
    """What the Configuration page reads to decide whether the radio is live."""
    return bool(st["ok_for_cleanup"]) and not st["simulate_required"]


# ── Nothing scanned: held, and the reason names the Simulate ───────────────
st = state()
check("with no plan, arming is held", st["simulate_required"] is True and not armable(st), st)
check("...while the thresholds themselves are fine, so the reason is the plan",
      st["ok_for_cleanup"] is True, st)
check("...and the message asks for the Simulate",
      "Simulate" in st["simulate_required_message"], st["simulate_required_message"])


# ── The gate cannot tell: it holds anyway ─────────────────────────────────
# Every helper the decision leans on, one at a time. Which one broke does not
# matter — "I could not work it out" has exactly one safe answer.
for name in ("_full_scan_overdue", "_pending_plan_current", "_redline_only_mode_cfg",
             "_deletion_limits_exceeded", "_simulate_evidence", "_pending_raw"):
    real = getattr(A, name)
    setattr(A, name, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store unavailable")))
    try:
        st = state()
    finally:
        setattr(A, name, real)
    check(f"{name} failing holds arming rather than allowing it",
          st["simulate_required"] is True and not armable(st), st)
    check("...and still gives the user a sentence to act on",
          "Simulate" in (st["simulate_required_message"] or ""),
          st["simulate_required_message"])


# ── A plan with no scan behind it is not a plan ───────────────────────────
# The stamp and the library snapshot are written to different parts of the
# store, and they come apart: a scan that finishes with no monitored path
# mounted keeps the last good snapshot instead of writing an empty one, and
# stamps its plan anyway. On a library that had never been scanned that leaves
# a stamped plan and no snapshot — and _full_scan_overdue() reads False with no
# snapshot to age, so nothing else in the gate objects.
_real_plan, _real_ev = A._pending_plan_current, A._simulate_evidence
A._pending_plan_current = lambda cfg=None, **k: True
A._simulate_evidence = lambda cfg=None, **k: False
try:
    st = state()
finally:
    A._pending_plan_current, A._simulate_evidence = _real_plan, _real_ev
check("a stamped plan with no scan behind it does NOT arm Automatic Cleanup",
      st["simulate_required"] is True and not armable(st), st)
check("...and still asks for the Simulate",
      "Simulate" in (st["simulate_required_message"] or ""),
      st["simulate_required_message"])


# ── A plan on top of a real scan does arm it ──────────────────────────────
# Or the checks above would be satisfied by a gate that is simply always shut,
# which would be a different bug with the same test result.
A._pending_plan_current = lambda cfg=None, **k: True
A._simulate_evidence = lambda cfg=None, **k: True
try:
    st = state()
finally:
    A._pending_plan_current, A._simulate_evidence = _real_plan, _real_ev
check("a current deletion plan over a completed scan makes Automatic Cleanup selectable",
      st["simulate_required"] is False and armable(st), st)


# ── ...and the radio really is disabled in the page ───────────────────────
A.app.config["TESTING"] = True
client = A.app.test_client()
html = client.get("/config").get_data(as_text=True)
m = re.search(r'<input[^>]*id="mode-headroom"[^>]*>', html)
check("the Automatic Cleanup radio is in the page", m is not None)
check("...and is rendered disabled while no plan exists",
      m is not None and "disabled" in m.group(0), m.group(0)[:160] if m else "")
check("...with the card marked so it reads as unavailable",
      'id="mode-card-headroom"' in html
      and re.search(r'class="[^"]*run-mode-disabled[^"]*"[^>]*id="mode-card-headroom"', html)
      is not None, "")
check("...and Monitor Only is held too, for its own reason",
      re.search(r'<input[^>]*id="mode-paused"[^>]*disabled', html) is not None)

# The preamble the page hands to config.js has to agree with the markup, or the
# first status poll re-enables what the render just ghosted.
mm = re.search(r"^let _simulateRequired = (\w+);", html, re.M)
check("the page tells its own script the same thing",
      mm is not None and mm.group(1) == "true", mm.group(1) if mm else None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
