"""Who gets the run slot when two background jobs want it at once.

Three things share _summary_active — the quiet Summary, its synchronous twin,
and the config-save reconcile — and a real run is refused while any of them
holds it. That makes the handoff at the END of each job load-bearing: whoever
clears the flag must also honour whatever queued behind it, and must not take
the slot back in the instant a caller was about to start a run.

Both failures here are silent. A lost Summary leaves the dashboard's disk
numbers stale with the UI having said "queued"; a reconcile that grabs the slot
between the scheduled tick's storage refresh and its deletion pass costs that
tick its Redline emergency, with the next chance 15 minutes later.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

# Nothing here may spawn a subprocess or a thread: every job is stubbed down to
# a marker so the handoff itself is what gets measured.
A._run_summary_subprocess = lambda *a, **k: (True, "ok")
A._run_reconcile_subprocess = lambda *a, **k: True
A._invalidate_request_store_cache = lambda *a, **k: None
A._pause_scheduler_for_run = lambda *a, **k: None
A._restart_schedule_clock = lambda *a, **k: None
A._sync_scheduler_mode_with_db = lambda *a, **k: None
A.library_stats = lambda *a, **k: {"library_gb": 300.0}

summaries = []
A.run_summary = lambda *a, **k: (summaries.append(1), (True, "stub"))[1]

def reset(*, queued_summary=False, queued_reconcile=False, run_active=False):
    A._summary_active = False
    A._summary_queued = queued_summary
    A._reconcile_queued = (False, "save") if queued_reconcile else None
    A._run_active = run_active
    summaries.clear()

# ── The reconcile worker owes the same handshake as the other two owners ─────
# run_summary() already answered its caller "Summary refresh queued", so the
# refresh has to happen; and a flag left latched fires one spurious engine
# subprocess after some unrelated later Summary.
reset(queued_summary=True)
A._reconcile_worker(refetch=False, trigger="save")
check("a Summary queued behind a reconcile actually runs", summaries == [1], summaries)
check("...and the queued flag is cleared, not latched", A._summary_queued is False)
check("...and the reconcile released the slot", A._summary_active is False)

# The other half of the same handshake: a queue abandoned because a real run
# started is dropped rather than left latched — the run refreshes the same stats.
reset(queued_summary=True, run_active=True)
A._reconcile_worker(refetch=False, trigger="save")
check("a Summary queued behind a reconcile is dropped when a run took over",
      summaries == [] and A._summary_queued is False)

# ── A queued reconcile must not take the slot out from under a run ───────────
# run_summary_sync's caller (the scheduled tick) starts a deletion pass the
# moment it returns. Launching the reconcile in the finally takes _summary_active
# synchronously, and run_script's guard then refuses that pass outright.
reset(queued_reconcile=True)
A.run_summary_sync(defer_reconcile=True)
check("a deferred reconcile stays queued for the caller to release",
      A._reconcile_queued is not None and A._summary_active is False)

reset(queued_reconcile=True)
A.run_summary_sync()
check("...and without the deferral it launches as before, on the way out",
      A._reconcile_queued is None)

# ── Handing a queued reconcile back during a run must not lose it ────────────
# The tick releases it right after launching, so a live run is the normal state
# here; run_reconcile re-queues rather than dropping the request.
reset(queued_reconcile=True, run_active=True)
A._maybe_launch_queued_reconcile()
check("a reconcile released while a run is active stays queued",
      A._reconcile_queued is not None, A._reconcile_queued)

reset()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
