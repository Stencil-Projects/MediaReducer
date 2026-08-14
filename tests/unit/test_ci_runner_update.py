"""github-ci-update.sh: what a scheduled run is allowed to do unattended.

Run by hand, this script is supervised — somebody reads the output and can
undo a bad call. On a monthly cron nobody is watching, so every refusal has to
be right on its own, and each of them protects something different:

  • a container that is DOWN stays down. `docker compose up -d` starts what it
    finds stopped, so an update on a schedule would quietly resurrect a runner
    somebody deliberately turned off;
  • a job that is EXECUTING is never interrupted. The whole point of pinning
    the runner is that CI does not change under a running job, and swapping
    the container mid-job fails a build that had nothing to do with this;
  • an unreachable GitHub, an absent stack, and a container that is not
    installed are all "nothing to do", not failures. A scheduled script that
    exits non-zero for ordinary conditions is a scheduled script people
    switch off.

And the one that is invisible when it goes wrong: the Dockerfile backup must
be taken BEFORE the pin is edited. Taken after, a failed build restores the
already-bumped file, so the pin claims a version the container is not running
— and on a schedule that is permanent, because the next run compares pin to
latest, finds them equal, and never retries. The runner then ages out of
GitHub's minimum version and jobs queue forever, which is the exact failure
the script exists to prevent.

Everything runs against a throwaway stack directory with a fake `docker` on
PATH that records what it was asked to do, so no container is touched and
nothing reaches the network (the release lookup is pointed at a file:// URL,
or left to fail, per case).
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "github-ci-update.sh"

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


# tools/ is held back from the public repo, so a checkout without this script
# is a checkout with nothing here to test — say so rather than fail.
if not SCRIPT.is_file():
    print(f"SKIP no {SCRIPT.relative_to(ROOT)} in this checkout, so there is "
          "nothing here to test. This file is not meant to ship without it.")
    print("RESULT: PASS")
    sys.exit(0)

PINNED = "2.336.0"
NEWER = "2.340.0"

FAKE_DOCKER = r"""#!/bin/bash
# Records every invocation, and answers the three questions the script asks.
echo "$@" >> "$DOCKER_LOG"
case "$1" in
  inspect)
    # -f '{{.State.Status}}' or '{{.State.Running}}'
    case "$*" in
      *State.Status*)  [[ "$FAKE_STATE" == "missing" ]] && exit 1; echo "$FAKE_STATE" ;;
      *State.Running*) [[ "$FAKE_STATE" == "running" ]] && echo "true" || echo "false" ;;
    esac
    ;;
  top)
    [[ "$FAKE_STATE" == "running" ]] || exit 1
    echo "UID   PID   CMD"
    if [[ "$FAKE_BUSY" == "1" ]]; then
      echo "root  123   /home/runner/bin/Runner.Worker"
    else
      echo "root  123   /home/runner/bin/Runner.Listener"
    fi
    ;;
  compose)
    case "$2" in
      build) [[ "$FAKE_BUILD_FAILS" == "1" ]] && exit 1; echo "built" ;;
      up)    [[ "$FAKE_UP_FAILS" == "1" ]] && exit 1; echo "up" ;;
    esac
    ;;
  image) ;;
esac
exit 0
"""

WORK = tempfile.mkdtemp(prefix="mediareducer-ci-update.")
BIN = os.path.join(WORK, "bin")
os.makedirs(BIN)
docker = os.path.join(BIN, "docker")
with open(docker, "w") as f:
    f.write(FAKE_DOCKER)
os.chmod(docker, 0o755)

# A release feed the script can read without a network: curl accepts file://.
RELEASES = os.path.join(WORK, "releases.json")
with open(RELEASES, "w") as f:
    f.write('{"tag_name": "v%s"}\n' % NEWER)


# Detection reads the process tree through `ps`, so a fake `ps` is how the two
# ancestries get tested without arranging a real crond.
FAKE_PS = r"""#!/bin/bash
if [[ -n "${FAKE_PS_COMM:-}" ]]; then
  case "$*" in
    *comm=*) echo "$FAKE_PS_COMM"; exit 0 ;;
    *ppid=*) echo 1; exit 0 ;;
  esac
