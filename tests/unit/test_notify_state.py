"""App-side notification state machines (notify.py's message layer is covered by
test_notify.py; this locks the stateful logic around it):

  • marked-change dedup — one alert per queue change, never a repeat: first
    observation baselines silently, identical state stays silent, a new mark
    alerts once naming ONLY the new movie, a deletion-delay change (re-dated
    marks) earns one fresh alert, and drops-only changes re-baseline silently;
  • the post-run re-baseline stores the marked MAP with the signature, so the
    next diff names exactly the new movies (a sig-only baseline would misreport
    the whole marked set as new);
  • mode muting — Paused, and Monitor Only without its opt-in, send nothing at
    all and leave the baseline untouched so the muted period is reported once
    when it ends;
  • low-space warn-once — warn on entering the margin zone, no repeat inside it,
    re-arm on recovery (even while the alert is toggled off), nothing below the
    floor itself, and the "First in line" list falls back to the queue front
    when no entry reads as marked (redline-only above the floor)."""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-notify-state.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A
import notify

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

sent: list = []
notify._deliver = lambda urls, title, body: (sent.append((title, body)), (True, "stub"))[1]

CFG = {"RUN_MODE": "headroom", "NOTIFY_ENABLED": True, "NOTIFY_ON_MARKED_CHANGES": True,
       "NOTIFY_ON_LOW_SPACE": True, "NOTIFY_SHOW_MOVIES": True, "NOTIFY_LOW_SPACE_GB": 25,
       "REDLINE_GB": 200.0, "OUTPUT_DIR": _OUT, "NOTIFY_CUSTOM_URLS": ["json://sink"]}
A.load_config = lambda: dict(CFG)

# Synthetic queue — the state machines read it through pending_deletion_entries.
ENTRIES: list = []
A.pending_deletion_entries = lambda cfg=None, with_lines=True: [dict(e) for e in ENTRIES]

def entry(path, title, *, marked, delete_on=None, size=1_000_000_000):
    return {"path": path, "title": title, "marked_at": 1.0 if marked else None,
            "marked": marked, "delete_on": delete_on, "size_bytes": size}


# ── Marked-change dedup ──────────────────────────────────────────────────────
ENTRIES[:] = [entry("/l/a.mkv", "Movie A", marked=True, delete_on="2026-08-01"),
              entry("/l/b.mkv", "Movie B", marked=True, delete_on="2026-08-01"),
              entry("/l/c.mkv", "Movie C", marked=False)]
A._store_notify_meta("notify_marked_state", None)

A._check_marked_change_notification()
check("first observation baselines silently", not sent)
A._check_marked_change_notification()
check("identical state stays silent", not sent)

ENTRIES.append(entry("/l/d.mkv", "Movie D", marked=True, delete_on="2026-08-02"))
A._check_marked_change_notification()
check("a new mark alerts once", len(sent) == 1)
if sent:
    t, b = sent[-1]
    check("alert names ONLY the new movie", "Movie D" in b and "Movie A" not in b)
    check("alert carries the marked total", "3 marked in total" in b)
A._check_marked_change_notification()
check("no repeat for the same state", len(sent) == 1)

# Deletion-delay change: same paths, new dates → one fresh alert.
sent.clear()
for e in ENTRIES:
    if e["marked_at"] is not None:
        e["delete_on"] = "2026-08-09"
A._check_marked_change_notification()
check("re-dated marks (delay change) alert once", len(sent) == 1)
A._check_marked_change_notification()
check("re-dated state then stays silent", len(sent) == 1)

# Drops-only: a mark leaves the queue → re-baseline silently.
sent.clear()
ENTRIES[:] = [e for e in ENTRIES if e["path"] != "/l/d.mkv"]
A._check_marked_change_notification()
check("drops-only change is silent", not sent)

