"""The workflows run in two repositories, and only one of them is this one.

The dev repo's publish script mirrors this directory into the public repo, so
every workflow file here is also a workflow file there — under a different
name, a different visibility, and with no self-hosted runner attached. The
checks below are the ones where getting it wrong is silent: a job that queues
for 24 hours before failing, or a commit quietly billed twice.

This ships, so it is written to hold in BOTH repositories. It asserts the
fallback, never that the run is self-hosted.
"""
import re
import sys
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def load(name):
    d = yaml.safe_load((WF / name).read_text())
    # YAML 1.1 reads a bare `on:` as the boolean True, which is exactly the key
    # every GitHub workflow uses. Both spellings, so this does not depend on
    # which YAML version the installed parser follows.
    d["on"] = d.get("on", d.get(True))
    return d


tests = load("tests.yml")
publish = load("publish.yml")

# ── Where the job runs ───────────────────────────────────────────────────────
runs_on = tests["jobs"]["tests"]["runs-on"]
check("the test job picks its runner from the repository it is in",
      "${{" in str(runs_on) and "github.repository" in str(runs_on), runs_on)
# The half that matters in the mirrored copy: no runner of ours is attached
# there, so the expression must land on a GitHub-hosted label.
check("...and falls back to a hosted runner for the public repo",
      "ubuntu-latest" in str(runs_on), runs_on)
# Named in full. A partial match ("MediaReducer") would also be true of the
# public repo, and would send its jobs to a runner that does not exist.
check("...keyed to the private repo by its full name",
      "Stencil923/MediaReducer-Dev" in str(runs_on), runs_on)

# The static check above only proves what the file asks for. Something has to
# check what the runner actually was, or a mis-edited expression quietly bills
# the private repo again and nothing says so until a billing page does.
steps = tests["jobs"]["tests"]["steps"]
proof = next((s for s in steps if "runner.environment" in str(s.get("run", ""))), None)
check("a step reports the runner it actually got", proof is not None,
      [s.get("name") for s in steps])
check("...before the suite spends anything on the wrong one",
      proof is not None and steps.index(proof) == 0,
      [s.get("name") or s.get("uses") for s in steps[:2]])
check("...and treats a billed runner here as a failure",
      proof is not None and "exit 1" in str(proof.get("run", "")), proof)

# The publish job builds and boots a Docker image. It only ever runs in the
# public repo, and the runner there must be GitHub's.
check("the publish job stays on a hosted runner",
      publish["jobs"]["publish"]["runs-on"] == "ubuntu-latest",
      publish["jobs"]["publish"]["runs-on"])
# A self-hosted runner with the docker socket is root on the host, and that is
# the one thing the container hardening is careful not to hand out.
check("nothing asks for the docker socket",
      not any("docker.sock" in p.read_text() for p in WF.glob("*.yml")))

# ── How often it runs ────────────────────────────────────────────────────────
# push and pull_request are separate concurrency groups, so when both fire for
# one commit neither cancels the other and the whole suite runs twice.
triggers = tests["on"]
push = triggers.get("push") or {}
branches = push.get("branches") or []
check("push and pull_request cannot both fire for one commit",
      "pull_request" not in triggers or "**" not in branches,
      {"push.branches": branches, "pull_request": "pull_request" in triggers})
# Fork pull requests never fire `push` on this repo, so dropping the trigger
# would leave outside contributions untested where that matters.
check("...with pull_request kept, since that is how the public repo sees a fork",
      "pull_request" in triggers, sorted(map(str, triggers)))
check("a branch with no pull request can still be run by hand",
      "workflow_dispatch" in triggers, sorted(map(str, triggers)))

# A newer push makes an older run pointless; without this they queue up behind
# each other on a single self-hosted runner.
check("an in-flight run is cancelled by a newer one",
      tests.get("concurrency", {}).get("cancel-in-progress") is True,
      tests.get("concurrency"))

# ── The release gate ─────────────────────────────────────────────────────────
# The image is what deletes people's files. It must not be buildable without
# the suite passing on the exact commit being tagged.
check("publishing still runs the suite first",
      str(publish["jobs"]["tests"].get("uses", "")).endswith("tests.yml"),
      publish["jobs"]["tests"])
check("...and the build waits for it",
      publish["jobs"]["publish"].get("needs") == "tests",
      publish["jobs"]["publish"].get("needs"))

# ── The runner image ─────────────────────────────────────────────────────────
# Held back from the public repo along with the rest of the dev tooling, so
# only checked when it is here.
compose = ROOT / "tools" / "ci-runner" / "docker-compose.yml"
if compose.exists():
    svc = yaml.safe_load(compose.read_text())["services"]["ci-runner"]
    env = " ".join(svc.get("environment") or [])
    # The reason this is a private-repo-only arrangement: a self-hosted runner
    # on a public repo executes whatever a fork's pull request contains.
    repo_url = next((v.split("=", 1)[1] for v in (svc.get("environment") or [])
                     if v.startswith("REPO_URL=")), "")
    check("the runner is pointed at the private repo",
          repo_url.rstrip("/").endswith("/MediaReducer-Dev"), repo_url)
    check("...and labelled to match runs-on",
          "self-hosted" in env, env)
    check("the runner cannot reach the docker socket",
          not any("docker.sock" in v for v in (svc.get("volumes") or [])), svc.get("volumes"))
    # Whichever credential is in use, it is interpolated from .env. A literal
    # one here is a token committed to the repo — and the registration token
    # is short and unremarkable enough to paste in without it looking wrong.
    creds = [v.split("=", 1)[1] for v in (svc.get("environment") or [])
             if v.split("=", 1)[0] in ("RUNNER_TOKEN", "ACCESS_TOKEN")]
    check("a credential is configured at all", len(creds) == 1, creds)
    check("...and it is read from the environment, never written here",
          all(c.startswith("${") for c in creds)
          and not re.search(r"gh[pous]_[A-Za-z0-9]{16,}|=A[A-Z0-9]{20,}", env), creds)
    dockerfile = (ROOT / "tools" / "ci-runner" / "Dockerfile").read_text()
    check("the runner image is pinned",
          re.search(r"^FROM \S+:\d+\.\d+\.\d+", dockerfile, re.M) is not None,
          dockerfile.splitlines()[-1])
    # The workflow and the baked chromium dependencies have to agree, or the
    # image installs the libraries for a browser build the suite does not use.
    pw = re.search(r"playwright@(\d+\.\d+\.\d+)", (WF / "tests.yml").read_text())
    check("...and its Playwright matches the workflow's",
          pw is not None and f"playwright@{pw.group(1)}" in dockerfile,
          pw.group(1) if pw else None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
