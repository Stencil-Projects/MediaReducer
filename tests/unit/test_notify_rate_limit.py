"""The notification rate limit: at most one delivery per destination per
_MIN_SEND_INTERVAL_S, with no path around it.

What actually paces MediaReducer is upstream of the floor: the tick that
produces the between-run alerts runs every 15 minutes and each alert type is
latched to fire once per state change. The floor is the backstop for the
pathological case — a caller in a loop — and it is a property of this module
rather than an assumption about how often the app happens to fire: one floor,
enforced at the single choke point that alerts and the Config page's test
button both go through.

The interesting cases are the ones where a naive limiter does damage:

  * two alert types legitimately firing on the SAME scheduler tick (a low-space
    warning while new marks appear) — the second must be delayed, never dropped;
  * a restart in the middle of a window — an in-memory limiter forgets, and the
    next alert goes out inside a window that was still open;
  * the test button, which a user can click as fast as they like.

apprise is never imported: _send_now is stubbed, so this is hermetic and the
assertions are about WHEN a send is attempted, not whether a webhook answered.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import notify

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


SENT = []
notify._send_now = lambda urls, title, body: (
    SENT.append({"urls": list(urls), "title": title, "body": body}) or
    (True, f"sent to {len(urls)} destination(s)"))

# dispatch_error is the simplest real entry point: one toggle, a body that is
# passed straight through, so the assertions below are about the limiter.
CFG = {"NOTIFY_ENABLED": True, "NOTIFY_ON_ERROR": True,
       "NOTIFY_CUSTOM_URLS": ["json://one.example.com"]}
TWO = {"NOTIFY_ENABLED": True, "NOTIFY_ON_ERROR": True,
       "NOTIFY_CUSTOM_URLS": ["json://one.example.com", "json://two.example.com"]}


def reset(state=None):
    """A fresh limiter, as if the process just started."""
    with notify._rate_lock:
        notify._last_sent.clear()
        notify._held.clear()
        notify._held_urls.clear()
        if notify._flush_timer is not None:
            notify._flush_timer.cancel()
        notify._flush_timer = None
        notify._flush_at = None
        notify._state_io = None
        notify._state_loaded = True
    SENT.clear()


def rewind(seconds):
    """Age every recorded send by `seconds`, standing in for the clock moving
    forward without making the test sleep out a real window."""
    with notify._rate_lock:
        for k in notify._last_sent:
            notify._last_sent[k] -= seconds


# ── The floor itself ────────────────────────────────────────────────────────
reset()
check("there is a floor at all — the choke point is only useful if it bites",
      isinstance(notify._MIN_SEND_INTERVAL_S, (int, float))
      and notify._MIN_SEND_INTERVAL_S > 0)

# The floor's wording is derived from the floor. It used to hardcode minutes,
# which reads correctly at 15 minutes and says "every 0 minutes" at 10 seconds —
# the kind of thing that only breaks when someone changes the constant, which is
# exactly when nobody is looking at this string.
for _secs, _want in [(1, "1 second"), (10, "10 seconds"), (59, "59 seconds"),
                     (60, "1 minute"), (90, "2 minutes"), (900, "15 minutes")]:
    check(f"a {_secs}s interval reads as '{_want}'", notify._interval_words(_secs) == _want)
_msg = notify.cooldown_message(min(1, notify._MIN_SEND_INTERVAL_S))
# \b, not a substring: "0 second" is inside "10 seconds", and a plain `in`
# check here reported a bug that wasn't there.
check("the cooldown message never quotes a zero interval",
      not re.search(r"\b0 (second|minute)", _msg))
check("...and states the floor actually in force",
      notify._interval_words(notify._MIN_SEND_INTERVAL_S) in _msg)

notify.dispatch_error(CFG, "first")
notify.dispatch_error(CFG, "second")
check("a second alert inside the window is not delivered", len(SENT) == 1)
check("...and the first one is the one that went", SENT[0]["body"] == "first")

rewind(notify._MIN_SEND_INTERVAL_S + 1)
notify.dispatch_error(CFG, "third")
check("once the window passes, sending resumes", len(SENT) == 2)

# ── Held, not dropped ───────────────────────────────────────────────────────
# A low-space warning and a marked-changes alert can fall on the SAME tick. A
# limiter that dropped the second would lose the one the user most needs.
reset()
notify.dispatch_error(CFG, "low space")
held_ok, held_detail = notify.dispatch_error(CFG, "new marks")
check("the blocked alert is queued, not discarded",
      len(SENT) == 1 and sum(len(v) for v in notify._held.values()) == 1)
# A hold is ACCEPTED delivery, and the caller must be told so: the app's run
# dispatcher baselines the marked queue only for a summary that was sent, and a
# held summary reported as a failure left the marks to be announced AGAIN by
# the next tick's marked-changes alert — every held daily summary doubled up.
check("...and reports as delivered, since it will be",
      held_ok is True and "held" in held_detail)

rewind(notify._MIN_SEND_INTERVAL_S + 1)
notify._flush_held()
check("it is delivered once the window opens", len(SENT) == 2)
check("...carrying the content it was holding", "new marks" in SENT[1]["body"])
# A held alert arrives as what it would have been. Explaining the delay spends a
# line of a phone notification on MediaReducer's own bookkeeping, and made a
# merged message read as an apology for arriving.
_late = SENT[1]["body"].lower()
check("...and says nothing about a rate limit or a delay",
      not any(w in _late for w in ("rate limit", "delayed", "delay so", "held")))

# Several held for one destination arrive as ONE message, not a burst that
# would blow the limit the moment the window opened.
reset()
notify.dispatch_error(CFG, "alpha")
for text in ("beta", "gamma", "delta"):
    notify.dispatch_error(CFG, text)
rewind(notify._MIN_SEND_INTERVAL_S + 1)
notify._flush_held()
check("everything held for a destination flushes as a single message",
      len(SENT) == 2 and all(w in SENT[1]["body"] for w in ("beta", "gamma", "delta")))
check("...and the merged message explains no delay either",
      not any(w in SENT[1]["body"].lower() for w in ("rate limit", "delayed")))

# The queue cannot grow without bound.
reset()
for i in range(notify._MAX_HELD_PER_DESTINATION * 3):
    notify.dispatch_error(CFG, f"msg{i}")
check("the held queue is capped",
      all(len(v) <= notify._MAX_HELD_PER_DESTINATION for v in notify._held.values()))

# ── Per destination, not global ─────────────────────────────────────────────
# The limits being respected are per webhook/chat, so a quiet destination must
# not be starved by a busy one.
reset()
notify.dispatch_error(CFG, "to one only")
SENT.clear()
notify.dispatch_error(TWO, "to both")
check("a destination still inside its window is held",
      len(SENT) == 1 and len(SENT[0]["urls"]) == 1)
check("...while the fresh destination is sent to immediately",
      SENT[0]["urls"] == ["json://two.example.com"])

# ── The test button ─────────────────────────────────────────────────────────
reset()
first_ok, _ = notify.send_test(CFG)
second_ok, detail = notify.send_test(CFG)
check("the test button sends once", first_ok and len(SENT) == 1)
check("...and is refused inside the window, like any alert", not second_ok)
check("...told how long to wait, not that delivery failed",
      "Try again in about" in detail and "failed" not in detail.lower())
check("a refused test is NOT queued for later (it would arrive after they gave up)",
      not any(notify._held.values()))

# A test consumes the slot: it is a real message to a real service, so an
# automatic alert right after one must wait its turn.
reset()
notify.send_test(CFG)
SENT.clear()
notify.dispatch_error(CFG, "alert after a test")
check("an alert right after a test is held, not sent", len(SENT) == 0)

# ── Surviving a restart ─────────────────────────────────────────────────────
# In memory alone, a container bounce clears every cooldown.
reset()
store = {}
notify.set_rate_state_store(lambda: store.get("v"), lambda v: store.__setitem__("v", v))
notify.dispatch_error(CFG, "before the restart")
check("the send is recorded to the injected store", bool(store.get("v")))
check("...without writing any URL or token",
      not any("example.com" in str(k) for k in (store.get("v") or {})))

# Simulate the restart: process state gone, the store survives.
with notify._rate_lock:
    notify._last_sent.clear()
    notify._held.clear()
    notify._held_urls.clear()
    notify._state_loaded = False
SENT.clear()
notify.dispatch_error(CFG, "right after the restart")
check("a restart does not reopen a window that was still closed", len(SENT) == 0)

# A stamp in the future (clock skew, or a hand-edited store) must not strand a
# destination until the skew passes.
with notify._rate_lock:
    notify._last_sent.clear()
    notify._held.clear()
    notify._state_loaded = False
store["v"] = {k: time.time() + 86400 for k in (store.get("v") or {"x": 0})}
SENT.clear()
notify.dispatch_error(CFG, "after a clock jump")
check("a future timestamp is ignored rather than locking the destination out",
      len(SENT) == 1)

reset()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
