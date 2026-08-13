"""A Library Size Cap that falls below the safety floor stops Automatic Cleanup.

The cap may not sit below library × (100 − MAX_HEADROOM_PCT)%, so one cleanup
trims at most that percentage. Two ways to end up under it, and the second is
the one nobody types:

  • you SAVE a cap below the floor;
  • you save a legal one and the LIBRARY GROWS underneath it. Files copied in
    move the floor up while the cap stays where it is, so a setting that was
    fine when saved is a violation an hour later with nobody having touched it.

Either way the tick pauses Automatic Cleanup into Monitor Only with the reason,
and the manual Cleanup button ghosts — the button deletes immediately, so it
must not stay live on a threshold the scheduler has already refused.

test_safety_autopause covers the pause PLUMBING with _space_threshold_state
stubbed out, which means it passes whether or not the real cap rule ever
reaches it. This drives the real rule through the real tick, so the wiring
between "the cap is unsafe" and "Automatic Cleanup stopped" is what is under
test, not a fixture asserting itself.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-capauto.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


TOTAL = 53984.0
DISK = {"used_gb": 36829.2, "total_gb": TOTAL, "free_gb": 17154.8, "pct_used": 68.2}
launched = []

A.output_dir = lambda: Path(_OUT)
A.disk_stats = lambda: dict(DISK)
A.cached_disk_stats = lambda s=None: dict(DISK)
A._refresh_connection_health_cache = lambda cfg=None, probe=True: {"critical_ok": True}
A.run_summary = lambda: (True, "ok")
A.run_script = lambda *a, **k: (launched.append(1), (True, "started"))[1]
A._restart_schedule_clock = lambda: None
A._has_monitored_dirs = lambda cfg=None: True
A._library_db_fresh = lambda cfg=None: True
A._sync_scheduler_mode_with_db = lambda *a, **k: None
A._arm_daily_slot_job = lambda cfg=None: None
A._check_marked_change_notification = lambda *a, **k: None
A._check_low_space_notification = lambda *a, **k: None
A._maybe_run_daily_maintenance_sim = lambda cfg: False
A._headroom_window_used_today = lambda: False
A._run_active = False
A._summary_active = False
# A current plan exists: without one every gate below answers "run Simulate
# first" and the cap rule never gets a word in.
GB = 1_000_000_000
A._pending_raw = lambda: {"/library/Movies HD/a.mkv": {"title": "A", "size_bytes": 2 * GB,
                                                       "marked_at": None, "score": 10.0}}
A._pending_plan_current = lambda cfg, **k: True
A._simulate_evidence = lambda cfg=None, **k: True
A._full_scan_overdue = lambda: False

BASE = {"OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/Movies HD"], "MAX_HEADROOM_PCT": 15,
        "HEADROOM_GB": 0, "REDLINE_GB": None, "REDLINE_ONLY_MODE": False,
        "MOVIE_CLEANUP_ENABLED": True, "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test",
        "JELLYFIN_API_KEY": "k", "RUN_MODE": "headroom", "DAILY_RUN_TIME": "03:00"}


def tick(library_gb, **over):
    """One scheduled tick with the library at this size. Returns the config after."""
    launched.clear()
    A.CONFIG_PATH.write_text(json.dumps({**BASE, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    A.library_stats = lambda: {"library_gb": library_gb}
    A.run_summary_sync = lambda *a, **k: (True, "ok", {"library_gb": library_gb})
    A._scheduled_tick_body()
    return json.loads(A.CONFIG_PATH.read_text())


A.app.config["TESTING"] = True
client = A.app.test_client()


def button_ghosted(library_gb, **over):
    """What the Dashboard's manual Cleanup button does with this config."""
    A.CONFIG_PATH.write_text(json.dumps({**BASE, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    A.library_stats = lambda: {"library_gb": library_gb}
    st = A._cleanup_button_state(A.load_config(), dict(DISK)) if hasattr(
        A, "_cleanup_button_state") else None
    if st is None:                              # name it via the status payload instead
        st = client.get("/api/status").get_json()["cleanup_state"]
    return bool(st["cleanup_disabled"]), st["cleanup_tooltip"]


# ── Saved below the floor: stopped, and told why ──────────────────────────
# 19,000 against a 23,489.5 GB library would trim 19.1% — past the 15%.
after = tick(23489.5, MAX_LIBRARY_GB=19000)
check("a cap under the floor pauses Automatic Cleanup into Monitor Only",
      after["RUN_MODE"] == "paused", after["RUN_MODE"])
check("...with the reason recorded for the page to show",
      "safety percentage" in after.get("_RUN_MODE_AUTOPAUSE_REASON", ""),
      after.get("_RUN_MODE_AUTOPAUSE_REASON"))
check("...and no deletion pass launched on the way out", not launched)
ghosted, tip = button_ghosted(23489.5, MAX_LIBRARY_GB=19000)
check("...and manual Cleanup ghosts — it deletes immediately", ghosted, tip)
check("...naming the floor, not just the percentage", "19,966 GB" in tip, tip)

# ── The library GREW under a legal cap: same treatment ────────────────────
# 22,000 is legal at 23,489.5 (floor 19,966) and a violation at 26,500
# (floor 22,525) with the cap untouched. This is the case a save-time check
# alone can never catch.
after = tick(23489.5, MAX_LIBRARY_GB=22000)
check("a legal cap keeps Automatic Cleanup armed",
      after["RUN_MODE"] == "headroom", after["RUN_MODE"])
ghosted, tip = button_ghosted(23489.5, MAX_LIBRARY_GB=22000)
check("...and leaves manual Cleanup clickable", not ghosted, tip)

after = tick(26500.0, MAX_LIBRARY_GB=22000)
check("the same cap, once the library outgrows it, pauses to Monitor Only",
      after["RUN_MODE"] == "paused", after["RUN_MODE"])
check("...with the reason, and nothing deleted first",
      "safety percentage" in after.get("_RUN_MODE_AUTOPAUSE_REASON", "") and not launched,
      after.get("_RUN_MODE_AUTOPAUSE_REASON"))
ghosted, _tip = button_ghosted(26500.0, MAX_LIBRARY_GB=22000)
check("...and manual Cleanup ghosts on the grown library too", ghosted)

# ── ...unless the deletion delay is why it is over ────────────────────────
# The guard measures the library NET OF WHAT IS ALREADY MARKED, because those
# bytes are on their way out and held only by the delay. Against the raw size it
# fires on its own tail: marks wait out the delay, the library grows meanwhile,
# the floor climbs over a cap nobody touched, and the trip pauses the very
# cleanup that would have brought the library back under.
NOW = __import__("time").time()


def with_queue(marked_gb=0.0, eligible_gb=0.0):
    q = {}
    if marked_gb:
        q["/library/Movies HD/marked.mkv"] = {"title": "Marked", "score": 1.0,
                                              "size_bytes": int(marked_gb * GB),
                                              "marked_at": NOW}
    if eligible_gb:
        q["/library/Movies HD/elig.mkv"] = {"title": "Eligible", "score": 1.0,
                                            "size_bytes": int(eligible_gb * GB),
                                            "marked_at": None}
    A._pending_raw = lambda: dict(q)


# 26,500 × 85% = 22,525, so a 22,000 cap is 525 GB under the raw floor...
with_queue(marked_gb=1489)
after = tick(26500.0, MAX_LIBRARY_GB=22000)
check("marks awaiting the delay keep Automatic Cleanup armed",
      after["RUN_MODE"] == "headroom", after["RUN_MODE"])
ghosted, tip = button_ghosted(26500.0, MAX_LIBRARY_GB=22000)
check("...and leave manual Cleanup clickable", not ghosted, tip)

# ...and netting cannot excuse a cap that is simply too low: subtracting the
# queue lowers the floor by 85% of the queue, so a gap the queue can't cover
# still trips. Without this the exemption would be a way to disable the rule.
with_queue(marked_gb=20000)
ghosted, tip = button_ghosted(23489.5, MAX_LIBRARY_GB=1)
check("a 1 GB cap still trips however much is marked", ghosted, tip)

# The netting is CLAMPED to one safety percentage of the library, which is the
# most a single pass may take. Unclamped, a queue marked against an already-
# illegal cap lowers the floor by 85% of ITSELF and then admits a cap whose
# first pass deletes the whole queue — the bar lowered by the very bytes that
# jump over it. Real numbers from a 23,489 GB library with 8,800 GB marked:
# the unclamped floor is 12,486 and lets a 12,500 GB cap through, whose first
# pass takes 37% of the library. Clamped to 15% the floor holds at 16,971.
with_queue(marked_gb=8799.7)
ghosted, tip = button_ghosted(23489.4, MAX_LIBRARY_GB=12500)
check("a queue bigger than the safety percentage cannot buy a lower cap", ghosted, tip)
ghosted, _tip = button_ghosted(23489.4, MAX_LIBRARY_GB=20000)
check("...while a cap above the clamped floor still passes", not ghosted)

# Merely ELIGIBLE entries are not netted out — no run has committed to them, so
# nothing is on its way anywhere and the delay is not what is holding them.
with_queue(eligible_gb=1489)
after = tick(26500.0, MAX_LIBRARY_GB=22000)
check("an eligible-only queue does not buy the exemption",
      after["RUN_MODE"] == "paused", after["RUN_MODE"])

with_queue()   # back to an empty queue for the case below

# ── Already stopped: no churn ─────────────────────────────────────────────
# Re-arming is the user's two-click confirm, so a paused mode is left alone
# rather than re-stamped with a fresh reason every 15 minutes.
after = tick(26500.0, MAX_LIBRARY_GB=22000, RUN_MODE="paused")
check("an already-paused mode is not re-paused each tick",
      after["RUN_MODE"] == "paused" and not after.get("_RUN_MODE_AUTOPAUSE_REASON"),
      after.get("_RUN_MODE_AUTOPAUSE_REASON"))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
