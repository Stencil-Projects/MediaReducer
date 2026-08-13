"""While a save's queue rebuild is running, nothing asks for a Simulate.

Change a space threshold and go to the Dashboard, and for a few seconds
Cleanup is ghosted with "Settings changed since the last Simulate — run it
again to rebuild the deletion plan", the Cleanup Targets card says "RUN
SIMULATE TO BUILD THE DELETION PLAN", and then both quietly go away and
Cleanup lights up. Nobody ran anything.

The plan really is stale in that window, so ghosting is right and the flicker
is the rebuild landing — _reconcile_after_save starts one for exactly these
keys. What was wrong is the instruction: the work was already under way and
finishes on its own, so asking the user to do it is advice that retracts
itself. A message that expires without being acted on teaches people to
ignore the next one.

_summary_active cannot answer this — a plain Summary refresh sets it too — so
the rebuild has its own flag, and it counts the queued and held cases because
both still end in a rebuild.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-rebuild.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


LIB, GB = 23489.4, 1_000_000_000
DISK = {"used_gb": 36829.2, "total_gb": 53994.2, "free_gb": 17165.0, "pct_used": 68.2}
A.disk_stats = lambda: dict(DISK)
A.library_stats = lambda: {"library_gb": LIB}
HEALTHY = {"critical_ok": True, "required_tooltip": "", "jellyfin_connected": True,
           "errors": [], "warnings": [], "blocked": False}
A._refresh_connection_health_cache = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_state = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_for_ui = lambda cfg=None: dict(HEALTHY)
# A queue exists but its stamp no longer matches — the state a threshold change
# leaves behind, and the one that produces the "settings changed" wording.
A._pending_raw = lambda: {"/library/Movies HD/a.mkv": {"title": "A", "size_bytes": 2 * GB,
                                                       "marked_at": None, "score": 10.0}}
A._pending_plan_current = lambda cfg, **k: False
A._simulate_evidence = lambda cfg=None, **k: True
A._full_scan_overdue = lambda: False

A.CONFIG_PATH.write_text(json.dumps({
    "OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/Movies HD"],
    "HEADROOM_GB": 0, "REDLINE_GB": None, "REDLINE_ONLY_MODE": False,
    "MAX_LIBRARY_GB": 21000, "MAX_HEADROOM_PCT": 15,
    "MOVIE_CLEANUP_ENABLED": True, "USE_JELLYFIN": True,
    "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k"}))
with A._config_memo_lock:
    A._config_memo["key"] = object()
cfg = A.load_config()
A.app.config["TESTING"] = True
client = A.app.test_client()


def message():
    return A._space_threshold_state(cfg, dict(DISK), LIB)["simulate_required_message"]


# ── Idle: the plan is stale and nothing is fixing it, so ASK ───────────────
A._reconcile_active = False
A._reconcile_queued = None
A._reconcile_held = None
idle = message()
check("with nothing running, a stale plan still asks for a Simulate",
      "Simulate" in idle, idle)
check("...and the status poll says no rebuild is under way",
      client.get("/api/status").get_json()["plan_rebuilding"] is False)

# ── Rebuild in flight: say what is happening instead ──────────────────────
for label, setup in (
        ("running", lambda: setattr(A, "_reconcile_active", True)),
        ("queued behind another job", lambda: setattr(A, "_reconcile_queued", (False, "test"))),
        ("held for a server that is down", lambda: setattr(A, "_reconcile_held", (True, "test")))):
    A._reconcile_active, A._reconcile_queued, A._reconcile_held = False, None, None
    setup()
    msg = message()
    check(f"rebuild {label}: the message stops asking for a Simulate",
          "Simulate" not in msg, msg)
    check(f"...and says a rebuild is under way ({label})",
          "Rebuilding the deletion plan" in msg, msg)
    check(f"...and the status poll flags it ({label})",
          client.get("/api/status").get_json()["plan_rebuilding"] is True)

# ── The gate itself does NOT relax — only the wording changes ─────────────
# The plan is genuinely stale while the rebuild runs, so Cleanup stays held.
# Softening the message must never soften the gate.
A._reconcile_active = True
st = A._space_threshold_state(cfg, dict(DISK), LIB)
check("a rebuild in flight still holds arming (the plan IS stale meanwhile)",
      st["simulate_required"] is True, st["simulate_required"])
A._reconcile_active = False

# ── The save actually raises the flag — not just this test's stubs ────────
# Everything above sets the globals by hand, so a run_reconcile that stopped
# setting _reconcile_active would leave the fix dead in the app and every check
# above still green. Drive it the way a threshold save does instead.
import threading  # noqa: E402

release = threading.Event()
A._pause_scheduler_for_run = lambda *a, **k: None
A._restart_schedule_clock = lambda *a, **k: None
A._run_reconcile_subprocess = lambda *a, **k: (release.wait(10), True)[1]
old_cfg = json.loads(A.CONFIG_PATH.read_text())
try:
    started = A._reconcile_after_save(old_cfg, {**old_cfg, "MAX_LIBRARY_GB": 21000 - 500},
                                      source="test")
    check("a space-threshold save launches the rebuild", started == "started", started)
    check("...and that launch is what the page sees", A._plan_rebuilding() is True)
    check("...so the page says so, with no hand-set flags",
          "Rebuilding the deletion plan" in message(), message())
finally:
    release.set()
for _ in range(200):
    if not A._plan_rebuilding():
        break
    __import__("time").sleep(0.05)
check("and it clears when the rebuild finishes, restoring the ask",
      A._plan_rebuilding() is False and "Simulate" in message(), message())

# ── And the dashboard is seeded with it, not left to guess ────────────────
# The Cleanup Targets card writes its own sentence — the server's message never
# reaches it — so the flag has to arrive twice: once in the render (the first
# paint is before any poll, and that first paint is where the user saw "RUN
# SIMULATE") and once on every poll after (or it would latch on and stay).
html = client.get("/").get_data(as_text=True)
check("the dashboard render seeds the rebuilding flag",
      "let _planRebuilding =" in html)

js = (ROOT / "static/js/dashboard.js").read_text(encoding="utf-8")
check("the poll keeps the flag current", "d.plan_rebuilding" in js)
check("the breach note reads it rather than always asking for a Simulate",
      "_planRebuilding" in js.split("} else if (_simulateRequired) {")[1].split("} else if")[0])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
