"""The one thing this program must never get wrong: what it is allowed to unlink.

is_safe_to_delete() is the last gate before path.unlink(). Everything upstream —
scoring, protection, the queue, the delay clock — decides what SHOULD go; this
decides what CAN. A regression here deletes someone's films, so it gets an
adversarial matrix rather than incidental coverage from the run tests.

The first case is load-bearing in a way that is easy to miss: it asserts a real
file IS deletable. Without it every other row passes for free the moment the
monitored-root list comes back empty, which is exactly what a first draft of
this file did — MONITOR_DIRS is loaded at run start, not at import, so a test
that only sets config.json arms nothing and proves nothing.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_TD = tempfile.mkdtemp(prefix="mr-boundary.")
_lib = Path(_TD, "library")
(_lib / "movies" / "Keep (2020)").mkdir(parents=True)
_outside = Path(_TD, "outside"); _outside.mkdir()
_cfg = Path(_TD, "config.json"); _cfg.write_text(json.dumps({"OUTPUT_DIR": _TD}))
os.environ["MEDIAREDUCER_CONFIG"] = str(_cfg)
os.environ["MEDIAREDUCER_LIBRARY"] = str(_lib)
sys.path.insert(0, str(ROOT))
import engine as E

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {extra}"))
    ok = ok and cond

# A run resolves MONITOR_DIRS into engine globals; do the same rather than
# relying on import-time config loading, which does not happen.
E.MONITOR_DIRS = ["movies"]
E._RESOLVED_MONITORED_ROOTS = None
check("the guard is armed (monitored roots resolved)",
      E.monitored_roots() == [_lib / "movies"], E.monitored_roots())

real = _lib / "movies" / "Keep (2020)" / "keep.mkv"; real.write_text("x")
victim = _outside / "precious.mkv"; victim.write_text("x")
sym = _lib / "movies" / "Keep (2020)" / "link.mkv"; sym.symlink_to(victim)
symdir = _lib / "movies" / "linkdir"; symdir.symlink_to(_outside)
unmon = _lib / "other"; unmon.mkdir()
stray = unmon / "stray.mkv"; stray.write_text("x")
hard = _lib / "movies" / "Keep (2020)" / "hard.mkv"; os.link(victim, hard)
escape = _lib / "movies" / "Keep (2020)" / ".." / ".." / ".." / "outside" / "precious.mkv"

# Isolating cases. The guard has three arms — no symlinks, inside the library,
# inside a monitored subtree — and the obvious adversarial paths are all caught
# by the LAST one, so deleting either of the others left this file still passing.
# These two are refused by exactly one arm each.
#   1. A symlink whose target is a perfectly legitimate file in the same
#      monitored folder: inside the library, inside a monitored dir, refused
#      only because deleting a link is not deleting a film.
inner_link = _lib / "movies" / "Keep (2020)" / "inner.mkv"
inner_link.symlink_to(real)
#   2. A monitored folder that is itself a symlink pointing off the library —
#      someone pointing "Movies" at a share. Its resolved root sits outside, so
#      is_under_monitored_dir says yes and only the containment check says no.
_offsite = Path(_TD, "offsite"); _offsite.mkdir()
_offsite_film = _offsite / "film.mkv"; _offsite_film.write_text("x")
(_lib / "viashare").symlink_to(_offsite)

CASES = [
    # The row that keeps the rest honest.
    ("a real file in a monitored dir is deletable", real, True),
    # Everything the boundary exists to refuse.
    ("a file outside the library is refused", victim, False),
    ("a symlink inside the library is refused", sym, False),
    ("a file reached through a symlinked directory is refused",
     symdir / "precious.mkv", False),
    ("a ../ escape out of the library is refused", escape, False),
    ("a real file in an UNMONITORED library dir is refused", stray, False),
    ("the library root itself is refused", _lib, False),
    ("None is refused", None, False),
    # Allowed, and correct: unlinking a hard link drops one directory entry —
    # the file outside the library keeps its own and survives.
    ("a hard link to an outside file is allowed", hard, True),
    ("a symlink to a legitimate file beside it is refused", inner_link, False),
]
for label, path, want in CASES:
    check(label, E.is_safe_to_delete(path) is want)

# The symlinked monitored folder needs its own arming, since the root list is
# keyed on MONITOR_DIRS.
E.MONITOR_DIRS = ["viashare"]
E._RESOLVED_MONITORED_ROOTS = None
check("a symlinked monitored folder resolves off the library",
      E._resolved_monitored_roots() == [_offsite.resolve()], E._resolved_monitored_roots())
check("...and a real file under it is still refused",
      E.is_safe_to_delete(_lib / "viashare" / "film.mkv") is False)
E.MONITOR_DIRS = ["movies"]
E._RESOLVED_MONITORED_ROOTS = None

# With nothing configured, nothing qualifies — the documented safe no-op.
E.MONITOR_DIRS = []
E._RESOLVED_MONITORED_ROOTS = None
check("no monitored dirs means nothing is deletable", E.is_safe_to_delete(real) is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
