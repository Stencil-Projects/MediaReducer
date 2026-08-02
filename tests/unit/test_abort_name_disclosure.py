"""Nothing that names a film leaves the box when Movie names is off.

The dashboard prints an abort sentence whole, and a few of them name a film on
purpose: the sample path in a mount mismatch, the protected collections that
went missing. A webhook is a different audience — the Movie names opt-in says
so — and only the call site that interpolated the name knows which span it is,
so `_abort_api_failure(..., names=...)` declares it and the notifier drops it.

The first guard here is the one that matters: a NEW interpolation in an abort
message fails this test until someone classifies it. Without that the leak is
one f-string away from coming back, and it is invisible on the surface where it
happens — the alert already left the building.
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import notify

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {extra}"))
    ok = ok and cond

# Expressions an abort message may interpolate that cannot name a film: server
# labels, mount points, grammar helpers, the IMDb dataset's own path, counts.
SAFE = {
    "label", "LIBRARY_ROOT", "_noun", "_verb", "_it", "_exist",
    "IMDB_RATINGS_PATH", "IMDB_RATINGS_URL", "age_days", "refresh_days",
    "_empty", "_other", "_other_n",
}
# …and the ones that DO, which must be declared.
NAME_BEARING = {"unmatched[0]", "_names"}

tree = ast.parse((ROOT / "engine.py").read_text())
calls = [c for c in ast.walk(tree)
         if isinstance(c, ast.Call)
         and getattr(c.func, "id", "") in ("_abort_api_failure", "_abort_imdb_ratings")
         and c.args]

def holes(node):
    out = []
    def walk(n):
        if isinstance(n, ast.JoinedStr):
            for v in n.values:
                walk(v)
        elif isinstance(n, ast.FormattedValue):
            out.append(ast.unparse(n.value))
        elif isinstance(n, ast.BinOp):
            walk(n.left); walk(n.right)
    walk(node)
    return out

unclassified, undeclared = [], []
for c in calls:
    kw = {k.arg for k in c.keywords}
    for h in holes(c.args[0]):
        if h not in SAFE and h not in NAME_BEARING:
            unclassified.append(f"engine.py:{c.lineno} {{{h}}}")
        elif h in NAME_BEARING and "names" not in kw:
            undeclared.append(f"engine.py:{c.lineno} {{{h}}}")

check(f"every abort message's interpolations are classified ({len(calls)} call sites)",
      not unclassified, unclassified)
check("every name-bearing abort message declares names=", not undeclared, undeclared)
check("the name-bearing set is not empty (the walk found real calls)",
      any(h in NAME_BEARING for c in calls for h in holes(c.args[0])))

# End to end, on the sentences the engine actually produces — including the two
# that broke a regex attempt at this: the mount point in the same sentence as
# the path, and an apostrophe inside a collection name.
OFF = json.loads((ROOT / "default_config.json").read_text())
check("Movie names is off on a fresh install", OFF.get("NOTIFY_SHOW_MOVIES") is False)
ON = {**OFF, "NOTIFY_SHOW_MOVIES": True}

FILM = "/mnt/user/media/Movies/Eyes Wide Shut (1999)/Eyes Wide Shut.mkv"
COLL = "Keep Forever — Dad's stuff"
CASES = [
    ("a sample path", f"Plex/Tautulli reports movie files that are not under /library, "
                      f"for example {FILM}. Check the media mounts, then run again.", [FILM]),
    ("a quoted collection", f"Protected Plex collection '{COLL}' no longer exists, so nothing "
                            f"was deleted. Fix or uncheck it under Configuration.", [COLL]),
]
for label, msg, names in CASES:
    off = notify.build_error_message(OFF, message=msg, names=names)[1].splitlines()[0]
    on = notify.build_error_message(ON, message=msg, names=names)[1].splitlines()[0]
    check(f"{label} is hidden with Movie names off", all(n not in off for n in names), off)
    check(f"...and shown with it on", all(n in on for n in names), on)
check("the mount point survives redaction in the same sentence",
      "/library" in notify.build_error_message(OFF, message=CASES[0][1], names=[FILM])[1])

# The issue block's example follows the same opt-in (it is a path too).
LEAKY = [{"category": "delete_failed", "detail": f"{FILM}: denied"},
         {"category": "delete_failed", "detail": "/library/Movies/Heat (1995)/Heat.mkv: denied"}]
off = notify.build_error_message(OFF, message="Jellyfin stopped answering.", issues=LEAKY)[1]
check("the issue example is hidden too, and the counts still go out",
      "Eyes Wide Shut" not in off and "Deletion failed: 2 movies" in off, off)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
