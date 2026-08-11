"""The scheduler tick: the once-a-day maintenance Simulate, the Redline
emergency's priority over tick housekeeping, and the Scheduler Mode lifecycle.

- Monitor Only: the daily slot launches the maintenance Simulate (scheduled=True,
  which is what produces the daily-summary notification); Automatic Cleanup
  within all limits owes the same Simulate, so quiet days still get a summary.
- Fully Paused runs the storage Summary only — no Simulate, no cleanup.
- A breached Redline launches on any tick, outranking a held reconcile and
  surviving a failed Summary precheck; without a Redline armed the daily
  window/run-time gate holds.
- The one-attempt-per-window latch holds (no respin every 15 minutes), and a
  run-time change or a later-in-the-day run time re-arms both once-per-day
  mechanisms.
- Mode lifecycle: incomplete setup or a missing/stale library database rests at
  fully-Paused, a completed scan wakes it back to Monitor Only, and run
  notifications follow the Monitor Only opt-in.
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
_OUT = tempfile.mkdtemp(prefix="mr-daily-sim.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)

# Pin the LOCAL time-of-day to midday before importing the app.
#
# Every slot time below is "now" shifted by minutes or an hour, and the app
# resolves a slot as a wall-clock HH:MM on TODAY's date. Near either end of the
# day those shifts silently cross midnight — an hour past 23:06 formats as
# "00:06", which reads as long PAST rather than an hour ahead — so the suite
# failed for one hour before midnight and two minutes after it. Picking a fixed
# UTC offset that puts local time at midday gives every shift room on both
# sides. Etc/GMT-X is X hours EAST, so local = UTC + X; the zone database only
# defines Etc/GMT+12 through Etc/GMT-14, so an offset past +14 is expressed as
# its negative equivalent.
_shift = (12 - time.gmtime().tm_hour) % 24
os.environ["TZ"] = f"Etc/GMT{-(_shift if _shift <= 14 else _shift - 24):+d}"
time.tzset()
assert 2 <= time.localtime().tm_hour <= 21, f"clock pinning failed: {time.localtime()}"

_now = time.time()
_ran_time = time.strftime("%H:%M", time.localtime(_now - 120))   # daily slot 2 min ago
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A
import db

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


BASE = {
    "RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
    "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:1", "JELLYFIN_API_KEY": "k",
    "IMDB_RATINGS_URL": "https://x.test/r.tsv.gz", "OUTPUT_DIR": _OUT,
    "MAX_LIBRARY_GB": 400.0, "HEADROOM_GB": 0, "REDLINE_GB": None,
    "REDLINE_ONLY_MODE": True, "DAILY_RUN_TIME": _ran_time,
}

# Snapshot exists, built before today's run time (a scan ran this morning).
_morning = _now - 8 * 3600
A._read_library_snapshot = lambda: ({"built_at": _morning, "movies": []}, None)
A._refresh_connection_health_cache = lambda cfg, probe=True: {"critical_ok": True}
A._retry_held_reconcile = lambda cfg: False
A.run_summary = lambda *a, **k: (True, "stub")
A.run_summary_sync = lambda *a, **k: (True, "ok", {"library_gb": 300.0})
A.cached_disk_stats = lambda s: {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
A.disk_stats = lambda: {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
A._space_threshold_state = lambda *a, **k: {"ok_for_cleanup": True, "ok_for_simulate": True,
                                            "simulate_required": False, "cleanup_tooltip": ""}

launches = []
A.run_script = lambda *a, **k: (launches.append(k), (True, "stub"))[1]

def tick(mode, *, limits_exceeded):
    A.load_config = lambda: dict(BASE, RUN_MODE=mode)
    A._deletion_limits_exceeded = lambda *a: limits_exceeded
    A._scheduled_tick()


# ── Monitor Only: daily slot launches the scheduled maintenance Simulate ─────
A._last_maintenance_scan_epoch = None
launches.clear()
tick("paused", limits_exceeded=False)
check("paused: daily slot launches the maintenance Simulate",
      launches == [{"mode_override": "debug_sim", "scheduled": True}])
launches.clear()
tick("paused", limits_exceeded=False)
check("paused: latch holds — no respin on the next tick", launches == [])

# ── Fully paused ("off"): no Simulate/cleanup, but storage still refreshes ───
_summaries = []
_orig_summary = A.run_summary
A.run_summary = lambda *a, **k: (_summaries.append(1), (True, "stub"))[1]
A._last_maintenance_scan_epoch = None
launches.clear()
tick("off", limits_exceeded=False)
check("off: the owed daily Simulate does NOT launch", launches == [])
check("off: the quiet Summary still refreshes the storage numbers",
      _summaries == [1])
launches.clear()
tick("off", limits_exceeded=True)
check("off: over limits still launches nothing (Summary only)",
      launches == [] and _summaries == [1, 1])
A.run_summary = _orig_summary

# ── Automatic Cleanup, WITHIN limits: the daily slot still owes a Simulate ───
A._last_maintenance_scan_epoch = None
launches.clear()
tick("headroom", limits_exceeded=False)
check("armed + within limits: daily slot launches the maintenance Simulate",
      launches == [{"mode_override": "debug_sim", "scheduled": True}])
launches.clear()
tick("headroom", limits_exceeded=False)
check("armed + within limits: latch holds on the next tick", launches == [])

# ── Armed + OVER limits: the real cleanup launches instead (window open) ─────
A.reopen_daily_window(reason="test setup — startup burned today's window")
A._last_maintenance_scan_epoch = None
launches.clear()
tick("headroom", limits_exceeded=True)
check("armed + over limits: the cleanup launches, not the Simulate",
      launches == [{"scheduled": True}])

# ── Redline emergencies outrank tick housekeeping and survive precheck loss ──
_BREACHED = {"total_gb": 1000, "used_gb": 850, "free_gb": 150, "pct_used": 85}
_SAFE = {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}

# A failed Summary precheck must not silence a breached Redline: the fast path
# deletes from the standing queue and needs no library walk.
_orig_sync = A.run_summary_sync
A.run_summary_sync = lambda *a, **k: (False, "walk failed", {})
A.disk_stats = lambda: dict(_BREACHED)
A.load_config = lambda: dict(BASE, RUN_MODE="headroom", REDLINE_GB=200)
A._deletion_limits_exceeded = lambda *a: True
launches.clear()
A._scheduled_tick()
check("failed precheck + breached Redline: the emergency still launches",
      launches == [{"scheduled": True}])
A.disk_stats = lambda: dict(_SAFE)
launches.clear()
A._scheduled_tick()
check("failed precheck + floor safe: the tick holds (no launch)", launches == [])
A.run_summary_sync = _orig_sync

# A held reconcile normally consumes the tick — but not over a breached floor.
_recon_calls = []
A._retry_held_reconcile = lambda cfg: (_recon_calls.append(1), True)[1]
A._reconcile_held = (True, "test")   # what the stubbed retry stands in for
A._reconcile_deferred_ticks = 0
A.disk_stats = lambda: dict(_BREACHED)
A.cached_disk_stats = lambda s: dict(_BREACHED)
launches.clear()
A._scheduled_tick()
check("breached Redline outranks the held-reconcile retry",
      launches == [{"scheduled": True}] and _recon_calls == [])
A.disk_stats = lambda: dict(_SAFE)
A.cached_disk_stats = lambda s: dict(_SAFE)
launches.clear()
A._scheduled_tick()
check("floor safe: the held reconcile consumes the tick as before",
      launches == [] and len(_recon_calls) == 1)

# The deferral is capped. A breach the cleanup can't clear would otherwise hold
# the user's saved filtering/scoring change forever — and that change is often
# what decides which movies may be deleted in the first place.
_recon_calls.clear()
A._reconcile_deferred_ticks = 0
A.disk_stats = lambda: dict(_BREACHED)
A.cached_disk_stats = lambda s: dict(_BREACHED)
for _ in range(A._RECONCILE_DEFER_TICKS + 1):
    launches.clear()
    A._scheduled_tick()
check("a reconcile deferred by a standing breach still runs eventually",
      len(_recon_calls) == 1 and launches == [])
A._reconcile_held = None
A._reconcile_deferred_ticks = 0
A.disk_stats = lambda: dict(_SAFE)
A.cached_disk_stats = lambda s: dict(_SAFE)
A._retry_held_reconcile = lambda cfg: False

# Unreadable disk stats fail open toward launching ONLY when a Redline is
# armed — without one the daily window/run-time gate must keep holding.
A.load_config = lambda: dict(BASE)
A.burn_daily_window_on_startup()          # today's window used
A.cached_disk_stats = lambda s: None
A.disk_stats = lambda: None
A.load_config = lambda: dict(BASE, RUN_MODE="headroom", REDLINE_GB=None)
launches.clear()
A._scheduled_tick()
check("unreadable disk without a Redline: the window gate still holds", launches == [])
A.load_config = lambda: dict(BASE, RUN_MODE="headroom", REDLINE_GB=200)
launches.clear()
A._scheduled_tick()
check("unreadable disk with a Redline armed: fails open and launches",
      launches == [{"scheduled": True}])
A.cached_disk_stats = lambda s: dict(_SAFE)
A.disk_stats = lambda: dict(_SAFE)
A.reopen_daily_window(reason="test cleanup — restore the window")

# ── Low-space warning re-arms while NOT in cleanup mode too ──────────────────
A._store_notify_meta("notify_low_space", {"warned": True, "redline": 200.0, "margin": 25.0})
A._check_low_space_notification(dict(BASE, RUN_MODE="paused", REDLINE_GB=200),
                                {"free_gb": 500})
check("recovered space re-arms the warning even in Monitor Only",
      A._read_notify_meta("notify_low_space").get("warned") is False)
A._store_notify_meta("notify_low_space", {"warned": True, "redline": 200.0, "margin": 25.0})
A._check_low_space_notification(dict(BASE, RUN_MODE="paused", REDLINE_GB=None),
                                {"free_gb": 210})
check("removing the Redline clears a stale warned flag",
      A._read_notify_meta("notify_low_space").get("warned") is False)

# ── Moving the run time later re-arms the latch (new run epoch) ──────────────
A._last_maintenance_scan_epoch = A._todays_daily_run_epoch(BASE)   # today's slot consumed
launches.clear()
tick("paused", limits_exceeded=False)
check("consumed slot stays quiet", launches == [])
_later = time.strftime("%H:%M", time.localtime(_now - 30))   # a NEW slot, also passed
BASE["DAILY_RUN_TIME"] = _later
launches.clear()
tick("paused", limits_exceeded=False)
check("run time moved to a new slot re-arms the daily Simulate",
      launches == [{"mode_override": "debug_sim", "scheduled": True}])

# ── Startup: a passed daily run time burns today's maintenance slot ──────────
# A restart must not hand the scheduler an immediate scan; the slot only stays
# armed when the run time is still ahead of the clock today.
A._last_maintenance_scan_epoch = None
A.load_config = lambda: dict(BASE)                # run time already passed
A.burn_todays_maintenance_slot_on_startup()
check("startup burn consumes an already-passed slot",
      A._last_maintenance_scan_epoch == A._todays_daily_run_epoch(BASE))
launches.clear()
tick("paused", limits_exceeded=False)
check("after the burn a restart does NOT scan immediately", launches == [])
A._last_maintenance_scan_epoch = None
_future_time = time.strftime("%H:%M", time.localtime(_now + 3600))
A.load_config = lambda: dict(BASE, DAILY_RUN_TIME=_future_time)
A.burn_todays_maintenance_slot_on_startup()
check("a run time still ahead today leaves the slot armed",
      A._last_maintenance_scan_epoch is None)

# The CLEANUP window's startup burn follows the same rule: a slot still ahead
# stays armed (it's the scheduled run, not a lost-window catch-up); a passed
# slot burns.
with db.transaction(A.db_path()) as conn:
    conn.execute("DELETE FROM meta WHERE key='last_cleanup_date'")
A.load_config = lambda: dict(BASE, DAILY_RUN_TIME=_future_time)
A.burn_daily_window_on_startup()
check("startup keeps the cleanup window armed while the slot is ahead",
      A._headroom_window_used_today() is False)
A.load_config = lambda: dict(BASE)          # run time already passed
A.burn_daily_window_on_startup()
check("startup burns the cleanup window once the slot has passed",
      A._headroom_window_used_today() is True)

# ── Missing snapshot: rebuild-wiped stores re-scan, fresh installs stay manual ─
# A code/schema-change rebuild wipes the snapshot; the snapshot_ever_built
# marker survives it, so the daily slot re-scans. A never-scanned install
# (no marker) keeps onboarding's manual Simulate.
_orig_read_snap = A._read_library_snapshot
_orig_ever = A._snapshot_ever_built
A._read_library_snapshot = lambda: (None, "no snapshot")
A._snapshot_ever_built = lambda: True
A._last_maintenance_scan_epoch = None
launches.clear()
tick("paused", limits_exceeded=False)
check("wiped store (has scanned before): daily slot relaunches the Simulate",
      launches == [{"mode_override": "debug_sim", "scheduled": True}])
A._snapshot_ever_built = lambda: False
A._last_maintenance_scan_epoch = None
launches.clear()
tick("paused", limits_exceeded=False)
check("never-scanned install: stays on manual-Simulate onboarding", launches == [])
A._read_library_snapshot = _orig_read_snap
A._snapshot_ever_built = _orig_ever

# The marker itself survives the engine's code-change wipe (it is not part of
# the code-derived sections), so the distinction still holds across a rebuild.
db_p = A.db_path()
with db.transaction(db_p) as conn:
    db.set_meta(conn, "snapshot_ever_built", True)
    db.set_meta(conn, "snapshot_built_at", 12345)
    db.ensure_code_current(conn, "a-brand-new-engine-checksum")
    check("code-change wipe drops the snapshot stamp but keeps the marker",
          db.get_meta(conn, "snapshot_built_at") is None
          and db.get_meta(conn, "snapshot_ever_built") is True)

# ── Save-side: moving the time later reopens a burned cleanup window ─────────
with db.transaction(A.db_path()) as conn:
    db.set_meta(conn, "last_cleanup_date", time.strftime("%Y-%m-%d"))
check("window burned", A._headroom_window_used_today() is True)
A.reopen_daily_window(reason="test")
check("reopen clears the burn", A._headroom_window_used_today() is False)
_hhmm = time.strftime("%H:%M", time.localtime(_now))
check("moved-later detection", A._run_time_moved_ahead_today("03:00",
      time.strftime("%H:%M", time.localtime(_now + 3600)), _hhmm) is True)
check("moved-earlier is a burn, not a reopen", A._run_time_moved_into_past(
      "23:59", "00:01", _hhmm) is True)

# ── Monitor Only notification gate (NOTIFY_IN_MONITOR_ONLY, default off) ─────
import notify

_dispatched = []
notify.dispatch_run_report = lambda cfg, report: (_dispatched.append(report), (True, "stub"))[1]
_errors = []
notify.dispatch_error = lambda cfg, msg="", **kw: (_errors.append((msg, kw)), (True, "stub"))[1]
A._read_run_report = lambda: {"mode": "simulate", "eligible_count": 1}
A.pending_delete_forecast = lambda cfg=None: {"event_on": None, "ripe": 0}
A._marked_queue_state = lambda cfg: ("sig", {})
A._update_marked_baseline = lambda cfg=None: None

def dispatch(run_mode, effective_mode, *, monitor_toggle, scheduled=True):
    _dispatched.clear()
    A.load_config = lambda: dict(BASE, RUN_MODE=run_mode, NOTIFY_ENABLED=True,
                                 NOTIFY_IN_MONITOR_ONLY=monitor_toggle,
                                 NOTIFY_CUSTOM_URLS=["json://x"])
    A._dispatch_run_notifications(effective_mode, 0, False, scheduled=scheduled)
    time.sleep(0.3)   # the dispatch hands off to a daemon thread
    return len(_dispatched)

check("Monitor Only + toggle OFF: daily summary suppressed",
      dispatch("paused", "debug_sim", monitor_toggle=False) == 0)
check("Monitor Only + toggle ON: daily summary sent",
      dispatch("paused", "debug_sim", monitor_toggle=True) == 1)
check("Armed mode: daily summary sent regardless of the toggle",
      dispatch("headroom", "debug_sim", monitor_toggle=False) == 1)
check("Monitor Only manual cleanup + toggle OFF: suppressed",
      dispatch("paused", "headroom", monitor_toggle=False, scheduled=False) == 0)
check("Monitor Only manual cleanup + toggle ON: sent",
      dispatch("paused", "headroom", monitor_toggle=True, scheduled=False) == 1)
check("Armed manual cleanup: sent regardless of the toggle",
      dispatch("headroom", "headroom", monitor_toggle=False, scheduled=False) == 1)
check("Debug Cleanup is silent (watched live, like a manual Simulate)",
      dispatch("headroom", "debug_cleanup", monitor_toggle=True, scheduled=False) == 0)

# ── A failed run alerts with the engine's own words ─────────────────────────
# "A MediaReducer run failed — check the detailed log" would be the exact trip
# the alert exists to save. The engine's last progress write says
# what stopped answering and which stage it died in; this is where the app picks
# that up. A run that FAILED never has a report, so this alert is the only thing
# it can send — which is why the Errors toggle turning it off means silence.
import json as _json
_errors.clear()
A.load_config = lambda: dict(BASE, RUN_MODE="headroom", NOTIFY_ENABLED=True,
                             NOTIFY_ON_ERROR=True, NOTIFY_CUSTOM_URLS=["json://x"])
(A.output_dir() / "progress.json").write_text(_json.dumps({
    "status": "error", "stage": "SCORING",
    "message": "Tautulli stopped answering while reading movie details.",
    "issues": [{"category": "identity_mismatch", "count": 2, "detail": "Bob (2020)"}]}))
A._dispatch_run_notifications("headroom", 1, False, scheduled=False)
time.sleep(0.3)
check("a failed run alerts once", len(_errors) == 1)
check("...carrying the engine's sentence, not a generic line",
      bool(_errors) and _errors[0][0].startswith("Tautulli stopped answering"))
check("...and the stage it died in",
      bool(_errors) and _errors[0][1].get("stage") == "SCORING")
check("...and whatever it had recorded before dying",
      bool(_errors) and len(_errors[0][1].get("issues") or []) == 1)

# A torn or stale progress file must not put the wrong sentence in an alert —
# it falls back to the same words the dashboard shows for the same state.
_errors.clear()
(A.output_dir() / "progress.json").write_text("{not json")
A._dispatch_run_notifications("headroom", 1, False, scheduled=False)
time.sleep(0.3)
check("an unreadable progress file alerts with the shared fallback, not a borrowed line",
      len(_errors) == 1 and _errors[0][0] == A.RUN_ENDED_UNEXPECTEDLY)

# The REAL sequence for a killed engine: run_script's worker stamps progress.json
# terminal and THEN dispatches. The checks above drive the dispatcher directly,
# so they could not see the app writing its own third variant of "the run
# failed" a few lines earlier — which is exactly what it was doing.
check("the app has one sentence for a run that did not report why",
      "Run failed — see the detailed log." not in Path(A.__file__).read_text())
_errors.clear()
(A.output_dir() / "progress.json").write_text(json.dumps({
    "schema": 1, "status": "running", "phase": "scanning", "message": "Scoring movies"}))
A._mark_progress_terminal("error", A.RUN_ENDED_UNEXPECTEDLY)
A._dispatch_run_notifications("headroom", 1, False, scheduled=False)
time.sleep(0.3)
check("...and a killed run's alert carries it rather than the step it was on",
      len(_errors) == 1 and _errors[0][0] == A.RUN_ENDED_UNEXPECTEDLY)

# A NORMAL abort must survive that same marker: the engine already wrote a
# terminal status, so the marker has to leave its sentence alone.
_errors.clear()
(A.output_dir() / "progress.json").write_text(json.dumps({
    "schema": 1, "status": "error", "phase": "scanning", "stage": "SCORING",
    "message": "Tautulli stopped answering while reading movie details."}))
A._mark_progress_terminal("error", A.RUN_ENDED_UNEXPECTEDLY)
A._dispatch_run_notifications("headroom", 1, False, scheduled=False)
time.sleep(0.3)
check("an engine abort's own sentence survives the app's terminal marker",
      len(_errors) == 1 and _errors[0][0].startswith("Tautulli stopped answering"))

# A run that stops on a bad connection or an invalid threshold RETURNS from
# run() instead of exiting nonzero. Gating the alert on the exit code left the
# two failures a user is most likely to hit completely silent, while the
# dashboard showed the red x for them the whole time.
_errors.clear()
_dispatched.clear()
(A.output_dir() / "progress.json").write_text(json.dumps({
    "schema": 1, "status": "error", "phase": "checking",
    "message": "Connection check failed: Tautulli URL is blank."}))
A._dispatch_run_notifications("headroom", 0, False, scheduled=False)   # exit code ZERO
time.sleep(0.3)
check("a run that stopped without a nonzero exit still alerts",
      len(_errors) == 1 and _errors[0][0].startswith("Connection check failed"))
check("...and does not also send a run summary", not _dispatched)

# …and a genuinely clean run is unaffected.
_errors.clear()
_dispatched.clear()
(A.output_dir() / "progress.json").write_text(json.dumps({
    "schema": 1, "status": "done", "phase": "done", "message": "Dry run."}))
A._dispatch_run_notifications("headroom", 0, False, scheduled=False)
time.sleep(0.3)
check("a clean run still summarises and raises no failure alert",
      not _errors and len(_dispatched) == 1)

# ── A killed run does not name the step it was on as the failure ────────────
# No terminal write, so progress.json still says "running" with whatever line
# was in flight. The panel now prints that message AS the failure — red, behind
# a × — so carrying it over would blame "Checking connections…" for the death.
A._run_active = False
(A.output_dir() / "progress.json").write_text(json.dumps({
    "schema": 1, "status": "running", "phase": "checking",
    "message": "Checking connections…"}))
with A.app.test_client() as _c:
    _prog = _c.get("/api/run/progress").get_json()
_pmsg = _prog.get("message") or ""
check("a run that died without reporting reads as failed", _prog.get("status") == "error")
check("...and does not blame the step it happened to be on",
      "Checking connections" not in _pmsg)
check("...saying instead that it ended without saying why", "ended unexpectedly" in _pmsg)


# ── Onboarding mode lifecycle (applied on every config save) ─────────────────
def life(cfg, saved, connected, *, db_fresh=True):
    c = dict(cfg)
    _orig_fresh = A._library_db_fresh
    A._library_db_fresh = lambda cfg=None: db_fresh
    try:
        r = A._apply_setup_mode_lifecycle(c, dict(saved), {"critical_ok": connected})
    finally:
        A._library_db_fresh = _orig_fresh
    return r, c.get("RUN_MODE"), bool(c.get(A.RUN_MODE_USER_PAUSED_KEY))

# Paused has two meanings and only ONE of them sticks. The flag records the rare
# case — the user deliberately chose Paused — so that its ABSENCE means "merely
# resting", which is the right reading for a fresh install, a config RESET, and a
# hand-edited file alike. Recording the resting state instead made every config
# that lacked the flag permanently unwakeable: a reset writes the shipped
# defaults, so setting up again and running Simulate never left Paused.
check("nothing set up (no paths, no server) forces fully-Paused, still wakeable",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": []}, {}, False)
      == ("forced_off", "off", False))
check("a config with no flag at all is RESTING, not paused by the user",
      life({"RUN_MODE": "off", "MONITOR_DIRS": []}, {"RUN_MODE": "off"}, False)
      == ("", "off", False))
check("choosing Paused from Monitor Only records the deliberate pause",
      life({"RUN_MODE": "off", "MONITOR_DIRS": []},
           {"RUN_MODE": "paused"}, False)
      == ("", "off", True))
check("choosing Paused from Automatic Cleanup records it too",
      life({"RUN_MODE": "off", "MONITOR_DIRS": ["/library/movies"]},
           {"RUN_MODE": "headroom"}, True)
      == ("", "off", True))
check("a deliberate pause survives later unrelated saves",
      life({"RUN_MODE": "off", "MONITOR_DIRS": []},
           {"RUN_MODE": "off", A.RUN_MODE_USER_PAUSED_KEY: True}, False)
      == ("", "off", True))
check("leaving Paused for Monitor Only clears the deliberate flag",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 500},
           {"RUN_MODE": "off", A.RUN_MODE_USER_PAUSED_KEY: True}, True)
      == ("", "paused", False))
check("everything ready wakes a RESTING off into Monitor Only",
      life({"RUN_MODE": "off", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 500},
           {"RUN_MODE": "off"}, True) == ("auto_enabled", "paused", False))
# The working set now includes an armed space trigger: with none (the shipped
# default), a fresh scan alone must not wake Monitor Only — there is nothing
# for it to monitor.
check("ready in every way EXCEPT an armed trigger: no wake",
      life({"RUN_MODE": "off", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 0},
           {"RUN_MODE": "off"}, True) == ("", "off", False))
check("...and Monitor Only with no armed trigger rests at Paused",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 0},
           {"RUN_MODE": "paused"}, True) == ("forced_off", "off", False))
# Unticking BOTH servers is a deliberate structural removal, so it rests at
# Paused on the save itself — even if the probe half of "ready" said fine.
check("unticking both servers while in Monitor Only rests at Paused",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
            "USE_PLEX": False, "USE_JELLYFIN": False, "HEADROOM_GB": 500},
           {"RUN_MODE": "paused", "USE_JELLYFIN": True}, True)
      == ("forced_off", "off", False))
check("a DELIBERATE Paused sticks even with everything ready",
      life({"RUN_MODE": "off", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 500},
           {"RUN_MODE": "off", A.RUN_MODE_USER_PAUSED_KEY: True}, True)
      == ("", "off", True))
check("server connected but no paths yet: still rests at fully-Paused",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": []}, {}, True)
      == ("forced_off", "off", False))
check("paths saved but no server connected: still rests at fully-Paused",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"]}, {}, False)
      == ("forced_off", "off", False))
check("setup complete but database stale: Monitor Only demotes and waits",
      life({"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 500}, {}, True,
           db_fresh=False) == ("forced_off", "off", False))
check("a resting off with a stale database keeps waiting (no wake)",
      life({"RUN_MODE": "off", "MONITOR_DIRS": ["/library/movies"],
            "USE_JELLYFIN": True, "HEADROOM_GB": 500},
           {"RUN_MODE": "off"}, True, db_fresh=False)
      == ("", "off", False))
check("cleanup mode is left to the arming gates, never demoted here",
      life({"RUN_MODE": "headroom", "MONITOR_DIRS": []}, {}, False)
      == ("", "headroom", False))

# ── Mode/database sync after a run, Summary, or tick (both directions) ──────
_wake_saves = []
_orig_wsave = A.save_config
_orig_wfresh = A._library_db_fresh
_orig_wrestart = A._restart_schedule_clock
A.save_config = lambda cfg, **k: (_wake_saves.append(dict(cfg)), True)[1]
A._restart_schedule_clock = lambda: None
A._library_db_fresh = lambda cfg=None: True
_SET_UP = {"MONITOR_DIRS": ["/library/movies"], "USE_JELLYFIN": True,
           "HEADROOM_GB": 500}
A.load_config = lambda: {"RUN_MODE": "off", **_SET_UP}
A._sync_scheduler_mode_with_db()
check("a RESTING off wakes into Monitor Only after a completed scan",
      len(_wake_saves) == 1 and _wake_saves[-1]["RUN_MODE"] == "paused")
_wake_saves.clear()
# The reset case: /api/config/reset rewrites the shipped defaults while the
# app keeps running. The defaults arm NO space trigger, so the wake must wait
# for one even with a server, paths and a fresh scan — but the moment a
# trigger is armed on top, the same resting config wakes.
A.load_config = lambda: dict(json.load(open("default_config.json")),
                             MONITOR_DIRS=["/library/movies"], USE_JELLYFIN=True)
A._sync_scheduler_mode_with_db()
check("a freshly-RESET config (no trigger armed) does NOT wake", _wake_saves == [])
A.load_config = lambda: dict(json.load(open("default_config.json")), **_SET_UP)
A._sync_scheduler_mode_with_db()
check("...and wakes once a trigger is armed on top of the defaults",
      len(_wake_saves) == 1 and _wake_saves[-1]["RUN_MODE"] == "paused")
_wake_saves.clear()
A.load_config = lambda: {"RUN_MODE": "off", A.RUN_MODE_USER_PAUSED_KEY: True, **_SET_UP}
A._sync_scheduler_mode_with_db()
check("a DELIBERATE Paused never wakes from a scan", _wake_saves == [])
A.load_config = lambda: {"RUN_MODE": "off", "MONITOR_DIRS": [], "USE_JELLYFIN": True}
A._sync_scheduler_mode_with_db()
check("resting without full setup stays resting", _wake_saves == [])
A._library_db_fresh = lambda cfg=None: False
A.load_config = lambda: {"RUN_MODE": "off", **_SET_UP}
A._sync_scheduler_mode_with_db()
check("resting without a fresh database stays resting", _wake_saves == [])

# Demote: the engine wipes every code-derived section (snapshot + queue) the
# first time it runs after its own code changes — which is the startup
# Summary, AFTER the startup mode check already ran. Monitor Only must not
# stay armed over an empty store.
_wake_saves.clear()
A.load_config = lambda: {"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
                         "USE_JELLYFIN": True}
A._sync_scheduler_mode_with_db()
check("a wiped database drops Monitor Only to the resting Paused",
      len(_wake_saves) == 1 and _wake_saves[-1]["RUN_MODE"] == "off"
      and not _wake_saves[-1].get(A.RUN_MODE_USER_PAUSED_KEY))
_wake_saves.clear()
A.load_config = lambda: {"RUN_MODE": "headroom", "MONITOR_DIRS": ["/library/movies"],
                         "USE_JELLYFIN": True}
A._sync_scheduler_mode_with_db()
check("a wiped database also drops Automatic Cleanup to the resting Paused",
      len(_wake_saves) == 1 and _wake_saves[-1]["RUN_MODE"] == "off")
# A fresh database leaves a working mode completely alone.
_wake_saves.clear()
A._library_db_fresh = lambda cfg=None: True
A.load_config = lambda: {"RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
                         "USE_JELLYFIN": True}
A._sync_scheduler_mode_with_db()
check("a fresh database leaves Monitor Only armed", _wake_saves == [])
# Never mid-write: a run or Summary holding the lock defers the judgement.
A._library_db_fresh = lambda cfg=None: False
A._run_active = True
A._sync_scheduler_mode_with_db()
check("no demote while a run is mid-write", _wake_saves == [])
A._run_active = False
A._summary_active = True
A._sync_scheduler_mode_with_db()
check("no demote while a Summary is mid-write", _wake_saves == [])
A._summary_active = False
A.save_config = _orig_wsave
A._library_db_fresh = _orig_wfresh
A._restart_schedule_clock = _orig_wrestart

# ── Report enrichment: existing marks (queue minus this run's new marks) ─────
A._read_run_report = lambda: {
    "mode": "simulate", "eligible_count": 3,
    "marked_items": [{"title": "New", "path": "/m/new", "delete_on": "2026-08-05"}]}
A._marked_queue_state = lambda cfg: ("sig", {
    "/m/new": {"title": "New", "delete_on": "2026-08-05"},
    "/m/old1": {"title": "Old1", "delete_on": "2026-08-01"},
    "/m/old2": {"title": "Old2", "delete_on": "2026-08-03"}})
check("dispatch attaches existing marks minus the run's new ones",
      dispatch("headroom", "debug_sim", monitor_toggle=False) == 1
      and sorted((it["title"], it["delete_on"])
                 for it in _dispatched[0]["existing_marked_items"])
      == [("Old1", "2026-08-01"), ("Old2", "2026-08-03")]
      and _dispatched[0]["marked_total"] == 3)

# A daily Simulate that added NO new marks (every mark carried over from an
# earlier day) still lists them. The Simulate's marked_count is the total it
# would delete now, NOT a count of new marks, so it can't gate this section.
A._read_run_report = lambda: {
    "mode": "simulate", "eligible_count": 2562, "marked_count": 6,
    "marked_items": []}
A._marked_queue_state = lambda cfg: ("sig", {
    f"/m/old{i}": {"title": f"Old{i}", "delete_on": "2026-08-04"} for i in range(6)})
check("carried-over marks are still listed when a run adds none",
      dispatch("paused", "debug_sim", monitor_toggle=True) == 1
      and len(_dispatched[0].get("existing_marked_items") or []) == 6)

# A TRUNCATED list (the run made more new marks than a report carries) is the
# one case where the unlisted paths would misfile as "already marked".
A._read_run_report = lambda: {
    "mode": "simulate", "eligible_count": 900, "marked_count": 400,
    "marked_items": [{"title": f"New{i}", "path": f"/m/new{i}"}
                     for i in range(A._MARKED_ITEMS_CAP)]}
check("a truncated marked list skips the section rather than mislabel",
      dispatch("headroom", "debug_sim", monitor_toggle=False) == 1
      and "existing_marked_items" not in _dispatched[0])

# ── Flag parity: every toggle behaves the SAME in both modes ─────────────────
# Automatic Cleanup and Monitor Only (with the opt-in) must be
# indistinguishable notification-wise; the opt-in governs run-related alerts
# only, never the between-run ones. notify is reloaded so this exercises the
# REAL builders — the sections above replace dispatch_run_report with a stub.
import importlib
import notify as _notify
importlib.reload(_notify)
_fired = []
_notify._deliver = lambda urls, t, b, **k: (_fired.append(t), (True, "stub"))[1]
A.pending_deletion_entries = lambda cfg=None, with_lines=True: []
A._read_run_report = lambda: {"mode": "simulate", "eligible_count": 5,
                              "marked_count": 0, "marked_items": []}
A.pending_delete_forecast = lambda cfg=None: {"event_on": None, "ripe": 0}
A._marked_queue_state = lambda cfg: ("sig", {})
A._update_marked_baseline = lambda cfg=None: None
_NOTIFY_BASE = {"NOTIFY_ENABLED": True, "NOTIFY_CUSTOM_URLS": ["json://x"],
                "NOTIFY_ON_RUN_SUMMARY": True, "NOTIFY_ON_MARKED_CHANGES": True,
                "NOTIFY_ON_LOW_SPACE": True, "NOTIFY_SHOW_MOVIES": True,
                "NOTIFY_ON_ERROR": True, "NOTIFY_LOW_SPACE_GB": 25}

def _alert_fires(kind, mode, monitor_toggle, flag_off=None):
    cfg = dict(BASE, **_NOTIFY_BASE, RUN_MODE=mode,
               NOTIFY_IN_MONITOR_ONLY=monitor_toggle, REDLINE_GB=200)
    if flag_off:
        cfg[flag_off] = False
    A.load_config = lambda: dict(cfg)
    _fired.clear()
    if kind == "summary":
        A._dispatch_run_notifications("debug_sim", 0, False, scheduled=True); time.sleep(0.3)
    elif kind == "error":
        A._dispatch_run_notifications("debug_sim", 1, False, scheduled=True); time.sleep(0.3)
    elif kind == "marked":
        _notify.dispatch_marked_changes(cfg, new_items=[{"title": "M"}], marked_total=1)
    else:
        _notify.dispatch_low_space(cfg, free_gb=210, redline_gb=200, margin_gb=25)
    return len(_fired)

check("baseline: a Monitor Only summary needs the opt-in, then fires",
      _alert_fires("summary", "paused", False) == 0
      and _alert_fires("summary", "paused", True) == 1)

_FLAGS = (None, "NOTIFY_ENABLED", "NOTIFY_ON_RUN_SUMMARY", "NOTIFY_ON_MARKED_CHANGES",
          "NOTIFY_ON_LOW_SPACE", "NOTIFY_ON_ERROR")
_parity = _between_runs = True
for _kind in ("summary", "error", "marked", "lowspace"):
    for _flag in _FLAGS:
        _auto = _alert_fires(_kind, "headroom", False, _flag)
        _mon_on = _alert_fires(_kind, "paused", True, _flag)
        _mon_off = _alert_fires(_kind, "paused", False, _flag)
        if _auto != _mon_on:
            _parity = False
            print(f"  gap: {_kind} / {_flag} off — auto={_auto} monitor+optin={_mon_on}")
        if _kind in ("marked", "lowspace") and _mon_off != _auto:
            _between_runs = False
            print(f"  opt-in leaked into {_kind} / {_flag} off")
check("every flag behaves identically in Automatic Cleanup and Monitor Only", _parity)
check("between-run alerts ignore the Monitor Only run-notification opt-in", _between_runs)

# ── _library_db_fresh itself ─────────────────────────────────────────────────
# The mode lifecycle above stubs this helper, so exercise the real one here:
# it decides whether Monitor Only may stay armed, and a wrong answer either
# rests a healthy install at Paused or leaves a wiped store armed.
_cfg_dirs = dict(BASE, MONITOR_DIRS=["/library/movies"])
_scan_dirs = A._normalized_monitor_dirs(_cfg_dirs)
_orig_snap = A._read_library_snapshot

def _snap(built_at, dirs):
    A._read_library_snapshot = lambda: ({"built_at": built_at, "monitor_dirs": dirs}, None)

A._read_library_snapshot = lambda: (None, "missing")
check("no snapshot at all is not fresh", A._library_db_fresh(_cfg_dirs) is False)
_snap(time.time() - 60, _scan_dirs)
check("a recent snapshot of the current paths is fresh",
      A._library_db_fresh(_cfg_dirs) is True)
_snap(time.time() - (A._FULL_SCAN_MAX_AGE_SECONDS + 3600), _scan_dirs)
check("a snapshot past the full-scan window is not fresh",
      A._library_db_fresh(_cfg_dirs) is False)
_snap(time.time() - 60, ["/library/other"])
check("a snapshot of DIFFERENT paths is not fresh",
      A._library_db_fresh(_cfg_dirs) is False)
_snap(None, _scan_dirs)
check("a snapshot with no build time is not fresh",
      A._library_db_fresh(_cfg_dirs) is False)
A._read_library_snapshot = _orig_snap

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
