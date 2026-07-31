"""Run-log stages: each section says how long it took, in the right place.

A stage is closed by the NEXT one opening, or by the run ending — there is no
per-stage end call to forget. That design has one failure mode, and it bit
during development: a banner emitted with log_raw instead of log_stage does not
close the stage before it, so that stage's timing prints AFTER the section it
actually preceded. The summary banners are exactly that shape — they report work
already finished — so they route through log_stage with timed=False: close the
previous stage, print the banner, claim no duration of their own.

Also pinned here: the dashboard's section deep links key off the `====== X ======`
banners (app.py's _LOG_SECTION_RES). The new `------ X finished in Y ------`
footer sits right beside them and must not be mistaken for one.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


_OUT = Path(tempfile.mkdtemp(prefix="mr-stages."))
E.OUTPUT_DIR = _OUT
E.LOGFILE = _OUT / "lastrun.log"


def fresh():
    """Start a log with no stage open."""
    E.LOGFILE.write_text("", encoding="utf-8")
    E._STAGE_TITLE = None
    E._STAGE_STARTED = None


def lines():
    return [ln.rstrip("\n") for ln in E.LOGFILE.read_text(encoding="utf-8").splitlines()]


def index_of(pred, ls):
    for i, ln in enumerate(ls):
        if pred(ln):
            return i
    return -1


# ── Every work stage reports its own duration, once ─────────────────────────
fresh()
E.log_stage("SCAN")
E.log("scanning things")
E.log_stage("ELIGIBLE CANDIDATES")
E.log("sorting things")
E.close_stage()
ls = lines()

scan_foot = [ln for ln in ls if ln.startswith("------ SCAN finished in")]
check("a stage gets exactly one duration line", len(scan_foot) == 1, scan_foot)
check("...and it reads as a duration", re.search(r"finished in \d", scan_foot[0] or ""), scan_foot)

i_scan = index_of(lambda l: l.startswith("====== SCAN"), ls)
i_foot = index_of(lambda l: l.startswith("------ SCAN finished"), ls)
i_next = index_of(lambda l: l.startswith("====== ELIGIBLE"), ls)
check("the duration lands after its own banner and before the next one",
      -1 < i_scan < i_foot < i_next, f"{i_scan} {i_foot} {i_next}")
check("the last stage closes too",
      any(ln.startswith("------ ELIGIBLE CANDIDATES finished") for ln in ls), ls[-3:])

# The blank lines the sections are separated by.
check("a blank line sits either side of the duration",
      ls[i_foot - 1] == "" and ls[i_foot + 1] == "", repr(ls[i_foot - 1:i_foot + 2]))

# ── Closing twice must not print twice ──────────────────────────────────────
fresh()
E.log_stage("SCAN")
E.close_stage()
E.close_stage()
check("closing an already-closed stage is a no-op",
      len([ln for ln in lines() if "finished in" in ln]) == 1,
      [ln for ln in lines() if "finished in" in ln])

# ── A summary banner closes the stage before it, and claims no time ─────────
# This is the ordering bug the timed=False flag exists to prevent.
fresh()
E.log_stage("SIMULATION")
E.log("simulating")
E.log_stage("DRY RUN SUMMARY [LIBRARY CAP]", timed=False)
E.log("Trigger: LIBRARY CAP")
E.close_stage()
ls = lines()
i_sim_foot = index_of(lambda l: l.startswith("------ SIMULATION finished"), ls)
i_summary = index_of(lambda l: l.startswith("====== DRY RUN SUMMARY"), ls)
check("the stage before a summary is closed BEFORE the summary banner",
      -1 < i_sim_foot < i_summary, f"foot={i_sim_foot} summary={i_summary}")
check("a summary claims no duration of its own",
      not any("DRY RUN SUMMARY" in ln and "finished in" in ln for ln in ls),
      [ln for ln in ls if "finished in" in ln])

# ── Durations read sensibly at every magnitude ──────────────────────────────
for secs, want in [(0, "0.0s"), (0.44, "0.4s"), (9.9, "9.9s"), (42.4, "42s"),
                   (90, "1m 30s"), (3600, "1h 0m"), (7830, "2h 10m")]:
    check(f"{secs}s reads as {want}", E._fmt_elapsed(secs) == want, E._fmt_elapsed(secs))
check("a nonsense duration does not crash the log", E._fmt_elapsed(None) == "?")

# ── Quiet steps report their duration and nothing else ──────────────────────
# Reading the library and querying protected collections do not log what they
# do — there would be thousands of lines — so the one thing they must produce
# is how long they took.
fresh()
_real_time = E.time.time
_clock = [1000.0]
E.time.time = lambda: _clock[0]
try:
    with E.timed_step("Library read from the media server"):
        _clock[0] += 4.6
    ls = lines()
    hits = [ln for ln in ls if "Library read from the media server" in ln]
    check("a quiet step logs exactly one line", len(hits) == 1, hits)
    check("...carrying its measured duration", "in 4.6s" in (hits[0] if hits else ""), hits)

    # A step that fails has nothing useful to say about its duration — the abort
    # message is the story, and a "finished in" line under it would read as if
    # the work had completed.
    fresh()
    try:
        with E.timed_step("Library read from the media server"):
            _clock[0] += 2.0
            raise RuntimeError("Plex unreachable")
    except RuntimeError:
        pass
    check("a step that raises logs no duration",
          not any("Library read" in ln for ln in lines()), lines())
finally:
    E.time.time = _real_time

# ── A stage moves the dashboard step, from the same call ────────────────────
# The banner and the stepper used to be set independently, and drifted: the
# protected-collections query drove the step to "Scoring" while the log was
# still inside RUN CONTEXT, so a failure there pointed at a step the run had not
# reached. One call now sets both.
fresh()
_emitted = []
_real_emit = E.emit_progress
E.emit_progress = lambda **f: _emitted.append(f)
try:
    E.log_stage("READING LIBRARY", phase="library")
    check("a stage moves the step with it",
          any(f.get("phase") == "library" for f in _emitted), _emitted)
    check("...and tells the UI which stage that is",
          any(f.get("stage") == "READING LIBRARY" for f in _emitted), _emitted)
    check("...and records it for a later failure to name",
          E._CURRENT_PHASE == "library", E._CURRENT_PHASE)

    # A failure marks the stage that was OPEN, not one hardcoded at the call
    # site — most abort sites live in helpers that run from several stages.
    _emitted.clear()
    try:
        E._abort_api_failure("Jellyfin unreachable")
    except SystemExit:
        pass
    err = [f for f in _emitted if f.get("status") == "error"]
    check("a failure marks the stage the run was actually in",
          err and err[0].get("stage") == "READING LIBRARY"
          and err[0].get("phase") == "library", err)
    check("...and the log names that stage for a bug report",
          any("ABORT stage: READING LIBRARY" in ln for ln in lines()), lines()[-3:])
finally:
    E.emit_progress = _real_emit

# ── A summary banner does not move the step backwards ───────────────────────
fresh()
E.log_stage("SIMULATION", phase="simulating")
before = E._CURRENT_PHASE
E.log_stage("DRY RUN SUMMARY [LIBRARY CAP]", timed=False)
check("an un-phased banner leaves the step where it was",
      E._CURRENT_PHASE == before, E._CURRENT_PHASE)

# ── The footer must not be mistaken for a section banner ────────────────────
# The dashboard's "jump to section" links match the ====== banners; a footer
# that also matched would send every link to the wrong place.
import app as A  # noqa: E402
for kind, rx in A._LOG_SECTION_RES.items():
    if kind == "errors":
        continue                      # matches words, not banners
    hit = rx.search("------ SCAN finished in 21s ------")
    check(f"the duration line is not read as the {kind} section", not hit, hit)
check("the SCAN banner is still found",
      bool(A._LOG_SECTION_RES["scan"].search("====== SCAN ======")))
check("a summary banner is still found",
      bool(A._LOG_SECTION_RES["summary"].search("====== DRY RUN SUMMARY [LIBRARY CAP] ======")))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
