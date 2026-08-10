"""The rule that stops a Library Size Cap from emptying a library.

A cap below the safety floor is the one setting that can clear most of a
library on purpose and be working as configured: the run keeps deleting until
the library fits the cap, and the cap says it should fit in less than
MAX_HEADROOM_PCT% of what is actually there. The app refuses to arm on it, but
a library that GREW afterwards crosses the floor with the cap untouched — so
the engine checks again against today's size and fails closed.

Nothing exercised it. Every scenario in the table runs MAX_HEADROOM_PCT at
100, which makes the floor `library × 0%` = 0, and no cap is below zero — so
the branch was dead across the whole suite and deleting it changed nothing.

Two things are pinned here. Whether a run is refused, since that is the
protection. And that Simulate is NOT refused: previewing what a setting would
do is how anyone discovers the setting is wrong, so a preview that refuses to
render is a worse failure than the one being prevented.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def refused(*, library_gb=100.0, cap=50.0, pct=15, breached=True,
            sim=False, debug_cleanup=False):
    """Put one configuration to the floor check and report the verdict, what
    it logged, and whether it told the dashboard to stop."""
    logs, progress = [], []
    saved = (E.MAX_HEADROOM_PCT, E.MAX_LIBRARY_GB, E.log, E.log_blank, E.emit_progress)
    E.MAX_HEADROOM_PCT = pct
    E.MAX_LIBRARY_GB = cap
    E.log = lambda msg="", *a, **k: logs.append(str(msg))
    E.log_blank = lambda *a, **k: None
    E.emit_progress = lambda **kw: progress.append(kw)
    try:
        stop = E._cap_floor_refusal(library_cap_hit=breached, library_gb=library_gb,
                                    _is_sim=sim, _is_debug_cleanup=debug_cleanup)
    finally:
        (E.MAX_HEADROOM_PCT, E.MAX_LIBRARY_GB, E.log, E.log_blank,
         E.emit_progress) = saved
    return stop, logs, progress


# ── The floor itself ─────────────────────────────────────────────────────────
# 100 GB library at 15% means a Cleanup may take 15 GB, so the cap may not ask
# for less than 85 GB.
stop, logs, prog = refused(library_gb=100.0, cap=50.0, pct=15)
check("a cap under the floor refuses a live run", stop is True)
check("...saying so as an ABORT", any(x.startswith("ABORT:") for x in logs), logs)
check("...naming the floor it computed", any("85" in x for x in logs), logs)
check("...and telling the dashboard the run is over, not still running",
      prog and prog[-1].get("status") == "error", prog)

check("a cap exactly ON the floor is allowed",
      refused(library_gb=100.0, cap=85.0, pct=15)[0] is False)
check("...and one above it", refused(library_gb=100.0, cap=90.0, pct=15)[0] is False)
check("a cap one tenth of a GB under the floor is still under it",
      refused(library_gb=100.0, cap=84.9, pct=15)[0] is True)

# The percentage is what a Cleanup may TAKE, so raising it lowers the floor.
check("raising the safety percentage lowers the floor",
      refused(library_gb=100.0, cap=50.0, pct=60)[0] is False)
check("...and lowering it raises the floor",
      refused(library_gb=100.0, cap=95.0, pct=2)[0] is True)

# The floor is compared UNROUNDED, and this is the case that forced it: a
# 0.3 GB library at 90% has a floor of 0.03 GB, which rounds for display to
# 0.0 — and every cap clears zero. Rounding first waved the deletion through.
check("a floor that rounds to 0.0 still stops a smaller cap",
      refused(library_gb=0.3, cap=0.01, pct=90)[0] is True)
# The same mistake one decimal place up, where the rounding is ordinary rather
# than a collapse: a 0.34 floor displayed as 0.3 would admit a 0.32 cap.
check("...and a floor rounded down by a hair does not admit a cap under it",
      refused(library_gb=0.4, cap=0.32, pct=15)[0] is True)

# ── Who it applies to ────────────────────────────────────────────────────────
for label, kw in (("Simulate", {"sim": True}),
                  ("Debug Cleanup", {"debug_cleanup": True})):
    stop, logs, prog = refused(library_gb=100.0, cap=50.0, pct=15, **kw)
    check(f"{label} previews past the floor rather than refusing", stop is False)
    check(f"...warning instead of aborting",
          any(x.startswith("NOTE:") for x in logs)
          and not any(x.startswith("ABORT:") for x in logs), logs)
    check(f"...and saying a real Cleanup would refuse",
          any("would refuse to run" in x for x in logs), logs)

# ── When it does not apply at all ────────────────────────────────────────────
# No breach, nothing to delete down to, so the floor is not in play.
check("an unbreached cap is not checked against the floor",
      refused(library_gb=100.0, cap=50.0, pct=15, breached=False)[0] is False)

# A percentage outside 0–100 is not a safety setting to reason from. The app
# validates it, so reaching here means it is already broken; the floor check
# stands aside rather than computing a nonsense limit from it.
for pct in (0, -5, 150, None, "lots"):
    check(f"a {pct!r} safety percentage is not used as a floor",
          refused(library_gb=100.0, cap=50.0, pct=pct)[0] is False, pct)

# ── The numbers are written for a person ─────────────────────────────────────
# The floor is COMPARED unrounded, which is the whole point above — but the
# message is read by someone deciding what to change, and the raw float turned
# a one-line abort into "2.45366784 GB is below ... (8.17889 GB library ...)".
# gb_text keeps small values from collapsing to 0.0 while spending no more
# digits than the size warrants.
for value, want in ((0.0, "0.0"), (150.0, "150.0"), (1234.567, "1234.6"),
                    (8.17889, "8.18"), (2.45366784, "2.45"),
                    (0.03, "0.03"), (0.001, "0.001")):
    check(f"{value} reads as {want}", E.gb_text(value) == want, E.gb_text(value))
check("a figure too small for two decimals still is not zero",
      E.gb_text(0.0004) not in ("0.0", "0.00"), E.gb_text(0.0004))
check("something that is not a number is passed through, not crashed on",
      E.gb_text(None) == "None" and E.gb_text("n/a") == "n/a")

_, logs, _ = refused(library_gb=8.17889, cap=2.45366784, pct=15)
msg = next(x for x in logs if x.startswith("ABORT:"))
check("the abort message spends no more than four decimals on a figure",
      not re.search(r"\d\.\d{5,}", msg), msg)
check("...and still names the cap, the library and the floor",
      "2.45 GB" in msg and "8.18 GB" in msg and "6.95 GB" in msg, msg)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
