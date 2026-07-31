"""Force stop: the escape hatch for a run the graceful Stop can't end.

Stop sends SIGTERM, which the engine turns into a clean unwind. The app's run
worker then sits in proc.wait(), so `_run_active` — and with it every locked
setting on the Configuration page — clears only when that process actually
exits. An engine that doesn't exit therefore locks the UI for as long as it
lives, with no way out of the web interface.

What is asserted here is mostly about honesty, because the failure modes differ:

  * an engine that IGNORES SIGTERM is exactly what SIGKILL is for, and the run
    state has to come back;
  * an engine SIGKILL can't touch either — blocked in an uninterruptible syscall,
    the hung-NFS case — must be reported as such and must NOT be papered over by
    force-clearing the run state. A lie there is worse than the lock: the wedged
    process can wake up later and start deleting alongside a second run.

No engine is launched. The processes here are stand-ins whose whole job is to
respond, or not, the way each of those cases would.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG",
                      str(Path(tempfile.mkdtemp(prefix="mr-forcestop.")) / "config.json"))
import app

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


app.FORCE_STOP_WAIT_S = 1.0   # only an unkillable process waits this out


class FakeProc:
    """A run process that can be told how to behave. `deaf` models the case that
    matters: a signal is delivered and nothing happens."""
    def __init__(self, *, deaf=False, already_exited=False):
        self.deaf = deaf
        self.returncode = 0 if already_exited else None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if not self.deaf:
            self.returncode = -9


def install(proc):
    """Put the app into "a run is active" with this process behind it, and run
    the same worker the real launcher does — reaping the child is what clears
    the run state, so a test that skipped it would prove nothing about unlocking."""
    with app._run_lock:
        app._run_active = True
        app._run_process = proc
        app._run_stop_requested.clear()

    def worker():
        while proc.poll() is None:
            time.sleep(0.02)
        with app._run_lock:
            app._run_active = False
            app._run_process = None
            app._run_stop_requested.clear()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def clear():
    with app._run_lock:
        app._run_active = False
        app._run_process = None
        app._run_stop_requested.clear()


# ── Nothing running ─────────────────────────────────────────────────────────
clear()
res_ok, res_msg = app.force_stop_script()
check("with no run active it declines rather than pretending",
      res_ok is False and "No active run" in res_msg)

# ── The case it exists for: an engine deaf to SIGTERM ───────────────────────
proc = FakeProc()
worker = install(proc)
check("the settings are locked while it runs", app._run_active is True)
res_ok, res_msg = app.force_stop_script()
worker.join(timeout=2)
check("force stop kills it", res_ok is True and proc.killed)
check("...and the run state clears, so the settings unlock",
      app._run_active is False and app._run_process is None)

# ── An engine SIGKILL can't touch (uninterruptible syscall / hung mount) ────
clear()
proc = FakeProc(deaf=True)
install(proc)
t0 = time.time()
res_ok, res_msg = app.force_stop_script()
waited = time.time() - t0
check("a kill that doesn't take is reported as a failure, not a success",
      res_ok is False and proc.killed)
check("...naming the cause the user can act on",
      "system call" in res_msg and "network share" in res_msg)
check("...after waiting for it rather than assuming either way",
      app.FORCE_STOP_WAIT_S <= waited < app.FORCE_STOP_WAIT_S + 1.5)
# The important one. Force-clearing here would unlock the settings and let a
# second run start beside a process that may still be deleting.
check("...and the run state is NOT force-cleared behind a still-live process",
      app._run_active is True)

# ── The process died on its own between the click and the handler ───────────
clear()
proc = FakeProc(already_exited=True)
with app._run_lock:
    app._run_active = True
    app._run_process = proc
res_ok, res_msg = app.force_stop_script()
check("an already-finished run reports success without signalling a dead pid",
      res_ok is True and not proc.killed and "already ended" in res_msg)

# ── Claimed but not yet launched ────────────────────────────────────────────
# The launcher marks the run active before Popen returns; a force stop landing
# in that window has no process, and the stop request it sets makes the
# launcher bail on its own.
clear()
with app._run_lock:
    app._run_active = True
    app._run_process = None
res_ok, res_msg = app.force_stop_script()
check("a run claimed but not yet launched is handled without a crash",
      res_ok is True and app._run_stop_requested.is_set())

clear()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
