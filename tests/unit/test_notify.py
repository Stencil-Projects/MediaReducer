"""notify.py: Apprise URL assembly from the friendly service fields, and the
alert-type gating that decides whether (and what) to send for a run. Pure logic
only — _deliver is stubbed so no apprise import or network is needed, which also
keeps the test hermetic on CI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import notify

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ── URL assembly ────────────────────────────────────────────────────────────
urls = notify.apprise_urls({
    "NOTIFY_DISCORD_WEBHOOK": "https://discord.com/api/webhooks/42/tok-EN_x",
    "NOTIFY_TELEGRAM_BOT_TOKEN": "111:AAA", "NOTIFY_TELEGRAM_CHAT_ID": "999",
    "NOTIFY_SLACK_WEBHOOK": "https://hooks.slack.com/services/T01/B02/ccc",
    "NOTIFY_PUSHOVER_USER_KEY": "uuu", "NOTIFY_PUSHOVER_TOKEN": "ttt",
    "NOTIFY_NTFY_TOPIC": "topic",
    "NOTIFY_GOTIFY_SERVER": "https://gotify.example.com", "NOTIFY_GOTIFY_TOKEN": "Gtok",
    "NOTIFY_CUSTOM_URLS": ["mailto://a:b@x.com", "  ", "matrix://tok@host"],
})
check("discord webhook → discord://", "discord://42/tok-EN_x" in urls)
check("telegram → tgram://", "tgram://111:AAA/999" in urls)
check("slack webhook → slack://", "slack://T01/B02/ccc" in urls)
check("pushover → pover://", "pover://uuu@ttt" in urls)
check("ntfy topic (no server) → ntfy.sh", "ntfy://topic" in urls)
check("gotify https → gotifys://", "gotifys://gotify.example.com/Gtok" in urls)
check("custom URLs pass through", "mailto://a:b@x.com" in urls and "matrix://tok@host" in urls)
check("blank custom line dropped", "" not in urls and "  " not in urls)

# ntfy self-hosted: http stays insecure, bare host / https → secure
check("ntfy http server → ntfy:// insecure",
      notify._ntfy_urls({"NOTIFY_NTFY_TOPIC": "t", "NOTIFY_NTFY_SERVER": "http://192.168.1.5:8080"})
      == ["ntfy://192.168.1.5:8080/t"])
check("ntfy bare host → ntfys:// secure",
      notify._ntfy_urls({"NOTIFY_NTFY_TOPIC": "t", "NOTIFY_NTFY_SERVER": "ntfy.example.com"})
      == ["ntfys://ntfy.example.com/t"])

# Partial fields never emit a broken URL.
check("telegram needs both token+chat", notify.apprise_urls({"NOTIFY_TELEGRAM_BOT_TOKEN": "x"}) == [])
check("gotify needs both server+token", notify.apprise_urls({"NOTIFY_GOTIFY_TOKEN": "x"}) == [])
check("no fields → no destinations", notify.apprise_urls({}) == [])
check("has_destinations reflects config",
      notify.has_destinations({"NOTIFY_NTFY_TOPIC": "t"}) and not notify.has_destinations({}))


# ── Message building / gating ───────────────────────────────────────────────
# Armed (Automatic Cleanup): the messages speak in deletions. The Monitor Only
# variants of the same messages are covered in their own section further down.
ALL_ON = {"NOTIFY_ON_RUN_SUMMARY": True, "NOTIFY_ON_MARKED_CHANGES": True,
          "NOTIFY_ON_LOW_SPACE": True, "NOTIFY_SHOW_MOVIES": True, "NOTIFY_ON_ERROR": True,
          "RUN_MODE": "headroom"}
MONITOR = dict(ALL_ON, RUN_MODE="paused")

# Scheduled daily Simulate: plan block + newly-marked names, "daily summary" title.
SIM = {
    "mode": "simulate", "eligible_count": 400, "marked_count": 12, "marked_total": 12,
    "next_event_on": "2026-08-02",
    "marked_items": [{"title": "A", "delete_on": "2026-08-02"},
                     {"title": "B", "delete_on": "2026-08-02"}],
    "deleted_items": [], "deleted_count": 0,
}
t, b = notify.build_run_message(ALL_ON, SIM)
check("simulate title is daily summary", t == "MediaReducer — daily summary")
# One labeled fact per line, not a sentence of them joined by "·" — a phone
# renders that as an unreadable run-on the moment there are three.
check("plan block lists eligible/marked/next date as labeled rows",
      "Eligible in deletion order: 400" in b and "Marked for deletion: 12" in b
      and "Next deletion: 2026-08-02" in b)
check("simulate has no cleanup block", "Removed" not in b and "Nothing was removed" not in b)
check("newly-marked names share one dated header",
      "Newly marked — delete on 2026-08-02:" in b and "• A" in b and "• B" in b)
check("no scores anywhere", "score" not in b.lower())

# Cleanup run: plan block + cleanup block + both name lists.
CLEAN = {
    "mode": "cleanup", "eligible_count": 388, "marked_total": 10, "marked_count": 1,
    "deleted_count": 5, "bytes_freed": 38 * 1_000_000_000, "next_event_ripe": True,
    "deleted_items": [{"title": "X", "size": 8 * 1_000_000_000}],
    "marked_items": [{"title": "N1", "delete_on": "2026-08-03"}],
}
t, b = notify.build_run_message(ALL_ON, CLEAN)
check("cleanup title", t == "MediaReducer — cleanup")
check("cleanup block reports removed+freed", "Removed 5 movie(s) — freed 38.0 GB." in b)

# Seasons ride the SAME message inside the run's own numbers — one removed
# line with the bytes summed, seasons folded into the marked total — never a
# separate block, and the words "TV pass" appear nowhere.
_, tvb = notify.build_run_message(ALL_ON, dict(CLEAN, seasons={
    "marked_new": 2, "held_by_delay": 1, "deleted_seasons": 1,
    "freed_bytes": 12_000_000_000, "marked_total": 3}))
check("one removed line covers movies and seasons with the bytes summed",
      "Removed 5 movie(s) and 1 season(s) — freed 50.0 GB." in tvb)
check("season marks fold into the one marked total (10 movies + 3 seasons)",
      "Marked for deletion: 13" in tvb)
check("no TV-pass nomenclature anywhere", "TV pass" not in tvb and "tv pass" not in tvb.lower())
_, tva = notify.build_run_message(ALL_ON, dict(CLEAN, seasons={
    "aborted": "Jellyfin's TV inventory did not answer"}))
check("a season-side failure states the fact without pass branding",
      "Season deletions skipped this run — Jellyfin's TV inventory did not "
      "answer; the movie side covers the full target." in tva
      and "TV pass" not in tva)
# The run-wide space-safety refusal is the thresholds module's message — a
# safety-held season report (aborted None) must add NO season line at all.
_, tvs = notify.build_run_message(ALL_ON, dict(CLEAN, seasons={
    "aborted": None, "blocked_by_safety": True, "marked_total": 0}))
check("the safety-held season side adds no season line",
      "season" not in tvs.lower())
_, tvsk = notify.build_run_message(ALL_ON, dict(CLEAN, seasons={
    "skipped": 2, "marked_total": 0}))
check("skipped seasons point at the log, factually",
      "2 season(s) skipped — see the log." in tvsk and "TV pass" not in tvsk)
check("no seasons key = no season wording", "season" not in b.lower())
check("ripe marks say next daily run", "Next deletion: at the next daily run" in b)
check("deleted list has name+size", "Deleted:" in b and "• X (8.0 GB)" in b)

# Names off → no lists, blocks stay.
t, b = notify.build_run_message({**ALL_ON, "NOTIFY_SHOW_MOVIES": False}, CLEAN)
check("names OFF → no lists", "• X" not in b and "Deleted:" not in b and "Removed 5" in b)

# Run summaries off → no run message at all.
check("run-summary OFF → no message",
      notify.build_run_message({**ALL_ON, "NOTIFY_ON_RUN_SUMMARY": False}, CLEAN) is None)

# Redline-only plan wording.
t, b = notify.build_run_message(ALL_ON, dict(SIM, redline_only=True, marked_items=[]))
check("redline-only plan wording", "Deletes when: free space hits the Redline floor" in b)

# Debug dry run keeps its message as the cleanup block, title flags dry run.
dbg = {"mode": "debug_cleanup", "debug": True, "eligible_count": 50, "marked_count": 2,
       "marked_total": 2, "deleted_count": 0,
       "marked_items": [{"title": "X", "delete_on": "2026-09-01"}],
       "message": "Debug Cleanup (dry run) — an automatic run would mark 2 movie(s)."}
t, b = notify.build_run_message(ALL_ON, dbg)
check("debug title marks dry run", "dry run" in t.lower())
check("debug body carries dry-run message + Would mark list",
      "Debug Cleanup (dry run)" in b and "Would mark for deletion" in b)

# The issue list rides the run message when enabled — by CATEGORY and COUNT,
# not a bare "there were errors" that sends the reader to the log to find out
# what and how many.
t, b = notify.build_run_message(ALL_ON, dict(CLEAN, issues=[
    {"category": "identity_mismatch", "detail": "Bob (2020)"},
    {"category": "identity_mismatch", "detail": "Ann (2021)"},
    {"category": "cap_unreachable", "detail": "still 900 GB over"},
]))
check("errored run lists each category with its count",
      "Identity mismatch: 2 movies skipped" in b
      and "Cap out of reach" in b or "Library cap out of reach" in b)
check("...and says how many KINDS went wrong, errors before warnings",
      "Completed with 1 kind of error and 1 warning" in b
      and b.index("Identity mismatch") < b.index("Library cap out of reach"))
# An older report with no issue list must still not read as a clean run.
_, _b_old = notify.build_run_message(ALL_ON, dict(CLEAN, completed_with_errors=True))
check("a report with no issue list still reports errors", "errors" in _b_old.lower())

# Marked-change alert (15-minute check).
mc = notify.build_marked_change_message(
    ALL_ON, new_items=[{"title": "M1", "delete_on": "2026-08-04"},
                       {"title": "M2", "delete_on": "2026-08-04"}], marked_total=13)
check("marked-change message built", mc is not None)
check("marked-change body counts + date",
      "2 more movie(s)" in mc[1] and "13 marked in total" in mc[1] and "2026-08-04" in mc[1])
check("marked-change lists names", "• M1" in mc[1] and "• M2" in mc[1])
check("marked-change toggle off → None",
      notify.build_marked_change_message({**ALL_ON, "NOTIFY_ON_MARKED_CHANGES": False},
                                         new_items=[{"title": "M"}], marked_total=1) is None)
check("marked-change empty → None",
      notify.build_marked_change_message(ALL_ON, new_items=[], marked_total=5) is None)

# Mixed delete dates among new marks: the headline names the soonest, the list
# stays clean titles (header date only), sorted soonest-first with undated last.
mc = notify.build_marked_change_message(
    ALL_ON, new_items=[{"title": "Late", "delete_on": "2026-08-09"},
                       {"title": "Soon", "delete_on": "2026-08-03"},
                       {"title": "Undated", "delete_on": ""}], marked_total=3)
check("mixed dates headline names the soonest", "delete starting 2026-08-03" in mc[1])
check("newly-marked lines are titles only (no per-line dates)",
      "• Soon\n" in mc[1] + "\n" and "— 2026-08-09" not in mc[1])
check("mixed dates sort soonest first, undated last",
      mc[1].index("• Soon") < mc[1].index("• Late") < mc[1].index("• Undated"))

# Run summary: existing marks (predating the run) get their own dated section,
# soonest at the top — only run reports carry it, never the 15-minute alert.
t, b = notify.build_run_message(ALL_ON, dict(
    CLEAN,
    marked_items=[{"title": "Fresh", "delete_on": "2026-09-05"}],
    existing_marked_items=[{"title": "E-late", "delete_on": "2026-09-04"},
                           {"title": "E-soon", "delete_on": "2026-09-01"}]))
check("Newly marked keeps the header date + bare titles",
      "Newly marked — delete on 2026-09-05:" in b and "• Fresh\n" in b + "\n")
check("Already marked section lists per-movie dates",
      "Already marked:" in b and "• E-soon — 2026-09-01" in b and "• E-late — 2026-09-04" in b)
check("Already marked sorts soonest first", b.index("E-soon") < b.index("E-late"))
check("Already marked comes after Newly marked", b.index("Newly marked") < b.index("Already marked"))
_, b = notify.build_run_message({**ALL_ON, "NOTIFY_SHOW_MOVIES": False}, dict(
    CLEAN, existing_marked_items=[{"title": "E", "delete_on": "2026-09-01"}]))
check("show-movies off hides Already marked too", "Already marked" not in b)
_, b = notify.build_run_message(ALL_ON, dict(CLEAN, existing_marked_items=[]))
check("no existing marks → no Already marked section", "Already marked" not in b)

# Low-space warning.
ls = notify.build_low_space_message(ALL_ON, free_gb=212.4, redline_gb=200, margin_gb=25,
                                    items=[{"title": "F1", "size": 3 * 1_000_000_000}])
check("low-space message built", ls is not None and "low space" in ls[0].lower())
check("low-space body has numbers", "212.4" in ls[1] and "25" in ls[1] and "200" in ls[1])
check("low-space lists first in line", "First in line:" in ls[1] and "• F1 (3.0 GB)" in ls[1])
check("low-space toggle off → None",
      notify.build_low_space_message({**ALL_ON, "NOTIFY_ON_LOW_SPACE": False},
                                     free_gb=1, redline_gb=1, margin_gb=1) is None)
# Movie names is a separate switch from whether the alert fires, and this is
# the alert whose privacy gate nothing checked. The run summary, the
# marked-change alert and the abort message all have this check; the low-space
# warning did not, so it was the one place a title could still leave the box
# with the toggle off.
_ls_private = notify.build_low_space_message(
    {**ALL_ON, "NOTIFY_SHOW_MOVIES": False}, free_gb=212.4, redline_gb=200,
    margin_gb=25, items=[{"title": "F1", "size": 3 * 1_000_000_000}])
check("low-space names no film with Movie names off",
      "F1" not in _ls_private[1] and "First in line" not in _ls_private[1])
check("...while the numbers that make it worth sending are still there",
      "212.4" in _ls_private[1] and "200" in _ls_private[1])

# ── Mode-aware wording ───────────────────────────────────────────────────────
# Every alert states whether deletions can happen in the CURRENT mode, and the
# delay dates are listed either way — a date means "gets deleted then" while
# armed and "becomes eligible then" in Monitor Only.
_ARMED_NOTE = "Automatic Cleanup is on"
_MONITOR_NOTE = "Monitor Only — nothing is deleted automatically"

# 1. Run summary carries the mode line and mode-appropriate plan wording.
_sum_armed = notify.build_run_message(ALL_ON, SIM)[1]
_sum_mon = notify.build_run_message(MONITOR, SIM)[1]
check("armed summary states deletions happen",
      _ARMED_NOTE in _sum_armed and _MONITOR_NOTE not in _sum_armed)
check("Monitor Only summary states nothing is deleted",
      _MONITOR_NOTE in _sum_mon and _ARMED_NOTE not in _sum_mon)
check("armed summary promises the next deletion date",
      "Next deletion: 2026-08-02" in _sum_armed)
check("Monitor Only summary gives the same date as a due date, not a deletion",
      "Next deletion: 2026-08-02 (due, not deleted)" in _sum_mon)
check("both summaries still list the marked movies and their dates",
      "• A" in _sum_armed and "• A" in _sum_mon
      and "2026-08-02" in _sum_armed and "2026-08-02" in _sum_mon)
check("Monitor Only newly-marked header says due, not delete",
      "Newly marked — due 2026-08-02:" in _sum_mon)
_ripe_mon = notify.build_run_message(MONITOR, CLEAN)[1]
check("Monitor Only ripe marks don't claim a daily-run deletion",
      "already due" in _ripe_mon and "next deletion at the next daily run" not in _ripe_mon)

# 2. Marked-between-runs alert (same input, both modes).
_mc_items = [{"title": "M1", "delete_on": "2026-08-04"}]
_mc_armed = notify.build_marked_change_message(
    ALL_ON, new_items=_mc_items, marked_total=2)[1]
_mc_mon = notify.build_marked_change_message(
    MONITOR, new_items=_mc_items, marked_total=2)[1]
check("armed marked alert says the movies delete",
      "They delete on 2026-08-04" in _mc_armed and _ARMED_NOTE in _mc_armed)
check("Monitor Only marked alert says they become eligible",
      "They become eligible on 2026-08-04" in _mc_mon and _MONITOR_NOTE in _mc_mon)

# 3. Low-space warning.
_ls_mon = notify.build_low_space_message(MONITOR, free_gb=212.4, redline_gb=200, margin_gb=25)
check("armed low-space promises the emergency cleanup",
      "emergency cleanup deletes from the marked queue immediately" in ls[1])
check("Monitor Only low-space says nothing is deleted",
      "Monitor Only — nothing is deleted automatically" in _ls_mon[1])

# 4. Every alert type is mode-aware — none may be silent about which mode it is.
for _label, _body in (("run summary", _sum_mon), ("marked alert", _mc_mon),
                      ("low-space", _ls_mon[1])):
    check(f"{_label} names the mode it is reporting for",
          "Monitor Only" in _body)

# Storage block: dashboard-style numbers + armed limits ride every run summary.
st_rep = dict(CLEAN, storage={"free_gb": 412.3, "total_gb": 1000.0, "used_gb": 587.7,
                              "library_gb": 3214.6, "headroom_gb": 500,
                              "redline_gb": 200.0, "max_library_gb": 3200.0})
_, st_body = notify.build_run_message(ALL_ON, st_rep)
# Measured sizes carry one decimal at every magnitude — the library figure is
# the one that shows it: rounding 3214.6 to whole GB would read 3,215.
check("storage line shows free/total/library, each to one decimal",
      "Storage: 412.3 GB free of 1,000.0 GB · library 3,214.6 GB" in st_body)
# Limits are settings, so they stay whole even though they arrive as floats.
check("limits line lists the armed thresholds",
      "Limits: Headroom 500 GB · Redline 200 GB · Library cap 3,200 GB" in st_body)
st_off = dict(CLEAN, storage={"free_gb": 412.3, "total_gb": 1000.0,
                              "headroom_gb": None, "redline_gb": None, "max_library_gb": None})
_, st_body2 = notify.build_run_message(ALL_ON, st_off)
check("no armed limits → no Limits line", "Limits:" not in st_body2 and "Storage:" in st_body2)
check("no storage in report → no storage block",
      "Storage:" not in notify.build_run_message(ALL_ON, CLEAN)[1])

# Long lists are bounded by the character budget with a "…and N more" tail.
big = dict(CLEAN, deleted_count=500, marked_count=0, marked_items=[],
           deleted_items=[{"title": f"Movie Number {i} With A Longish Title",
                           "size": 1_000_000_000} for i in range(500)])
_, big_body = notify.build_run_message(ALL_ON, big)
check("long list is trimmed with an overflow note", "more" in big_body and "…and" in big_body)
check("trimmed body stays within a safe character bound", len(big_body) < 2200)


# ── Dispatch gating (stub _deliver; never hits apprise/network) ──────────────
sent = []
def _fake_deliver(urls, title, body):
    sent.append((urls, title, body))
    return True, "stubbed"
notify._deliver = _fake_deliver

DEST = {"NOTIFY_CUSTOM_URLS": ["json://127.0.0.1:1"]}

sent.clear()
res = notify.dispatch_run_report({**DEST, **ALL_ON, "NOTIFY_ENABLED": False}, CLEAN)
check("master OFF → no send", res == (False, "disabled") and not sent)

sent.clear()
notify.dispatch_run_report({**DEST, **ALL_ON, "NOTIFY_ENABLED": True}, CLEAN)
check("master ON → delivers once", len(sent) == 1)

sent.clear()
notify.dispatch_marked_changes({**DEST, **ALL_ON, "NOTIFY_ENABLED": True},
                               new_items=[{"title": "M"}], marked_total=1)
check("marked-change dispatch delivers", len(sent) == 1)
notify.dispatch_low_space({**DEST, **ALL_ON, "NOTIFY_ENABLED": True},
                          free_gb=10, redline_gb=8, margin_gb=5)
check("low-space dispatch delivers", len(sent) == 2)

sent.clear()
r = notify.dispatch_error({**DEST, "NOTIFY_ENABLED": True, "NOTIFY_ON_ERROR": False}, "boom")
check("error dispatch honors NOTIFY_ON_ERROR", r == (False, "disabled") and not sent)
notify.dispatch_error({**DEST, "NOTIFY_ENABLED": True, "NOTIFY_ON_ERROR": True}, "boom")
check("error dispatch sends when enabled", len(sent) == 1 and "boom" in sent[0][2])

# ── A failed run reads like the dashboard's run panel ────────────────────────
# Same three things in the same order: the engine's sentence behind a ×, the
# stage to quote, then anything the run had recorded before it died. "A run
# failed, check the log" would be the exact trip the alert exists to save.
sent.clear()
notify.dispatch_error(
    {**DEST, "NOTIFY_ENABLED": True, "NOTIFY_ON_ERROR": True},
    "Tautulli stopped answering while reading movie details. Nothing was deleted.",
    stage="SCORING",
    issues=[{"category": "identity_mismatch", "count": 43, "detail": "Some Movie (2011)"}])
_body = sent[-1][2]
check("failure alert leads with × and the engine's sentence",
      _body.startswith("× Tautulli stopped answering"))
check("failure alert names the stage", "Failed during SCORING" in _body)
check("failure alert carries issues recorded before the abort",
      "Identity mismatch: 43 movies skipped" in _body)
check("failure alert titles itself a failure", sent[-1][1] == "MediaReducer — run failed")

sent.clear()
notify.dispatch_error({**DEST, "NOTIFY_ENABLED": True, "NOTIFY_ON_ERROR": True})
check("a failure with no message still says something",
      sent[-1][2].startswith("× The run stopped"))

# Movie names OFF must mean no film leaves the box. The issue block's "e.g."
# example is a title or a full library path, so it follows that opt-in — the
# counts and categories, which are the point of the block, still go out.
_LEAKY = [{"category": "delete_failed", "detail": "/library/Movies/Private (2019)/f.mkv: denied"},
          {"category": "delete_failed", "detail": "/library/Movies/Other (2020)/o.mkv: denied"}]
_, _off = notify.build_error_message({}, message="Jellyfin stopped answering.", issues=_LEAKY)
_, _on = notify.build_error_message({"NOTIFY_SHOW_MOVIES": True},
                                    message="Jellyfin stopped answering.", issues=_LEAKY)
# Scope: the ISSUE BLOCK only — `message` here carries no film, so this says
# nothing about the alert's first line. That line is the engine's sentence and
# some of those DO name a film; test_abort_name_disclosure covers it, and this
# check quietly passing while that line leaked is why it exists separately.
check("the issue block's example is hidden while Movie names is off",
      "Private" not in _off and "Deletion failed: 2 movies" in _off)
check("...and shown when it is on", "Private" not in _off and "Private" in _on)
_run = dict(CLEAN, issues=_LEAKY)
check("the run summary follows the same opt-in",
      "Private" not in notify.build_run_message({**ALL_ON, "NOTIFY_SHOW_MOVIES": False}, _run)[1]
      and "Private" in notify.build_run_message(ALL_ON, _run)[1])

# ── Fresh-install defaults ───────────────────────────────────────────────────
# Nothing is delivered until the master switch goes on and a service is set up.
# These defaults decide what arrives the moment it does: the three alert types
# that report on the library are pre-selected, while the flags that add detail
# (movie names, error flagging) or widen scope (Monitor Only) start off, so the
# first alerts a user sees are short and say only what happened.
import json as _json
from pathlib import Path as _Path
_defaults = _json.loads((_Path(__file__).resolve().parents[2] / "default_config.json").read_text())
for _k in ("NOTIFY_ON_RUN_SUMMARY", "NOTIFY_ON_MARKED_CHANGES", "NOTIFY_ON_LOW_SPACE"):
    check(f"{_k} is pre-selected on a fresh install",
          notify.TOGGLE_DEFAULTS[_k] is True and _defaults[_k] is True)
for _k in ("NOTIFY_ENABLED", "NOTIFY_SHOW_MOVIES", "NOTIFY_ON_ERROR",
           "NOTIFY_IN_MONITOR_ONLY"):
    check(f"{_k} is off on a fresh install",
          notify.TOGGLE_DEFAULTS[_k] is False and _defaults[_k] is False)
check("every notification toggle default matches default_config.json",
      all(_defaults.get(_k) is _v for _k, _v in notify.TOGGLE_DEFAULTS.items()))

# The master switch alone gates delivery: with it off, the pre-selected alert
# types build nothing, so a fresh install is silent until the user opts in.
check("a fresh install delivers nothing while the master switch is off",
      notify.dispatch_run_report(_defaults, SIM) == (False, "disabled"))

# Turned on, the three pre-selected alerts build — without movie names, since
# that flag is off. This is the shape of a new user's first notification.
_fresh = dict(_defaults, NOTIFY_ENABLED=True, NOTIFY_CUSTOM_URLS=["json://x"])
_run = notify.build_run_message(_fresh, SIM)
check("fresh install + notifications on builds the run summary",
      _run is not None and "eligible" in _run[1])
check("...and it carries no movie names until Movie names is checked",
      _run is not None and "Newly marked" not in _run[1] and "Deleted:" not in _run[1])
_marked = notify.build_marked_change_message(
    _fresh, new_items=[{"title": "Some Movie", "delete_on": "2026-08-09"}], marked_total=1)
check("fresh install + notifications on builds the marked-between-runs alert",
      _marked is not None and "marked 1 more movie" in _marked[1])
check("...naming no movies either, but still quoting the delay date",
      _marked is not None and "Some Movie" not in _marked[1] and "2026-08-09" in _marked[1])
check("fresh install + notifications on builds the low-space warning",
      notify.build_low_space_message(
          _fresh, free_gb=210, redline_gb=200, margin_gb=25) is not None)
check("checking Movie names is what adds the names",
      "Some Movie" in notify.build_marked_change_message(
          dict(_fresh, NOTIFY_SHOW_MOVIES=True),
          new_items=[{"title": "Some Movie", "delete_on": "2026-08-09"}],
          marked_total=1)[1])
check("notify.flag reads the shared defaults for absent keys",
      notify.flag({}, "NOTIFY_ON_RUN_SUMMARY") is True
      and notify.flag({}, "NOTIFY_SHOW_MOVIES") is False
      and notify.flag({"NOTIFY_ON_RUN_SUMMARY": False}, "NOTIFY_ON_RUN_SUMMARY") is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
