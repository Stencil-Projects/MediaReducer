"""App-layer safety and resilience that the scenario tests don't reach:

  • /api/run's load-bearing safety default — a missing/garbled run mode must
    become a non-destructive Simulate, NEVER a live deletion — plus the
    unknown-mode 400 and run-active 409 guards.
  • The run-log section/error extraction that backs the log viewer's jump
    buttons: the "COMPLETED WITH ERRORS" report is served whole and content
    sections stop before it; an early ABORT gets a synthetic RUN FAILED banner.
  • The hostile-input guards on the hand-editable pending-deletions queue: a
    garbage size or an out-of-range marked_at epoch must read as safe defaults,
    not 500 every status poll.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_OUT = tempfile.mkdtemp(prefix="mr-appcov.")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# ── /api/run: the missing-mode → Simulate safety default ─────────────────────
# Stub every gate so the route reaches the launch; capture what mode it launches.
_launched = {}
def _fake_run_script(mode_override=None, manual=False):
    _launched["mode"] = mode_override
    _launched["manual"] = manual
    return True, "started"
A.run_script = _fake_run_script
A._refresh_connection_health_cache = lambda cfg=None, probe=True: {"critical_ok": True}
A.disk_stats = lambda: {}
A._space_threshold_state = lambda cfg=None, disk=None, **k: {
    "ok_for_simulate": True, "ok_for_cleanup": True, "simulate_required": False}
A._has_monitored_dirs = lambda cfg=None: True
# Skip the "already satisfied" precheck (degrade-don't-block else branch) so the
# route proceeds straight to the launch regardless of disk state.
A.run_summary_sync = lambda timeout=600: (False, "precheck skipped", {})
A._run_active = False

client = A.app.test_client()
# The API's simple-CSRF gate requires this header on every write request.
HDR = {"X-MediaReducer": "1"}

_launched.clear()
r = client.post("/api/run", json={}, headers=HDR)       # empty body: no mode at all
check("an empty /api/run body launches, not rejected", r.status_code == 200)
check("a missing run mode falls back to the non-destructive Simulate",
      _launched.get("mode") == "debug_sim")
check("the safety default is NOT a live deletion mode",
      not A._is_cleanup_mode(_launched.get("mode")))

_launched.clear()
r = client.post("/api/run", json={"mode": "headroom"}, headers=HDR)
check("an explicit live mode is honored", _launched.get("mode") == "headroom" and r.status_code == 200)

r = client.post("/api/run", json={"mode": "evil-scripted-mode"}, headers=HDR)
check("an unknown run mode is rejected 400", r.status_code == 400)
check("an unknown mode never launched a run", _launched.get("mode") == "headroom")

A._run_active = True
try:
    r = client.post("/api/run", json={"mode": "debug_sim"}, headers=HDR)
    check("a run already in progress is rejected 409", r.status_code == 409)
finally:
    A._run_active = False

# ── Log section + error extraction (pure, over synthetic run logs) ───────────
def L(*text):  # build a lines list the way _read_tail_lines yields it
    return [t + "\n" for t in text]

clean = L("======= SCAN =======", "scanned 10 movies",
          "======= ELIGIBLE CANDIDATES =======", "Candidate stats: 3",
          "======= SIMULATION =======", "DRY RUN DELETE #1: film",
          "SUMMARY [ok]", "done")
found, scan = A._extract_log_section(clean, "scan")
check("scan section runs from its banner to the next section",
      found and "scanned 10 movies" in scan and "Candidate stats" not in scan)
found, summ = A._extract_log_section(clean, "summary")
check("summary section runs to end of file", found and "done" in summ)
found, _ = A._extract_log_section(clean, "deletions")
check("a present section reports found", found)

# A run that completed WITH errors: the error report sits between deletions and
# summary and must be served whole by the errors view — and content sections
# must stop before it rather than absorbing it.
witherr = L("======= SIMULATION =======", "DRY RUN DELETE #1: film",
            "!!!!!! COMPLETED WITH ERRORS !!!!!!", "SKIP identity_mismatch: twin.mkv",
            "SUMMARY [errors]", "done")
found, errs = A._extract_log_section(witherr, "errors")
check("the COMPLETED WITH ERRORS report is served whole",
      found and "identity_mismatch" in errs and "SUMMARY" not in errs)
found, dele = A._extract_log_section(witherr, "deletions")
check("a content section stops before the error report banner",
      found and "DRY RUN DELETE #1" in dele and "COMPLETED WITH ERRORS" not in dele)

# A run that died early (ABORT, no banner) gets a synthetic RUN FAILED header.
died = L("======= SCAN =======", "scanning...", "ABORT: Plex unreachable")
found, rep = A._extract_errors_report(died, A._log_section_indexes(died))
check("an early ABORT yields a synthetic RUN FAILED report",
      found and "RUN FAILED" in rep and "Plex unreachable" in rep)

# A clean run has no errors view.
found, _ = A._extract_log_section(clean, "errors")
check("a clean run reports no errors section", not found)

# A Debug Cleanup works from the marked queue (no library scan), so its log has no
# SCAN / ELIGIBLE CANDIDATES banners — but its "Marked-queue re-verify" and
# "Delete-from-queue preview" stages map to the eligible & deletions sections, so the
# same detailed-log jump targets work, mirroring a real Cleanup.
dbg = L("====== DEBUG CLEANUP (no deletions) ======",
        "── Marked-queue re-verify (same as a Cleanup) ──",
        "Mark re-size: refreshed plays & scores for 40 movie(s).",
        "── Delete-from-queue preview [LIBRARY CAP] — target ~2642.4 GB ──",
        "WOULD DELETE (queue #1): Starship: Rising | score=14.3 | size=0.79 GB | path=/x",
        "====== CLEANUP SUMMARY [DEBUG CLEANUP] ======",
        "Debug Cleanup: the marked queue WOULD free ~308.7 GB", "done")
found_e, elig = A._extract_log_section(dbg, "eligible")
check("Debug Cleanup: the marked-queue re-verify is the eligible section",
      found_e and "refreshed plays" in elig and "WOULD DELETE" not in elig)
found_d, dele2 = A._extract_log_section(dbg, "deletions")
check("Debug Cleanup: the delete-from-queue preview is the deletions section",
      found_d and "WOULD DELETE (queue #1)" in dele2 and "CLEANUP SUMMARY" not in dele2)
found_s, _ = A._extract_log_section(dbg, "summary")
check("Debug Cleanup: the summary section is found", found_s)
found_sc, _ = A._extract_log_section(dbg, "scan")
check("Debug Cleanup: there is no scan section (it never scans the library)", not found_sc)

# ── Hostile pending-queue input: no crash, safe reads ────────────────────────
check("_entry_size_bytes reads garbage size as 0",
      A._entry_size_bytes({"size_bytes": "not-a-number"}) == 0
      and A._entry_size_bytes({"size_bytes": None}) == 0
      and A._entry_size_bytes({}) == 0)
check("_entry_size_bytes reads a real size", A._entry_size_bytes({"size_bytes": "1024"}) == 1024)
check("_entry_size_bytes clamps a negative to 0", A._entry_size_bytes({"size_bytes": -5}) == 0)

# pending_deletion_entries over an out-of-range epoch + a non-numeric marked_at:
# it must return safely (treating both as "just marked"), not raise.
_now = time.time()
A._pending_raw = lambda: {
    "/m/huge.mkv": {"marked_at": 1e18, "size_bytes": 1000, "title": "Huge Epoch"},
    "/m/bad.mkv":  {"marked_at": "garbage", "size_bytes": 2000, "title": "Bad Stamp"},
    "/m/ok.mkv":   {"marked_at": _now, "size_bytes": 3000, "title": "Fine"},
}
A.load_config = lambda: {"OUTPUT_DIR": _OUT, "DELETE_DELAY_DAYS": 3}
try:
    entries = A.pending_deletion_entries()
    check("an out-of-range / non-numeric marked_at does not crash the queue",
          len(entries) == 3)
    check("every hostile entry still renders as a marked row",
          all(e.get("marked") for e in entries))
except Exception as e:
    check(f"pending_deletion_entries survived hostile input (raised {e!r})", False)

# ── /api/queue/reset-delays: every marked clock restarts from NOW ────────────
import db

_old = _now - 5 * 86400   # marked 5 days ago
with db.transaction(A.db_path()) as conn:
    db.replace_queue(conn, {
        "/m/a.mkv": {"title": "A", "size_bytes": 1, "marked_at": _old},
        "/m/b.mkv": {"title": "B", "size_bytes": 1, "marked_at": _old - 86400},
        "/m/c.mkv": {"title": "C", "size_bytes": 1, "marked_at": None},   # eligible only
    })

A._run_active = True
r = client.post("/api/queue/reset-delays", headers=HDR)
check("reset-delays refuses while a run is active (409)", r.status_code == 409)
A._run_active = False
A._summary_active = True
r = client.post("/api/queue/reset-delays", headers=HDR)
check("reset-delays refuses while a reconcile/summary is active (409)", r.status_code == 409)
A._summary_active = False

_before = time.time()
r = client.post("/api/queue/reset-delays", headers=HDR)
d = r.get_json()
check("reset-delays restarts exactly the marked clocks",
      r.status_code == 200 and d.get("ok") and d.get("reset") == 2)
with db.connect(A.db_path()) as conn:
    rows = {r2["path"]: (r2["marked_at"], r2["delay_days"])
            for r2 in conn.execute("SELECT path, marked_at, delay_days FROM queue")}
check("marked clocks re-stamped to now",
      rows["/m/a.mkv"][0] >= _before and rows["/m/b.mkv"][0] >= _before)
check("reset stamps the CURRENT delay onto each mark",
      rows["/m/a.mkv"][1] == 3 and rows["/m/b.mkv"][1] == 3)
check("eligible (unmarked) entries stay unmarked", rows["/m/c.mkv"][0] is None)

with db.transaction(A.db_path()) as conn:
    db.replace_queue(conn, {})
r = client.post("/api/queue/reset-delays", headers=HDR)
d = r.get_json()
check("nothing marked: clean no-op, not an error",
      r.status_code == 200 and d.get("ok") and d.get("reset") == 0)

# ── Season marks run the same clock, so a reset re-dates them too ────────────
# Half the marked queue can be seasons. A reset that only touched the SQL queue
# would leave them on their original dates while the movies moved, and with only
# seasons marked the Config page would report nothing to reset at all.
A._pending_raw = lambda: {}          # the hostile-input stub above is done with
A._save_tv_cleanup_state({"marked": {
    "Show|1": {"marked_at": _old, "delay_days": 1, "title": "Show", "season": 1,
               "path": "/library/TV/Show", "size_bytes": 1, "score": 5},
    "Show|2": {"marked_at": _old - 86400, "delay_days": 1, "title": "Show", "season": 2,
               "path": "/library/TV/Show", "size_bytes": 1, "score": 6},
}})
_cfg_reset = {"OUTPUT_DIR": _OUT, "DELETE_DELAY_DAYS": 3,
              "HEADROOM_GB": 500, "REDLINE_GB": None, "MAX_LIBRARY_GB": None}
A.load_config = lambda: dict(_cfg_reset)
check("marked_clocked_count counts season marks, so the reset button is reachable",
      A._marked_clocked_count(A.load_config(), A.pending_delete_forecast()) == 2)

_before = time.time()
r = client.post("/api/queue/reset-delays", headers=HDR)
d = r.get_json()
check("reset-delays restarts season clocks with no movies marked",
      r.status_code == 200 and d.get("ok") and d.get("reset") == 2)
_marks = A._tv_cleanup_state()["marked"]
check("season clocks re-stamped to now",
      all(m["marked_at"] >= _before for m in _marks.values()))
check("reset stamps the CURRENT delay onto each season mark",
      all(m["delay_days"] == 3 for m in _marks.values()))

# Redline-only marks no seasons at all, so that branch must not add a count the
# reset can never act on.
A.load_config = lambda: {"OUTPUT_DIR": _OUT, "DELETE_DELAY_DAYS": 3,
                         "HEADROOM_GB": 0, "REDLINE_GB": 50, "MAX_LIBRARY_GB": None}
check("redline-only reports no clocked marks",
      A._marked_clocked_count(A.load_config(), A.pending_delete_forecast()) == 0)
A._save_tv_cleanup_state({"marked": {}})
A.load_config = lambda: dict(_cfg_reset)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
