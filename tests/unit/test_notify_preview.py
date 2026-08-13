"""/api/debug/notify-preview renders what the notifications would say — and
consumes nothing.

The button exists for one situation: Debug mode (or Paused/Monitor Only)
mutes real notifications, so there is no way to SEE what the daily summary or
a pending alert looks like without triggering a run and arming a mode that
deletes files. The preview must therefore be a pure read — the one way it
could do damage is by advancing the marked-changes baseline or the low-space
warned flag, which would silently swallow the real alert the user is owed
when they leave debug mode. The last check here is that exact end-to-end
promise: preview the pending alert, then unmute, and the real dispatch still
fires with the same movies in it.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
OUT = Path(_tmpout.config())
import app as A  # noqa: E402
import notify  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


LIB = OUT / "library" / "movies"
LIB.mkdir(parents=True, exist_ok=True)
CFG = {"OUTPUT_DIR": str(OUT), "MONITOR_DIRS": [str(LIB)],
       # Monitor Only without the opt-in: notifications are MUTED — the state
       # the preview exists for. Debug mode keeps the scheduler out of
       # Automatic Cleanup, so this is also the debug-mode shape.
       "RUN_MODE": "paused", "NOTIFY_IN_MONITOR_ONLY": False,
       "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k",
       "NOTIFY_ENABLED": True, "NOTIFY_ON_RUN_SUMMARY": True,
       "NOTIFY_ON_MARKED_CHANGES": True, "NOTIFY_ON_LOW_SPACE": True,
       "NOTIFY_SHOW_MOVIES": True, "NOTIFY_LOW_SPACE_GB": 25,
       "NOTIFY_PUSHOVER_USER_KEY": "ukey", "NOTIFY_PUSHOVER_TOKEN": "tkey",
       "REDLINE_GB": 500, "HEADROOM_GB": 0, "REDLINE_ONLY_MODE": True}
A.CONFIG_PATH.write_text(json.dumps(CFG))
with A._config_memo_lock:
    A._config_memo["key"] = object()

DISK = {"used_gb": 490.0, "total_gb": 1000.0, "free_gb": 510.0, "pct_used": 49.0}
A.disk_stats = lambda: dict(DISK)
A.library_stats = lambda: {"library_gb": 100.0}
A.pending_delete_forecast = lambda cfg=None: {"count": 0, "marked": 0, "ripe": 0,
                                              "event_on": None, "event_count": 0,
                                              "event_bytes": 0}
A.pending_deletion_entries = lambda cfg=None, **k: [
    {"title": "Queue Front", "size_bytes": 4_000_000_000, "marked": True}]

# The queue as the app would see it: one mark that postdates the stored
# baseline, i.e. an un-announced change waiting for the next unmuted tick.
MARKED_NOW = {"/library/movies/beta.mkv": {"title": "Movie Beta", "delete_on": "2026-08-20"}}
A._marked_queue_state = lambda cfg: ("sig-2", dict(MARKED_NOW))
A._store_notify_meta("notify_marked_state", {"sig": "sig-1", "marked": {}})
A._store_notify_meta("notify_low_space", {"warned": False})

# The preview must never dispatch anything. These spies stay armed for every
# call below; the real-dispatch check at the end swaps its own recorder in.
_dispatch_calls = []
for _name in ("dispatch_run_report", "dispatch_marked_changes",
              "dispatch_low_space", "dispatch_error", "send_test"):
    setattr(notify, _name,
            (lambda _n: lambda *a, **k: _dispatch_calls.append(_n) or (True, ""))(_name))

A.app.config["TESTING"] = True
client = A.app.test_client()
HDRS = {"X-MediaReducer": "1"}


def preview(**over):
    """The preview for a saved config — writing it first, because the endpoint
    judges the file and nothing else."""
    if over:
        A.CONFIG_PATH.write_text(json.dumps({**CFG, **over}))
        with A._config_memo_lock:
            A._config_memo["key"] = object()
    r = client.post("/api/debug/notify-preview", json={}, headers=HDRS)
    d = r.get_json()
    assert r.status_code == 200 and d.get("ok"), (r.status_code, d)
    return d["text"]


# ── No run yet: the summary section says so instead of vanishing ──────────
text = preview()
check("without a run report, the preview explains where one comes from",
      "no completed run report" in text, text[:120])

# ── With a report: the summary is rebuilt under the current settings ──────
A.run_report_path().write_text(json.dumps({
    "mode": "simulate", "eligible_count": 12, "marked_count": 1,
    "marked_items": [{"title": "Movie Alpha", "delete_on": "2026-08-19",
                      "path": "/library/movies/alpha.mkv"}],
    "redline_only": False, "ts": time.time()}))
text = preview()
check("the run summary is rebuilt from the last report", "RUN SUMMARY" in text)
check("...and names the run's movies with the privacy toggle on",
      "Movie Alpha" in text)
check("...and says why it would not deliver right now (muted mode)",
      "Would NOT deliver" in text and "Monitor Only" in text, text[:400])
text_private = preview(NOTIFY_SHOW_MOVIES=False)
check("the SAVED settings are what gets previewed: privacy off drops titles",
      "Movie Alpha" not in text_private and "RUN SUMMARY" in text_private)
preview(**CFG)   # back to the shared state the checks below assume
text = preview()

# ── The pending marked-changes alert, only while one is pending ───────────
check("an un-announced mark shows as a pending alert, with a divider",
      "PENDING MARKED-CHANGES" in text and "Movie Beta" in text
      and "────" in text, text[-400:])
_low = preview(NOTIFY_ON_MARKED_CHANGES=False)
preview(**CFG)
check("...rendered even with its toggle off, labeled as suppressed",
      "PENDING MARKED-CHANGES" in _low and "toggled off" in _low)
A._store_notify_meta("notify_marked_state", {"sig": "sig-2", "marked": dict(MARKED_NOW)})
check("with the baseline caught up, the pending section disappears",
      "PENDING MARKED-CHANGES" not in preview())
A._store_notify_meta("notify_marked_state", {"sig": "sig-1", "marked": {}})

# ── The low-space warning, only while inside the margin ───────────────────
check("free space inside the margin shows the warning",
      "LOW-SPACE WARNING" in text and "Queue Front" in text)
DISK["free_gb"] = 900.0
check("free space clear of the margin drops the section",
      "LOW-SPACE WARNING" not in preview())
DISK["free_gb"] = 510.0

# ── Strictly read-only ────────────────────────────────────────────────────
before = {k: A._read_notify_meta(k) for k in
          ("notify_marked_state", "notify_low_space", "notify_rate_state")}
report_before = A.run_report_path().read_bytes()
_dispatch_calls.clear()
preview()
preview(NOTIFY_SHOW_MOVIES=False)
preview(**CFG)
after = {k: A._read_notify_meta(k) for k in
         ("notify_marked_state", "notify_low_space", "notify_rate_state")}
check("previewing changes no notification state", before == after,
      {k: (before[k], after[k]) for k in before if before[k] != after[k]})
check("...and rewrites no run report", A.run_report_path().read_bytes() == report_before)
check("...and dispatches nothing", not _dispatch_calls, _dispatch_calls)

# ── The promise itself: a previewed alert still fires for real ────────────
# Unmute (tick the Monitor Only opt-in), run the real 15-minute check, and
# the alert the preview showed goes out with the same movies — the preview
# did not consume its trigger.
A.CONFIG_PATH.write_text(json.dumps({**CFG, "NOTIFY_IN_MONITOR_ONLY": True}))
with A._config_memo_lock:
    A._config_memo["key"] = object()
sent_with = []
notify.dispatch_marked_changes = (
    lambda cfg, **kw: sent_with.append(kw) or (True, ""))
A._check_marked_change_notification()
check("after previewing, the real check still announces the same marks",
      sent_with and [i["title"] for i in sent_with[0]["new_items"]] == ["Movie Beta"],
      sent_with)
check("...and only the real send advances the baseline",
      (A._read_notify_meta("notify_marked_state") or {}).get("sig") == "sig-2")

# ── The Debug button's own availability ──────────────────────────────────
# The preview's three sections are each conditional, so with none of them
# applicable the popup would open on nothing. The button ghosts on this
# instead — and because none of the three depend on the notification fields,
# the verdict rides the status poll rather than a per-keystroke round trip.
A.CONFIG_PATH.write_text(json.dumps(CFG))
with A._config_memo_lock:
    A._config_memo["key"] = object()
cfg_now = A.load_config()
check("with a run report on disk, the preview is available",
      A._notify_preview_reason(cfg_now) == "", A._notify_preview_reason(cfg_now))

A.run_report_path().unlink()
check("...and a pending marked-changes alert keeps it available on its own",
      A._notify_preview_reason(cfg_now) == "")

A._store_notify_meta("notify_marked_state", {"sig": "sig-2", "marked": dict(MARKED_NOW)})
check("...as does low space on its own",
      A._notify_preview_reason(cfg_now) == "")

DISK["free_gb"] = 900.0
reason = A._notify_preview_reason(cfg_now)
check("with nothing to show, the button ghosts", reason != "", reason)
check("...and the reason names the Simulate to run",
      "Simulate" in reason, reason)
d = client.get("/api/status").get_json()
check("/api/status carries the same verdict",
      d.get("notify_preview_reason") == reason, d.get("notify_preview_reason"))
DISK["free_gb"] = 510.0
A._store_notify_meta("notify_marked_state", {"sig": "sig-1", "marked": {}})

# ── The test button's availability: the SAVED destinations ────────────────
# Judged on what is on disk, not on what is typed — like every other control
# state on the page — and by notify.py's own assembler rather than a rule
# copied into the browser: "configured" is not field-presence, a pasted
# Discord webhook counts only if it parses.
def saved_reason(**over):
    A.CONFIG_PATH.write_text(json.dumps({**CFG, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    return A._notify_test_reason(A.load_config())


check("a saved Pushover pair counts as a destination", saved_reason() == "")
blank = {"NOTIFY_PUSHOVER_USER_KEY": "", "NOTIFY_PUSHOVER_TOKEN": ""}
reason = saved_reason(**blank)
check("with every destination cleared, the test button ghosts", reason != "", reason)
check("...and the reason says to add one and save",
      "save" in reason.lower(), reason)
check("half a Telegram pair is not a destination",
      saved_reason(**blank, NOTIFY_TELEGRAM_BOT_TOKEN="123:ABC") != "")
check("...and both halves are",
      saved_reason(**blank, NOTIFY_TELEGRAM_BOT_TOKEN="123:ABC",
                   NOTIFY_TELEGRAM_CHAT_ID="42") == "")
check("an unparseable Discord webhook is not a destination",
      saved_reason(**blank, NOTIFY_DISCORD_WEBHOOK="not-a-webhook") != "")
check("...and a real one is",
      saved_reason(**blank,
                   NOTIFY_DISCORD_WEBHOOK="https://discord.com/api/webhooks/123/abc-DEF") == "")
check("the verdict agrees with notify.has_destinations itself",
      (saved_reason(**blank, NOTIFY_NTFY_TOPIC="alerts") == "")
      is notify.has_destinations(A.load_config()))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
