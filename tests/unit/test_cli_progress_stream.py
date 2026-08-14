"""What `mediareducer cleanup` shows while it runs, and what it exits with.

The streaming half of the CLI never executed under the suite. cli_smoke drives
the read commands over real HTTP and deliberately starts no run, and the
confirmation tests stub `_stream_run` out entirely — so the loop a user watches
during a real Cleanup, and the exit code a script reads afterwards, had no test
at all.

Both halves fail in ways that look like working software:

  • the exit code is the whole interface for `mediareducer cleanup && rsync …`.
    A run that was STOPPED or that ERRORED must not exit 0, or the next command
    in the chain runs against a library that was only partly reduced;
  • Ctrl-C is a detach, not a failure — the run keeps going server-side, so
    exiting non-zero there would report a problem that does not exist;
  • the loop must end when the run does. A terminal status it fails to
    recognise is an infinite poll, which in a terminal reads as a hung app;
  • a frame the server has not filled in yet must be skipped, not rendered.
    Progress is polled, so the first frames of a run legitimately arrive empty;
  • and a Simulate must never say it "freed" anything. It deletes nothing, and
    a line claiming otherwise is the one piece of output that would make
    someone check their library in a panic.

No network and no waiting: api() is stubbed with a scripted sequence of frames
and time.sleep is a no-op. The frame feed raises if it is polled after its last
frame, so a loop that fails to terminate fails the test instead of hanging it.
"""
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import cli  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


BASE = "http://127.0.0.1:7575"

# The loop's only pause. Left real, a dozen frames would cost a dozen seconds.
cli.time = SimpleNamespace(sleep=lambda _s: None)

CALLS = []


def feed(*frames):
    """api() stub whose /api/run/progress returns each frame in turn.

    Running past the last frame means the loop did not stop when the run did,
    so it raises rather than repeating the final frame forever — an infinite
    poll should fail this test, not hang it.
    """
    it = iter(frames)

    def _api(method, path, base, *, body=None, timeout=60):
        CALLS.append((method, path))
        if path.startswith("/api/run/progress"):
            try:
                return 200, next(it)
            except StopIteration:
                raise AssertionError("polled again after the last frame — "
                                     "the loop did not recognise a terminal status")
        return 200, {"ok": True, "started": True}
    return _api


def stream(*frames):
    """Run _stream_run against a scripted feed; return (exit code, output)."""
    del CALLS[:]
    cli.api = feed(*frames)
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        rc = cli._stream_run(BASE)
    return rc, buf.getvalue()


def frame(status="running", **kw):
    d = {"status": status}
    d.update(kw)
    return d


# ── The exit code a script reads ───────────────────────────────────────────
# Only a run that reached "done" succeeded. The other two terminal states are
# a user's Stop and a crash, and both leave the library in a state the next
# command in a shell chain must not assume anything about.
rc, o = stream(frame(), frame("done", message="All done"))
check("a run that finishes exits 0", rc == 0, rc)
check("...and says so", "DONE: All done" in o, o)

rc, o = stream(frame(), frame("stopped", message="Stopped by request"))
check("a stopped run exits 1, so `cleanup && …` does not continue", rc == 1, rc)
check("...naming the state", "STOPPED: Stopped by request" in o, o)

rc, o = stream(frame(), frame("error", message="Tautulli unreachable"))
check("a failed run exits 1", rc == 1, rc)
check("...and surfaces the server's message", "ERROR: Tautulli unreachable" in o, o)

# A terminal frame with no message still terminates, and prints no stand-in
# for the message it did not get.
rc, o = stream(frame("done"))
check("a terminal frame with no message still ends the loop", rc == 0, rc)
check("...printing the state without a literal None", "DONE" in o and "None" not in o, o)


# ── Frames the server has not filled in ────────────────────────────────────
# Progress is polled, so the first reads of a run arrive before the server has
# written a status. Those must be skipped and retried, not rendered as blanks
# and not crashed on.
rc, o = stream({}, frame("done", message="ok"))
check("an empty frame is skipped, not printed", rc == 0 and "None" not in o, o)

rc, o = stream("<html>502 Bad Gateway</html>", frame("done", message="ok"))
check("a non-JSON body (a proxy error page) does not crash the stream", rc == 0, rc)

rc, o = stream(frame("running", phase="reticulating"), frame("done", message="ok"))
check("an unknown phase prints no label rather than raising",
      rc == 0 and "reticulating" not in o, o)


# ── Saying each thing once ─────────────────────────────────────────────────
# The loop polls once a second; a run spends minutes in one phase. Printing
# every identical frame would bury the run in duplicate lines.
same = frame("running", phase="scanning", scanned=3, total=10, eligible=2)
rc, o = stream(same, dict(same), dict(same), frame("done", message="ok"))
check("a repeated phase prints its label once",
      o.count("Scanning & scoring") == 1, o)
check("...and a repeated progress line prints once",
      o.count("scanned 3/10") == 1, o)

rc, o = stream(frame("running", phase="scanning", scanned=1, total=10, eligible=0),
               frame("running", phase="scanning", scanned=7, total=10, eligible=4),
               frame("done", message="ok"))
