"""Two things happening at once, made to actually happen at once.

The app is multi-threaded by construction: Flask serves requests concurrently,
APScheduler runs two independent timer jobs, and background workers finish
whenever they finish. Several locks exist to make that safe, and a mutation
audit removed each in turn — the scheduler tick's serializer and the config
read-modify-write lock could both be deleted outright with nothing failing.

Sequential tests cannot find that. A lock only does anything when two things
want the same thing at the same instant, so a test that calls A then B proves
the code works when the lock was never needed. The interleaving here is FORCED
rather than hoped for: a stand-in inside the critical section blocks the first
caller and signals the test, the second caller is released into the window, and
what happens next is the assertion. Under the real lock the second caller
cannot enter, so the wait times out on a bound and the test still finishes.

  • The scheduler tick is fired by TWO separate APScheduler jobs — the
    15-minute interval and the one-shot at the daily run time — so
    max_instances=1 does not stop them overlapping when the interval lands in
    the same second as the slot. The loser must DROP its tick, not queue
    behind the winner: queueing runs the daily work twice.
  • Two config writers that read, modify and write must not interleave. The
    pair named in save_config's own comment is real — the welcome popup's
    mark-seen POST against the Config page's onboarding flag — and losing
    either one re-opens a dialog the user already dismissed.
"""
import json
import os
import sys
import tempfile
import threading
import time
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


# ── The scheduler tick: the loser drops, it does not queue ─────────────────
entered = []
release = threading.Event()
inside = threading.Event()
_real_body = A._scheduled_tick_body


def _blocking_body():
    entered.append(time.monotonic())
    inside.set()
    release.wait(5)


A._scheduled_tick_body = _blocking_body
first = threading.Thread(target=A._scheduled_tick, daemon=True)
first.start()
check("the first tick is running its work", inside.wait(5))

_t0 = time.monotonic()
A._scheduled_tick()                       # the overlapping timer fires
_elapsed = time.monotonic() - _t0
check("an overlapping tick returns immediately instead of queueing",
      _elapsed < 1.0, f"{_elapsed:.2f}s")
check("...without running the work a second time", len(entered) == 1, entered)

release.set()
first.join(5)
check("the first tick still finished its own work", len(entered) == 1, entered)

# ...and the lock is released afterwards, or every later tick is a no-op and
# the scheduler goes quiet for good.
del entered[:]
release.clear()
inside.clear()
A._scheduled_tick_body = lambda: entered.append("later")
A._scheduled_tick()
check("a tick after the previous one finished still runs", entered == ["later"], entered)
A._scheduled_tick_body = _real_body


# ── Two config writers that read-modify-write ─────────────────────────────
# save_config's comment names this exact pair as one that happens in practice.
A.CONFIG_PATH.write_text(json.dumps({"OUTPUT_DIR": str(OUT)}))
with A._config_memo_lock:
    A._config_memo["key"] = object()

FLAG_A = A.WELCOME_GUIDE_SEEN_KEY
FLAG_B = A.CONNECTION_ONBOARDING_SEEN_KEY

held = threading.Event()
second_done = threading.Event()
_real_load = A.load_config
_first = threading.Event()


def _load_holding_the_window():
    """The first caller through stops here — still inside the critical section,
    having read but not yet written — and waits for the second to finish. Under
    the real lock the second never gets that far, so this falls through on its
    bound instead."""
    cfg = _real_load()
    if not _first.is_set():
        _first.set()
        held.set()
        second_done.wait(1.5)
    return cfg


A.load_config = _load_holding_the_window
try:
    t1 = threading.Thread(target=A._mark_welcome_guide_seen, daemon=True)
    t1.start()
    check("the first writer has read the config and not yet written", held.wait(5))

    def _second():
        A._mark_connection_onboarding_seen()
        second_done.set()

    t2 = threading.Thread(target=_second, daemon=True)
    t2.start()
    t1.join(10)
    t2.join(10)
finally:
    A.load_config = _real_load

_saved = json.loads(A.CONFIG_PATH.read_text())
check("the welcome flag survived", _saved.get(FLAG_A) is True, _saved.get(FLAG_A))
check("...and so did the onboarding flag written against it",
      _saved.get(FLAG_B) is True, _saved.get(FLAG_B))
check("...and OUTPUT_DIR, which neither of them touched, is still there",
      _saved.get("OUTPUT_DIR") == str(OUT), _saved.get("OUTPUT_DIR"))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