fi
exec "$REAL_PS" "$@"
"""
REAL_PS = shutil.which("ps") or "/bin/ps"
with open(os.path.join(BIN, "ps"), "w") as f:
    f.write(FAKE_PS)
os.chmod(os.path.join(BIN, "ps"), 0o755)


def run(state="running", busy=False, args=(), latest=NEWER,
        build_fails=False, up_fails=False, stdin=None, ancestor=None):
    """Run the script against a fresh stack dir. Returns (rc, out, pin, docker log)."""
    stack = tempfile.mkdtemp(prefix="stack.", dir=WORK)
    with open(os.path.join(stack, "Dockerfile"), "w") as f:
        f.write(f"FROM myoung34/github-runner:{PINNED}-ubuntu-noble\n"
                "RUN echo hello\n")
    with open(os.path.join(stack, "docker-compose.yml"), "w") as f:
        f.write("services:\n  runner:\n    build: .\n")
    log = os.path.join(stack, "docker.log")
    open(log, "w").close()

    env = dict(os.environ)
    env["PATH"] = BIN + os.pathsep + env["PATH"]
    env.update(DOCKER_LOG=log, FAKE_STATE=state, FAKE_BUSY="1" if busy else "0",
               FAKE_BUILD_FAILS="1" if build_fails else "0",
               FAKE_UP_FAILS="1" if up_fails else "0",
               REAL_PS=REAL_PS, FAKE_PS_COMM=ancestor or "")
    # An empty "latest" stands for github.com being unreachable.
    src = f"file://{RELEASES}" if latest else f"file://{WORK}/nope.json"
    body = SCRIPT.read_text().replace(
        'RUNNER_RELEASES_API="https://api.github.com/repos/actions/runner/releases/latest"',
        f'RUNNER_RELEASES_API="{src}"')
    patched = os.path.join(stack, "github-ci-update.sh")
    with open(patched, "w") as f:
        f.write(body)
    os.chmod(patched, 0o755)

    p = subprocess.run(["bash", patched, f"RUNNER_DIR={stack}", *args],
                       capture_output=True, text=True, env=env,
                       input=stdin if stdin is not None else "")
    pin = ""
    for line in Path(stack, "Dockerfile").read_text().splitlines():
        if line.startswith("FROM "):
            pin = line.split(":", 1)[1].split("-", 1)[0]
    return p.returncode, p.stdout + p.stderr, pin, Path(log).read_text()


check("the script parses", subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0)


# ── A scheduled run, in the state where it should act ──────────────────────
rc, out, pin, dlog = run(args=["AUTO=1"])
check("a scheduled run updates a behind pin", pin == NEWER, (pin, out))
check("...says what it is doing", f"{PINNED} is behind {NEWER}" in out, out)
check("...builds and swaps the container",
      "compose build" in dlog and "compose up" in dlog, dlog)
check("...and exits 0", rc == 0, (rc, out))

rc, out, pin, dlog = run(args=["AUTO=1"], latest=None)
check("...but not when GitHub cannot be reached", pin == PINNED, (pin, out))
check("...saying so, without failing the schedule", rc == 0 and "could not reach" in out,
      (rc, out))


# ── A stopped container is left stopped ────────────────────────────────────
# `compose up -d` starts what it finds stopped, so acting here would undo a
# decision somebody made.
for state in ("exited", "created", "paused"):
    rc, out, pin, dlog = run(state=state, args=["AUTO=1"])
    check(f"a {state} container is left down", "compose up" not in dlog, dlog)
    check(f"...and its pin untouched ({state})", pin == PINNED, pin)
    check(f"...reported, not failed ({state})", rc == 0 and "left down" in out, (rc, out))

rc, out, pin, dlog = run(state="missing", args=["AUTO=1"])
check("a container that is not installed is nothing to do",
      rc == 0 and "nothing to update" in out, (rc, out))
check("...and nothing is built", "compose build" not in dlog, dlog)


# ── A job in flight is never interrupted ───────────────────────────────────
rc, out, pin, dlog = run(busy=True, args=["AUTO=1"])
check("a scheduled run skips while a job is executing",
      rc == 0 and "a job is executing" in out, (rc, out))
check("...touching neither the pin nor the container",
      pin == PINNED and "compose" not in dlog, (pin, dlog))
# It has to decide that BEFORE editing anything, and say the schedule-shaped
# thing. The rebuild path refuses a busy runner too, but only after the pin
# has been written and rolled back, and it advises re-running by hand — which
# is the wrong instruction for something that re-runs itself.
check("...deciding before it edits, and pointing at its own next run",
      "The next run tries again" in out, out)

rc, out, pin, dlog = run(busy=True, args=["BUMP=1"])
check("a manual BUMP skips too", "a job is executing" in out, out)
check("...and leaves the pin as it found it", pin == PINNED, pin)


# ── Nothing to do ──────────────────────────────────────────────────────────
# Pin already current: point the feed at the pinned version.
with open(RELEASES, "w") as f:
    f.write('{"tag_name": "v%s"}\n' % PINNED)
rc, out, pin, dlog = run(args=["AUTO=1"])
check("a current pin is nothing to do", rc == 0 and "is current" in out, (rc, out))
check("...and builds nothing", "compose build" not in dlog, dlog)
with open(RELEASES, "w") as f:
    f.write('{"tag_name": "v%s"}\n' % NEWER)


# ── The backup ordering ────────────────────────────────────────────────────
# The failure this catches is silent: a bumped pin left behind by a build that
# never succeeded reads as up to date forever after.
rc, out, pin, dlog = run(args=["AUTO=1"], build_fails=True)
check("a failed build puts the ORIGINAL pin back", pin == PINNED, (pin, out))
check("...exits non-zero, because that one IS a failure", rc != 0, (rc, out))
check("...and never swaps the container", "compose up" not in dlog, dlog)

rc, out, pin, dlog = run(args=["AUTO=1"], up_fails=True)
check("an image that will not start also restores the pin", pin == PINNED, (pin, out))
check("...and exits non-zero", rc != 0, (rc, out))

rc, out, pin, dlog = run(args=["BUMP=1"], build_fails=True)
check("a manual BUMP restores its pin the same way", pin == PINNED, (pin, out))


# ── Manual runs ────────────────────────────────────────────────────────────
# No TTY and no mode word: report only. This is the User Scripts "Run Script"
# case, where acting without being asked would be a surprise.
rc, out, pin, dlog = run(args=["defaults"])
check("a manual run with no mode only reports", rc == 0 and "Pinned runner:" in out,
      (rc, out))
check("...changing nothing", pin == PINNED and "compose" not in dlog, (pin, dlog))
check("...and says which mode it chose, so a mis-detected schedule is visible",
      "started by hand" in out or "Latest runner" in out, out)

rc, out, pin, dlog = run(args=[])
check("no argument at all behaves the same as defaults",
      rc == 0 and "Pinned runner:" in out and pin == PINNED, (rc, out))

rc, out, pin, dlog = run(args=["REBUILD=1"])
check("REBUILD rebuilds without moving the pin",
      pin == PINNED and "compose build" in dlog, (pin, dlog))

rc, out, pin, dlog = run(args=["NONSENSE=1"])
check("an unrecognised override is refused rather than ignored",
      rc != 0 and "Unrecognized override" in out, (rc, out))

# A stack that is not there: fatal by hand (you asked for something it cannot
# do), nothing to do on a schedule (this may not be the runner's server).
env = dict(os.environ)
env["PATH"] = BIN + os.pathsep + env["PATH"]
env.update(DOCKER_LOG=os.path.join(WORK, "unused.log"), FAKE_STATE="running",
           FAKE_BUSY="0", FAKE_BUILD_FAILS="0", FAKE_UP_FAILS="0")
missing = os.path.join(WORK, "no-such-stack")
p = subprocess.run(["bash", str(SCRIPT), f"RUNNER_DIR={missing}"],
                   capture_output=True, text=True, env=env, input="")
check("a missing stack is an error by hand", p.returncode != 0
      and "No Dockerfile" in (p.stdout + p.stderr), p.stdout + p.stderr)
p = subprocess.run(["bash", str(SCRIPT), f"RUNNER_DIR={missing}", "AUTO=1"],
                   capture_output=True, text=True, env=env, input="")
check("...and merely nothing to do on a schedule", p.returncode == 0
      and "nothing to update" in p.stdout, p.stdout)


# ── Which mode a run lands in, with no argument to tell it ─────────────────
# This is the whole point: one script, one argument field, two behaviours.
# The plugin gives a scheduled run the same (default) argument as a manual
# one, so the ancestry is the only thing left to tell them apart.
rc, out, pin, dlog = run(ancestor="crond")
check("a run started by cron updates without being asked", pin == NEWER, (pin, out))
check("...and says why it decided that", "started by cron" in out, out)

rc, out, pin, dlog = run(ancestor="php")
check("a run started from the webGUI only reports",
      pin == PINNED and "compose" not in dlog, (pin, dlog))
check("...and says why", "started by hand" in out, out)

# The php rung is not decoration: the webGUI itself can sit under a
# cron-started service, and without it such a run would read as scheduled and
# start rebuilding while somebody watches a report.
rc, out, pin, dlog = run(ancestor="php", args=["AUTO=1"])
check("AUTO=1 overrides detection when it gets it wrong", pin == NEWER, (pin, out))
rc, out, pin, dlog = run(ancestor="crond", args=["REBUILD=1"])
check("...and an explicit mode wins over cron too",
      pin == PINNED and "compose build" in dlog, (pin, dlog))


# ── The User Scripts header the plugin reads ───────────────────────────────
head = SCRIPT.read_text().splitlines()
directives = {ln.split("=", 1)[0]: ln.split("=", 1)[1]
              for ln in head[:8] if ln.startswith("#") and "=" in ln}
check("the plugin gets a description", bool(directives.get("#description")), directives)
check("the argument field explains itself, since it IS the prompt",
      "BUMP=1" in directives.get("#argumentDescription", "")
      and "AUTO=1" in directives.get("#argumentDescription", ""), directives)
# The plugin refuses to run a script whose argument field is empty, so the
# default has to be a word — and it has to be one the parser ignores.
check("the default argument is a word the script treats as no override",
      directives.get("#argumentDefault", "").strip() == "defaults", directives)

shutil.rmtree(WORK, ignore_errors=True)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