check("a changed progress line does print again",
      "scanned 1/10" in o and "scanned 7/10" in o, o)

rc, o = stream(frame("running", phase="library"),
               frame("running", phase="scanning", scanned=1, total=2, eligible=1),
               frame("done", message="ok"))
check("each phase it passes through is announced",
      "Reading library" in o and "Scanning & scoring" in o, o)


# ── Ctrl-C detaches; it does not fail ──────────────────────────────────────
# The run is server-side. Detaching from the log is not an error, and exiting
# non-zero would report a failure that did not happen.
def interrupting(method, path, base, *, body=None, timeout=60):
    raise KeyboardInterrupt


cli.api = interrupting
buf = io.StringIO()
with redirect_stdout(buf), redirect_stderr(io.StringIO()):
    rc = cli._stream_run(BASE)
o = buf.getvalue()
check("Ctrl-C exits 0 — the run is not the terminal", rc == 0, rc)
check("...and says the run continues", "detached" in o and "continues" in o, o)


# ── _progress_line: the verb a Simulate is allowed to use ──────────────────
# "freed" during a Simulate would claim files are gone that are all still
# there. This is the one line in the CLI where a wrong word is alarming.
line = cli._progress_line({"phase": "simulating", "bytes_freed": 5 * cli.GB,
                           "target_bytes": 20 * cli.GB, "deleted": 3})
check("a Simulate says what it WOULD free", "would free" in line, line)
check("...and never claims anything was freed", "freed " not in line, line)
check("...while still showing the amount and the target",
      "5.0 GB" in line and "20.0 GB" in line, line)

line = cli._progress_line({"phase": "deleting", "bytes_freed": 5 * cli.GB,
                           "target_bytes": 20 * cli.GB, "deleted": 3})
check("a real deletion says freed", line.startswith("freed 5.0 GB"), line)
check("...against its target", "/ 20.0 GB" in line, line)

line = cli._progress_line({"phase": "deleting", "bytes_freed": 5 * cli.GB, "deleted": 1})
check("no target means no divisor, not '/ 0.0 GB'", "/" not in line, line)

line = cli._progress_line({"phase": "scanning", "scanned": 4, "total": 9, "eligible": 2})
check("scanning counts what it has seen and what qualified",
      line == "scanned 4/9 — eligible 2", line)

check("a scan with no total yet renders nothing",
      cli._progress_line({"phase": "scanning", "scanned": 4}) == "", "")
check("a phase with no progress of its own renders nothing",
      cli._progress_line({"phase": "checking"}) == "", "")


# ── cmd_run decides whether to stream at all ───────────────────────────────
def args(**kw):
    base = {"yes": True, "json": False, "no_follow": False, "timeout": 60}
    base.update(kw)
    return SimpleNamespace(**base)


def run_cmd(a, api_stub):
    del CALLS[:]
    cli.api = api_stub
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = cli.cmd_run(a, BASE, "headroom", "Cleanup")
    return rc, buf.getvalue(), err.getvalue(), list(CALLS)


started = feed(frame("done", message="ok"))

rc, o, e, calls = run_cmd(args(no_follow=True), started)
check("--no-follow returns without polling progress",
      rc == 0 and not any(p.startswith("/api/run/progress") for _, p in calls), calls)
check("...after saying the run started", "Cleanup started." in o, o)

rc, o, e, calls = run_cmd(args(json=True), feed(frame("done", message="ok")))
check("--json returns the server's reply and does not stream",
      rc == 0 and not any(p.startswith("/api/run/progress") for _, p in calls), calls)
check("...as JSON", o.strip().startswith("{"), o)

rc, o, e, calls = run_cmd(args(), feed(frame("running", phase="deleting"),
                                       frame("done", message="ok")))
check("without --no-follow it streams the run",
      any(p.startswith("/api/run/progress") for _, p in calls), calls)
check("...printing the phase as it goes", "Deleting" in o, o)


# A refusal from the server must not be followed by a progress stream: there
# is no run to follow, and polling would print a stale previous run's frames.
def refused(method, path, base, *, body=None, timeout=60):
    CALLS.append((method, path))
    if path == "/api/run":
        return 409, {"ok": False, "message": "A run is already active"}
    raise AssertionError("streamed a run the server refused to start")


rc, o, e, calls = run_cmd(args(), refused)
check("a refused run exits 1 without streaming", rc == 1, rc)
check("...reporting the server's reason on stderr",
      "A run is already active" in e, e)

# "started": False is the server saying there was nothing to do — a real
# outcome, not a failure, and nothing to stream.
def nothing_to_do(method, path, base, *, body=None, timeout=60):
    CALLS.append((method, path))
    if path == "/api/run":
        return 200, {"ok": True, "started": False, "message": "Already within limits."}
    raise AssertionError("streamed a run that never started")


rc, o, e, calls = run_cmd(args(), nothing_to_do)
check("a run the server declined to start exits 0 and streams nothing", rc == 0, rc)
check("...passing on why", "Already within limits." in o, o)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
