"""publish.sh's release tag: created once, moved only when asked.

The publish workflow builds an image when a v* tag ARRIVES, so the tag is the
release trigger and not just a label. That gives the two behaviours here their
weight:

  • --tag must leave an existing tag alone. Re-tagging on every publish would
    silently rebuild and replace a published image whenever anyone synced a
    doc fix, and the User Scripts field is meant to hold `tag` permanently.
  • --retag must actually move it — deleting the tag from the public repo AND
    its origin, then putting it on the commit just pushed. That is how a fix
    too small to spend a version on gets an image, and it is the one path in
    this script that rewrites something already published.

Everything runs against throwaway repos with a local bare "origin", so this
touches no network and no real remote. The dev repo is a fixture, not this one:
a test that published THIS repo for real would be a very bad afternoon.

The wrapper pasted into Unraid's User Scripts is checked too, from the header
comment it is copied out of, since that block is what actually runs on the
server. Its keyword patterns are anchored at both ends: that is what keeps
`retag …` out of the `tag` branch and an ordinary message like "Fix the retag
docs" out of all three. Loosen one to `*tag*` and both stop being true — the
first would publish under the wrong flag, the second would eat words off the
commit message.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / "tools" / "publish.sh"

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


ENV = dict(os.environ,
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
           GIT_TERMINAL_PROMPT="0")


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, env=ENV, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def git(repo, *args):
    r = run("git", "-C", str(repo), *args)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stdout}")
    return r.stdout.strip()


T = Path(tempfile.mkdtemp(prefix="mr-publish."))
try:
    DEV, PUB, ORIGIN = T / "dev", T / "public", T / "origin.git"
    run("git", "init", "-q", "--bare", str(ORIGIN))
    run("git", "init", "-q", "-b", "main", str(DEV))
    (DEV / "tools").mkdir(parents=True)
    shutil.copy2(PUBLISH, DEV / "tools" / "publish.sh")
    (DEV / "app.py").write_text('APP_VERSION = "9.9.9-test.1"\n')
    (DEV / "README.md").write_text("one\n")
    git(DEV, "add", "-A"); git(DEV, "commit", "-qm", "one")
    run("git", "clone", "-q", str(ORIGIN), str(PUB))
    (PUB / "seed.txt").write_text("seed\n")
    git(PUB, "checkout", "-q", "-b", "main")
    git(PUB, "add", "-A"); git(PUB, "commit", "-qm", "seed")
    git(PUB, "push", "-q", "-u", "origin", "main")

    def publish(*flags):
        return run("bash", str(DEV / "tools" / "publish.sh"), "--yes",
                   *flags, str(DEV), str(PUB))

    def commit_dev(text, version=None):
        (DEV / "README.md").write_text(text)
        if version:
            (DEV / "app.py").write_text(f'APP_VERSION = "{version}"\n')
        git(DEV, "add", "-A"); git(DEV, "commit", "-qm", text.strip())

    # Where a tag points, on each side. They are read separately on purpose:
    # a delete that only lands locally leaves the release still triggered from
    # the server's copy, and that is the failure worth catching.
    def local_tag(tag):
        r = run("git", "-C", str(PUB), "rev-list", "-n1", tag)
        return r.stdout.strip() if r.returncode == 0 else None

    def remote_tag(tag):
        # Both patterns: asking for refs/tags/<name> alone answers with the
        # annotated tag OBJECT, not the commit under it, and comparing that to
        # a commit sha fails for reasons that have nothing to do with the tag
        # being in the right place.
        out = git(PUB, "ls-remote", "--tags", "origin",
                  f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
        for line in out.splitlines():
            if line.endswith("^{}"):          # the commit an annotated tag wraps
                return line.split()[0]
        return out.split()[0] if out else None

    TAG = "v9.9.9-test.1"

    # ── It gets created once ────────────────────────────────────────────────
    publish("--tag")
    first = git(PUB, "rev-parse", "HEAD")
    check("--tag creates the version tag on the published commit",
          local_tag(TAG) == first and remote_tag(TAG) == first,
          (local_tag(TAG), remote_tag(TAG), first))

    # ── ...and --tag never moves it ─────────────────────────────────────────
    commit_dev("two\n")
    out = publish("--tag").stdout
    second = git(PUB, "rev-parse", "HEAD")
    check("a second publish on the same version moves nothing",
          second != first and local_tag(TAG) == first and remote_tag(TAG) == first,
          (local_tag(TAG), first, second))
    check("...and says --retag is how to move it",
          "already exists" in out and "--retag" in out, out)

    # ── --dry-run --retag touches nothing ───────────────────────────────────
    out = publish("--retag", "--dry-run").stdout
    check("--dry-run --retag reports the delete without doing it",
          "DELETE" in out and local_tag(TAG) == first and remote_tag(TAG) == first,
          (out, local_tag(TAG)))

    # ── --retag moves it, on BOTH sides ─────────────────────────────────────
    commit_dev("three\n")
    out = publish("--retag").stdout
    third = git(PUB, "rev-parse", "HEAD")
    check("--retag puts the tag on the commit just published",
          third != first and local_tag(TAG) == third and remote_tag(TAG) == third,
          (local_tag(TAG), remote_tag(TAG), third))
    check("...and says what it deleted, rather than moving it quietly",
          "Deleted" in out and TAG in out, out)

    # ── The rebuild case: nothing to publish, the tag IS the run ────────────
    # A fix already published under this version leaves no diff, so the whole
    # point of the run is the tag arriving again.
    out = publish("--retag").stdout
    check("--retag still moves the tag when there is nothing to commit",
          "nothing to publish" in out.lower()
          and local_tag(TAG) == third and remote_tag(TAG) == third, out)

    # ── An untagged version is just tagged, not "re"-tagged ─────────────────
    commit_dev("four\n", version="9.9.9-test.2")
    publish("--retag")
    fourth = git(PUB, "rev-parse", "HEAD")
    check("--retag on a version that was never tagged simply creates it",
          local_tag("v9.9.9-test.2") == fourth
          and remote_tag("v9.9.9-test.2") == fourth,
          (local_tag("v9.9.9-test.2"), fourth))
    check("...and leaves the previous version's tag where it was",
          local_tag(TAG) == third, (local_tag(TAG), third))

    # ── The User Scripts wrapper ────────────────────────────────────────────
    # Extracted the documented way, so a wrapper that drifts from the block
    # people actually paste is a failure here.
    block = subprocess.run(
        ["sed", "-n", r"/^# #!\/bin\/bash/,/^# exec bash/p", str(PUBLISH)],
        env=ENV, text=True, capture_output=True).stdout
    wrapper = re.sub(r"^# ?", "", block, flags=re.M)
    check("the wrapper block is still extractable from the header",
          wrapper.startswith("#!/bin/bash") and "TAG_FLAG" in wrapper, wrapper[:80])

    # Run it for real, with two edits: the Unraid path points at this repo so
    # its own existence check passes, and the exec becomes an echo so it
    # reports the flags it would have passed instead of publishing anything.
    probe = T / "wrapper.sh"
    probe.write_text(
        re.sub(r'^DEV_REPO=.*$', f'DEV_REPO="{ROOT}"', wrapper, flags=re.M)
        .replace('exec bash "$PUBLISH"', 'echo ARGS:'))
    def dispatch(field):
        r = run("bash", str(probe), *field.split())
        line = [l for l in r.stdout.splitlines() if l.startswith("ARGS:")]
        return line[0] if line else r.stdout.strip()

    check("the wrapper reads a leading 'retag' as --retag, not --tag",
          dispatch("retag Alpha 9 rebuild") == "ARGS: --yes --retag -m Alpha 9 rebuild",
          dispatch("retag Alpha 9 rebuild"))
    check("...and 'retag' alone keeps the default message",
          dispatch("retag") == "ARGS: --yes --retag", dispatch("retag"))
    check("plain 'tag' still means --tag",
          dispatch("tag Alpha 9 release") == "ARGS: --yes --tag -m Alpha 9 release",
          dispatch("tag Alpha 9 release"))
    check("a message that merely CONTAINS retag is still just a message",
          dispatch("Fix the retag docs")
          == "ARGS: --yes --no-tag -m Fix the retag docs",
          dispatch("Fix the retag docs"))
    check("no keyword still means no tag",
          dispatch("defaults") == "ARGS: --yes --no-tag", dispatch("defaults"))
finally:
    shutil.rmtree(T, ignore_errors=True)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