# A mode that mutes notifications sends NOTHING and advances NO baseline, so
# what happened while it was muted is reported once when it is unmuted rather
# than swallowed. Fully-Paused is always muted; Monitor Only is muted unless
# "Alert in Monitor Only" is ticked, and that opt-in covers this alert too —
# turning notifications off for a mode that never deletes used to still deliver
# "a space check marked 34 more movies" a few minutes later.
sent.clear()
ENTRIES.append(entry("/l/e.mkv", "Movie E", marked=True, delete_on="2026-08-03"))
_saved_mode = CFG["RUN_MODE"]
CFG["RUN_MODE"] = "off"
A._check_marked_change_notification()
check("fully-paused (off) never alerts", not sent)
CFG["RUN_MODE"] = "paused"; CFG["NOTIFY_IN_MONITOR_ONLY"] = False
A._check_marked_change_notification()
check("Monitor Only without the opt-in never alerts", not sent)
CFG["NOTIFY_IN_MONITOR_ONLY"] = True
A._check_marked_change_notification()
check("...and turning the opt-in on reports what the muted period skipped",
      len(sent) == 1 and "Movie E" in sent[-1][1])
check("Monitor Only wording notes nothing deletes on its own",
      "Monitor Only — nothing is deleted automatically" in sent[-1][1])
CFG["RUN_MODE"] = _saved_mode; CFG["NOTIFY_IN_MONITOR_ONLY"] = False


# ── Post-run re-baseline stores the marked MAP ───────────────────────────────
A._update_marked_baseline()
state = A._read_notify_meta("notify_marked_state")
check("run re-baseline stores the marked map with the sig",
      isinstance(state, dict) and isinstance(state.get("marked"), dict)
      and set(state["marked"]) == {e["path"] for e in ENTRIES if e["marked_at"] is not None})
sent.clear()
A._check_marked_change_notification()
check("post-run state reads as already-announced", not sent)
ENTRIES.append(entry("/l/f.mkv", "Movie F", marked=True, delete_on="2026-08-04"))
A._check_marked_change_notification()
check("next change after a run names only the new movie",
      len(sent) == 1 and "Movie F" in sent[-1][1] and "Movie E" not in sent[-1][1])


# ── Low-space warn-once + re-arm ─────────────────────────────────────────────
sent.clear()
A._store_notify_meta("notify_low_space", None)
A._check_low_space_notification(dict(CFG), {"free_gb": 300.0})
check("well above the zone: no warning", not sent)
A._check_low_space_notification(dict(CFG), {"free_gb": 210.0})
check("entering the zone warns once", len(sent) == 1)
A._check_low_space_notification(dict(CFG), {"free_gb": 205.0})
check("still in the zone: no repeat", len(sent) == 1)
A._check_low_space_notification(dict(CFG), {"free_gb": 150.0})
check("below the floor itself: no extra warning", len(sent) == 1)

# Recovery re-arms even while the alert is toggled OFF (state maintenance must
# not be gated on the toggle, or re-enabling finds a stale "warned" flag).
off = dict(CFG, NOTIFY_ON_LOW_SPACE=False)
A._check_low_space_notification(off, {"free_gb": 300.0})   # recovers, toggle off
A._check_low_space_notification(dict(CFG), {"free_gb": 212.0})
check("recovery while toggled off still re-arms the warning", len(sent) == 2)

# Monitor Only mutes this warning along with everything else unless the opt-in
# is ticked; with it ticked the floor is worth watching even though nothing
# deletes, so the message carries the no-deletion wording.
sent.clear()
A._store_notify_meta("notify_low_space", None)
A._check_low_space_notification(dict(CFG, RUN_MODE="paused"), {"free_gb": 210.0})
check("Monitor Only without the opt-in sends no low-space warning", not sent)
_mon = dict(CFG, RUN_MODE="paused", NOTIFY_IN_MONITOR_ONLY=True)
A._check_low_space_notification(_mon, {"free_gb": 210.0})
check("Monitor Only WITH the opt-in warns",
      len(sent) == 1 and "Monitor Only — nothing is deleted automatically" in sent[-1][1])
A._check_low_space_notification(_mon, {"free_gb": 205.0})
check("Monitor Only warn-once dedup still holds", len(sent) == 1)

# The movie list falls back to the queue FRONT when nothing reads as marked
# (redline-only mode above the floor has a zero deficit).
sent.clear()
A._store_notify_meta("notify_low_space", None)
ENTRIES[:] = [entry("/l/x.mkv", "Front Movie", marked=False),
              entry("/l/y.mkv", "Second Movie", marked=False)]
A._check_low_space_notification(dict(CFG), {"free_gb": 210.0})
check("no marked entries → queue front listed as first in line",
      len(sent) == 1 and "First in line:" in sent[-1][1] and "Front Movie" in sent[-1][1])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
