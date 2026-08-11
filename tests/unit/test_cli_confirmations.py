"""The questions the CLI asks before it destroys something.

cli.py is a thin HTTP client over the same API the UI uses, so almost nothing
in it can go wrong on its own — with one exception. Four of its commands are
irreversible, and the only thing standing between typing them and doing them
is a prompt: a real Cleanup deletes files with no recycle bin, Clear Cache
throws away the plan and the snapshot, history-clear erases deleted.log, and
config reset returns the install to first-time setup.

A mutation audit removed each of those prompts in turn, and the suite passed
all seven times. cli_smoke drives the CLI over real HTTP against a booted app
and passes --yes throughout, which is right for a smoke test and means the
prompt itself has never run.

What is pinned here is the whole shape of the gate, because every part of it
has a failure that reads as working software:

  • no answer, or the wrong answer, must not proceed — and "no answer" is what
    a terminal gives you when the command is in a cron job or a pipe;
  • Ctrl-C and EOF are refusals, not confirmations;
  • --yes skips the question, because a scripted caller has already answered;
  • and Simulate, which deletes nothing, must NOT ask — a prompt on the safe
    command is how people learn to type y without reading.

No network: api() is stubbed and every call it would have made is recorded.
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


CALLS = []
PROMPTS = []


def fake_api(method, path, base, *, body=None, timeout=60):
    CALLS.append((method, path))
    return 200, {"ok": True, "started": True, "message": "done"}


cli.api = fake_api
cli._stream_run = lambda base: 0          # no progress polling in a unit test


def answering(*replies):
    """Stand in for input(): each call takes the next reply. A reply that is an
    exception class is raised, which is how Ctrl-C and a closed stdin arrive."""
    it = iter(replies)

    def _input(prompt=""):
        PROMPTS.append(prompt)
        r = next(it)
        if isinstance(r, type) and issubclass(r, BaseException):
            raise r()
        return r
    return _input


def drive(fn, *args, replies=(), **kw):
    """Run a command with a scripted answer, returning (exit code, api calls)."""
    del CALLS[:], PROMPTS[:]
    real_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    import builtins
    builtins.input = answering(*replies)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = fn(*args, **kw)
    finally:
        builtins.input = real_input
    return code, list(CALLS), list(PROMPTS)


def args(**kw):
    base = {"yes": False, "json": False, "no_follow": True, "timeout": 60, "limit": 10}
    base.update(kw)
    return SimpleNamespace(**base)


BASE = "http://127.0.0.1:7575"

# ── The one that deletes files ─────────────────────────────────────────────
code, calls, prompts = drive(cli.cmd_run, args(), BASE, "headroom", "Cleanup",
                             replies=["n"])
check("a declined Cleanup starts no run", not calls, calls)
check("...and reports failure, so a script can tell", code == 1, code)
check("...having actually asked", len(prompts) == 1 and "DELETES" in prompts[0], prompts)

code, calls, _ = drive(cli.cmd_run, args(), BASE, "headroom", "Cleanup", replies=[""])
check("pressing Enter is a refusal — the prompt is [y/N]", not calls and code == 1,
      (code, calls))

code, calls, _ = drive(cli.cmd_run, args(), BASE, "headroom", "Cleanup", replies=["yes"])
check("an explicit yes starts the run", ("POST", "/api/run") in calls, calls)

code, calls, _ = drive(cli.cmd_run, args(), BASE, "headroom", "Cleanup", replies=["Y"])
check("...and it is case-insensitive", ("POST", "/api/run") in calls, calls)

for junk in ("maybe", "sure", "1", "yolo"):
    code, calls, _ = drive(cli.cmd_run, args(), BASE, "headroom", "Cleanup", replies=[junk])
    check(f"an answer that is not yes ({junk!r}) refuses", not calls and code == 1,
          (code, calls))

# A closed stdin is the cron/pipe case: the command cannot be answered, so it
# must not act. Ctrl-C is the same answer given deliberately.
for exc, label in ((EOFError, "a closed stdin"), (KeyboardInterrupt, "Ctrl-C")):
    code, calls, _ = drive(cli.cmd_run, args(), BASE, "headroom", "Cleanup", replies=[exc])
    check(f"{label} refuses rather than proceeding", not calls and code == 1,
          (code, calls))

code, calls, prompts = drive(cli.cmd_run, args(yes=True), BASE, "headroom", "Cleanup")
check("--yes answers in advance and asks nothing",
      ("POST", "/api/run") in calls and not prompts, (calls, prompts))

# Simulate deletes nothing, so asking would be noise — and a prompt people
# answer reflexively on the safe command is a prompt they answer reflexively
# on the dangerous one.
code, calls, prompts = drive(cli.cmd_run, args(), BASE, "debug_sim", "Simulate")
check("Simulate does not ask, because it deletes nothing",
      not prompts and ("POST", "/api/run") in calls, (prompts, calls))


# ── The other three irreversible commands ──────────────────────────────────
for fn, label, endpoint in (
        (cli.cmd_cache_clear, "Clear Cache", "/api/cache/clear"),
        (cli.cmd_history_clear, "erasing the deletion history", "/api/logs/deleted/clear"),
        (cli.cmd_config_reset, "resetting all configuration", "/api/config/reset")):
    code, calls, prompts = drive(fn, args(), BASE, replies=["n"])
    check(f"{label} asks first, and a no stops it", not calls and len(prompts) == 1,
          (calls, prompts))
    code, calls, _ = drive(fn, args(), BASE, replies=[""])
    check(f"...Enter alone does not {label}", not calls, calls)
    code, calls, _ = drive(fn, args(), BASE, replies=[EOFError])
    check(f"...and neither does a closed stdin", not calls, calls)
    code, calls, _ = drive(fn, args(), BASE, replies=["y"])
    check(f"...while a yes goes through to {endpoint}",
          any(c[1].startswith(endpoint) for c in calls), calls)
    code, calls, prompts = drive(fn, args(yes=True), BASE)
    check(f"...and --yes skips the question", not prompts and calls, (prompts, calls))


# ── A reply the server did not mark ok is a refusal ────────────────────────
# The default matters: reading a missing "ok" as success turns any unexpected
# reply — a proxy error page, a truncated body — into "Cleanup started".
def unmarked(method, path, base, *, body=None, timeout=60):
    CALLS.append((method, path))
    return 200, {"message": "something else entirely"}


cli.api = unmarked
code, calls, _ = drive(cli.cmd_run, args(yes=True), BASE, "headroom", "Cleanup")
check("a reply with no ok field is treated as a refusal, not a start", code == 1, code)
cli.api = fake_api

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
