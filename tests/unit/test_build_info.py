"""The welcome guide names the commit a build came from, beside its version.

Two builds of one version looked the same: "v0.7.0" said nothing about which
commit a dev image, a local build or a release was. An image has no .git, so
the Dockerfile resolves the commit from its build context while it builds and
ships only the answer (BUILD); a checkout run from source reads its own .git.

Pinned: the resolver reads every shape a checkout comes in (a branch ref,
packed refs, a detached HEAD, a worktree) and answers "" rather than guessing;
BUILD wins over .git and a garbled one is ignored; the guide shows "build
<hash>" beside the version and nothing when there is none; and the Dockerfile
resolves the commit in a throwaway stage that .dockerignore feeds only HEAD and
the refs of .git, with a plain COPY any builder runs, and the image takes
only the answer.

The version beside it is read from the VERSION file, the one copy a bump
moves, and the image ships that file.

Hermetic: temp directories for every checkout; the app renders from a temp config.
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-build-info.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import build_info as B  # noqa: E402
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


SHA = "3ceebee0123456789abcdef0123456789abcdef0"
OTHER = "67e7ff2fedcba9876543210fedcba9876543210f"


def repo(name, files):
    """A checkout under the temp dir: {relative path: text}."""
    root = Path(_OUT) / name
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── Every shape a checkout comes in ─────────────────────────────────────────
check("a branch checked out: HEAD names the ref, the ref names the commit",
      B.commit_of(repo("branch", {".git/HEAD": "ref: refs/heads/main\n",
                                  ".git/refs/heads/main": SHA + "\n"})) == SHA[:7])
check("a branch whose ref git has packed away",
      B.commit_of(repo("packed", {".git/HEAD": "ref: refs/heads/claude/x\n",
                                  ".git/packed-refs": "# pack-refs with: peeled fully-peeled sorted\n"
                                                      f"{OTHER} refs/heads/main\n{SHA} refs/heads/claude/x\n"}))
      == SHA[:7])
check("a detached HEAD, as CI checks out a tag",
      B.commit_of(repo("detached", {".git/HEAD": SHA + "\n"})) == SHA[:7])
wt = repo("wt-main", {".git/worktrees/side/HEAD": "ref: refs/heads/side\n",
                      ".git/worktrees/side/commondir": "../..\n",
                      ".git/refs/heads/side": OTHER + "\n"})
check("a worktree, whose .git is a file naming its own git dir",
      B.commit_of(repo("wt-side", {".git": f"gitdir: {wt}/.git/worktrees/side\n"})) == OTHER[:7])
check("no .git at all (a source archive) says nothing",
      B.commit_of(repo("archive", {"app.py": "# not a checkout\n"})) == "")
check("a ref it cannot find says nothing",
      B.commit_of(repo("lost", {".git/HEAD": "ref: refs/heads/gone\n"})) == "")
check("a HEAD that is not a commit says nothing",
      B.commit_of(repo("garbled", {".git/HEAD": "not a hash\n"})) == "")
check("a worktree pointing out of reach says nothing",
      B.commit_of(repo("wt-lost", {".git": "gitdir: /nowhere/at/all\n"})) == "")

# ── What the app shows: BUILD first, then the checkout's own .git ─────────
stamped = repo("image", {"BUILD": OTHER[:7] + "\n", ".git/HEAD": SHA + "\n"})
check("an image's BUILD file is what the app reports", A._app_build(stamped) == OTHER[:7])
(stamped / "BUILD").write_text("\n", encoding="utf-8")
check("...an empty one (built with no .git) falls back to a checkout's .git",
      A._app_build(stamped) == SHA[:7])
(stamped / "BUILD").write_text("garbage\n", encoding="utf-8")
check("...and so does a garbled one", A._app_build(stamped) == SHA[:7])
check("neither: nothing to show", A._app_build(repo("bare", {})) == "")

# ── The welcome guide ─────────────────────────────────────────────────────
A.app.config["TESTING"] = True


def welcome_line():
    html = A.app.test_client().get("/").get_data(as_text=True)
    m = re.search(r'<div class="welcome-version">([^<]*)</div>', html)
    return m.group(1) if m else None


A.APP_BUILD = "abc1234"
line = welcome_line()
check("the guide shows the build beside the version",
      line is not None and line.startswith(f"v{A.APP_VERSION} &middot; build abc1234"), line)
A.APP_BUILD = ""
line = welcome_line()
check("...and the version alone when there is no build",
      line is not None and line.startswith(f"v{A.APP_VERSION} &middot; ") and "build" not in line, line)

# ── The version beside it is the VERSION file's ───────────────────────────
# It used to be typed into app.py as well, and publish.yml refused any tag the
# two disagreed on. The dashboard's bump moves VERSION alone, so 0.8.0 could
# never have been released: its tag would name a version the app did not.
check("the app's version is what the VERSION file says",
      A.APP_VERSION == (ROOT / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip(),
      A.APP_VERSION)
check("...and app.py types no version of its own",
      not re.search(r"""^APP_VERSION\s*=\s*['"]""", (ROOT / "app.py").read_text(encoding="utf-8"), re.M))
v = repo("version-file", {"VERSION": "1.2.3\r\n"})
check("a VERSION written with Windows line endings reads the same", A._read_app_version(v) == "1.2.3")
check("...and a build without one says so rather than naming a version",
      A._read_app_version(repo("no-version", {})) == "unknown"
      and A._read_app_version(repo("empty-version", {"VERSION": "\n"})) == "unknown")
workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
check("the release workflow checks the tag against VERSION, not against a copy in app.py",
      'FILEVER="$(head -1 VERSION' in workflow
      and not re.search(r"grep[^\n]*APP_VERSION[^\n]*app\.py", workflow))

# ── The image works it out while it builds ────────────────────────────────
docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
_image_copies = [ln.split() for ln in docker.split("FROM python:3.11-slim\n", 1)[1].splitlines()
                 if ln.startswith("COPY ")]
check("the image ships VERSION, which the app reads its version from",
      any("VERSION" in words[1:-1] for words in _image_copies), _image_copies)
ignore = [ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
          if ln.strip() and not ln.startswith("#")]
check("the Dockerfile resolves BUILD in a throwaway stage and the image takes only that",
      "FROM python:3.11-slim AS build-info" in docker and "COPY . /ctx" in docker
      and "RUN python3 /ctx/build_info.py /ctx > /BUILD" in docker
      and "COPY --from=build-info /BUILD BUILD" in docker)
# A BuildKit mount would do the same, and fail the build outright under the
# legacy builder an older Docker or docker-compose v1 still uses; the README's
# install is a local `docker compose up --build`.
check("...with nothing only BuildKit can run", "--mount" not in docker)
check(".dockerignore lets in .git's HEAD and refs, and nothing else of it",
      ".git/*" in ignore and {"!.git/HEAD", "!.git/refs", "!.git/packed-refs"} <= set(ignore)
      and not [x for x in ignore if x.startswith("!.git/") and x not in
               ("!.git/HEAD", "!.git/refs", "!.git/packed-refs")], ignore)
check("...and the image's own stage copies nothing of .git",
      not [ln for ln in docker.split("FROM python:3.11-slim\n", 1)[1].splitlines()
           if ln.strip().startswith("COPY") and (".git" in ln or ln.split()[1:2] == ["."])])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
