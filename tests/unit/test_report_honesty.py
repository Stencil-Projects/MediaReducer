"""What a run REPORTS matches what the run DID — four ways it did not.

All four came out of one range review (the Aug 9-12 changes), verified against
source before being fixed, and each is a wrong CLAIM to the user rather than a
wrong deletion:

  1. The notification's "Newly marked …and N more" tail was computed from
     marked_count, which counts CARRIED marks the new-marks list deliberately
     omits — so a quiet day that carried 50 marks and added none sent
     "Newly marked: …and 50 more" with zero names.
  2. The overshoot note said "this run freed 42.0 GB" over reversible marks:
     planned_bytes sums carried marks, new marks, and externally-vanished
     files alongside real deletions, and the note used the sum with one verb.
  3. The quiet Summary re-sized the marked set from a library measure up to
     six hours old whenever the cap was armed (the reuse optimization), so
     out-of-band deletions left a stale covering set marked — clocks running,
     notifications firing — until the next fresh walk.
  4. The notification Debug preview rebuilt the last report WITHOUT the
     run's season side, showing a movie-only message that did not match what
     was actually sent — in the feature whose point is showing the message.
"""
import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
OUT = Path(_tmpout.config())
import notify  # noqa: E402
import shared  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


GB = 1_000_000_000
CFG = {"NOTIFY_SHOW_MOVIES": True, "NOTIFY_ON_RUN_SUMMARY": True, "RUN_MODE": "headroom"}


def run_msg(report):
    # build_run_message returns (title, body); the body is what the checks read.
    return "\n".join(notify.build_run_message(dict(CFG), dict(report)))


# ── 1. The "Newly marked" tail counts only this run's additions ────────────
BASE = {"mode": "cleanup", "deleted_count": 0, "bytes_freed": 0,
        "marked_count": 50, "eligible_count": 100, "message": "run done",
        "ts": time.time(), "issues": []}

msg = run_msg({**BASE, "marked_items": [], "marked_new_count": 0})
check("a quiet day (50 carried, 0 new) claims no new marks",
      "Newly marked" not in msg and "more" not in msg, msg)

msg = run_msg({**BASE, "marked_new_count": 5,
               "marked_items": [{"title": f"M{i}", "delete_on": "2026-08-20"}
                                for i in range(5)]})
check("5 new among 50 carried lists the 5, with no false tail",
      "M0" in msg and "M4" in msg and "more" not in msg, msg)

# The tail still fires for what it was built for: a list larger than the
# 40 names a message shows, where the REAL new count sizes the remainder.
msg = run_msg({**BASE, "marked_count": 300, "marked_new_count": 300,
               "marked_items": [{"title": f"M{i}", "delete_on": "2026-08-20"}
                                for i in range(200)]})
check("300 real new marks show 40 names and 'and 260 more'",
      "and 260 more" in msg, msg)

# An old report without the field must not resurrect the fabrication.
msg = run_msg({**BASE, "marked_items": []})
check("an unstamped old report claims nothing rather than fabricating",
      "Newly marked" not in msg, msg)

# ── 2. The overshoot note says what the run did ────────────────────────────
note = shared.composed_overshoot_note(0, 42 * GB, 2 * GB)
check("a mark-only run says marked, not freed",
      note.startswith("marked 42.0 GB") and "freed" not in note, note)
note = shared.composed_overshoot_note(42 * GB, 0, 2 * GB)
check("a delete-only run still says freed", note.startswith("freed 42.0 GB"), note)
note = shared.composed_overshoot_note(30 * GB, 12 * GB, 2 * GB)
check("a mixed run says both, sized to the sum",
      note.startswith("freed and marked a combined 42.0 GB"), note)
check("small change stays quiet in every wording",
      shared.composed_overshoot_note(0, int(2.4 * GB), 2 * GB) == ""
      and shared.composed_overshoot_note(int(2.4 * GB), 0, 2 * GB) == "")
check("vanished bytes are the caller's to exclude — zero args, no note",
      shared.composed_overshoot_note(0, 0, 2 * GB) == "")

# ── 3. The Summary only re-sizes marks from a fresh measure ────────────────
import engine as E  # noqa: E402
_tmpout.redirect_engine(E, str(OUT))

calls = []
E.log = lambda m="", **k: None
E.log_stage = lambda *a, **k: None
E.log_blank = lambda: None
E.emit_progress = lambda **k: None
E._revalidate_pending_marks = lambda deficit: calls.append(deficit)
E._daily_deficit_bytes = lambda u, m, l: 123
E.get_usage_info = lambda: {"used_gb": 100.0, "free": 900 * GB, "total": 1000 * GB}


def summary(cap, fresh):
    calls.clear()
    E.MAX_LIBRARY_GB = cap
    E.REDLINE_GB = None
    E.HEADROOM_GB = 0
    E.DELETE_DELAY_DAYS = 7
    E._report_debug_info(used_gb=100.0, max_gb=1000.0, free_gb=900.0,
                         library_gb=500.0, over_limit=False, redline_hit=False,
                         _cap_active=True, _threshold_errors=[], _total_gb=1000.0,
                         library_measure_fresh=fresh)
    return list(calls)


check("cap armed + reused measure: marks are left alone", summary(400, False) == [])
check("cap armed + fresh measure: marks re-verify", summary(400, True) == [123])
check("cap off: disk usage is fresh every tick, so marks re-verify regardless",
      summary(None, False) == [123])

# ── 4. The preview pairs the season report by run identity ─────────────────
import app as A  # noqa: E402

A.output_dir = lambda: OUT
enriched = []
A._enrich_run_report = lambda report, tv=None: enriched.append(tv) or report
A._read_run_report = lambda: {"mode": "cleanup", "message": "done", "ts": time.time(),
                              "run_started_at": 1000.5, "issues": []}
A.load_config = lambda: {"OUTPUT_DIR": str(OUT), "NOTIFY_ON_RUN_SUMMARY": True,
                         "NOTIFY_ENABLED": True, "NOTIFY_NTFY_SERVER": "https://x",
                         "NOTIFY_NTFY_TOPIC": "t", "RUN_MODE": "headroom",
                         "MONITOR_DIRS": ["/library/x"]}
A.app.config["TESTING"] = True
client = A.app.test_client()


def preview_tv(last_pass):
    enriched.clear()
    A._tv_cleanup_state = lambda: {"last_pass": last_pass} if last_pass else {}
    client.post("/api/debug/notify-preview", json={}, headers={"X-MediaReducer": "1"})
    return enriched[0] if enriched else "never-enriched"


tv = {"run_started_at": 1000.5, "deleted_seasons": [{"title": "S"}], "seasons_seen": 9}
check("the same run's season report rides the preview", preview_tv(tv) is tv)
check("a fresh-but-foreign season report is refused",
      preview_tv({**tv, "run_started_at": 999.25}) is None)
check("an unstamped season report is refused, not assumed",
      preview_tv({"deleted_seasons": [{"title": "S"}]}) is None)
check("no season report at all previews movie-only", preview_tv(None) is None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
