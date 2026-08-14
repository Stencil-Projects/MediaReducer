"""update.sh's overrides, and the deploy key it reaches the dev repo with.

Two of these are worth a test because their failure does not look like their
cause:

  • the SSH key. A deploy key belongs to exactly ONE repository, so the host
    keeps one per repo — a read key for the dev repo this script deploys, and
    the public repo's write key for publish.sh. Point this script at the wrong
    one and GitHub authenticates it happily and then answers "Repository not
    found", which reads as a deleted repo rather than a mismatched key. The
    two names differ by a suffix, so the pairing is pinned here: the key this
    script defaults to must be the dev one, and the repo it clones must be the
    dev repo.

  • the override list. Every key in OVERRIDE_KEYS has to have a case arm, and
    every case arm has to be listed — a key that parses but is not listed is
    undiscoverable, and one that is listed but does not parse is refused as
    "Unrecognized override" after being advertised. The plugin's argument
    description is the only documentation most runs ever see, so it is checked
    against the same list.

Nothing here runs the script against a host: it stops at the first ssh, and
these are the decisions made before that.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPDATE = ROOT / "tools" / "update.sh"

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


# tools/ is held back from the public repo, so a checkout without this script
# has nothing here to test.
if not UPDATE.is_file():
    print(f"SKIP no {UPDATE.relative_to(ROOT)} in this checkout, so there is "
          "nothing here to test. This file is not meant to ship without it.")
    print("RESULT: PASS")
    sys.exit(0)

src = UPDATE.read_text()


def value_of(name):
    m = re.search(rf'^{name}="([^"]*)"', src, re.M)
    return m.group(1) if m else None


# ── The key and the repo have to be the same side of the fence ─────────────
repo = value_of("REPO_URL")
check("it clones the dev repo", repo == "git@github.com:Stencil-Projects/MediaReducer-Dev.git", repo)

key = value_of("SSH_KEY")
check("the default key is overridable", key and key.startswith("${SSH_KEY:-"), key)
key_path = (key or "").replace("${SSH_KEY:-", "").rstrip("}")
check("...and defaults to the DEV deploy key, not the public repo's write key",
      key_path.endswith("github_mediareducer-dev"), key_path)
# The names differ only by the suffix, which is exactly why this is a test and
# not a comment: dropping it leaves a key that authenticates and cannot read.
check("...whose name is distinguishable from the public one",
      key_path != "/root/.ssh/github_mediareducer", key_path)


# ── Advertised and accepted have to be the same set ────────────────────────
listed = (value_of("OVERRIDE_KEYS") or "").split()
parsed = set(re.findall(r"^\s{4}([A-Z_]+)=\*\)", src, re.M))
check("every advertised override is listed", bool(listed), listed)
check("...and each one is actually parsed", set(listed) <= parsed,
      sorted(set(listed) - parsed))
check("...with nothing parsed that is not advertised", parsed <= set(listed),
      sorted(parsed - set(listed)))
check("SSH_KEY is among them", "SSH_KEY" in listed, listed)

# The plugin's argument box is the only documentation a User Scripts run shows.
argdesc = ""
for line in src.splitlines()[:80]:
    if "#argumentDescription=" in line:
        argdesc = line.split("=", 1)[1]
        break
check("the plugin's argument description exists", bool(argdesc), argdesc)
for k in listed:
    check(f"...and mentions {k}", k in argdesc, argdesc)

# The header's key table is the other half of that documentation.
for k in listed:
    check(f"the header documents {k}", re.search(rf"^#   {k}\s", src, re.M) is not None, k)


# ── The error path names the confusion it is most likely to hit ────────────
# "Repository not found" from a wrong-but-valid key is the failure that wastes
# an afternoon, so the access test has to point at what distinguishes it.
access = src.split("repository access test", 1)[-1].split("Repository access succeeded", 1)[0]
check("the access-test error prints the ssh -T check",
      "-T git@github.com" in access, access[-400:])
check("...and says which greeting to expect",
      "Stencil-Projects/MediaReducer-Dev" in access, access[-400:])
check("...naming the wrong-key case, not just the no-key one",
      "Permission denied" in access and "Another repository" in access, access[-400:])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
