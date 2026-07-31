"""The last line of defense before an unlink, exercised against real files.

Everything upstream — eligibility, scoring, protection, the delay clock, the
plan stamp — decides what SHOULD be deleted. These are the checks that decide
what MAY be, and they are the ones that have to hold when something upstream is
wrong. They had no direct test.

The assertions are deliberately the boring direction: a real file is created on
disk and the test asserts it is STILL THERE afterwards. A guard that silently
stopped working would leave every other test passing, because every other test
is about choosing correctly rather than about the consequence of choosing
wrongly.

Covered here: the path gate (is_safe_to_delete), the dry-run gate inside
delete_candidate, and the folder tidy-up, which is the only other call that can
remove something from the library.
"""
import os
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


# A real library on disk: /lib is the boundary, /lib/Movies is monitored,
# /lib/Other is inside the boundary but NOT monitored.
BASE = Path(tempfile.mkdtemp(prefix="mr-guards."))
LIB = BASE / "lib"
(LIB / "Movies" / "A Film (2001)").mkdir(parents=True)
(LIB / "Other").mkdir(parents=True)
(BASE / "outside").mkdir(parents=True)

INSIDE = LIB / "Movies" / "A Film (2001)" / "A Film (2001).mkv"
UNMONITORED = LIB / "Other" / "Other Film.mkv"
OUTSIDE = BASE / "outside" / "Not Ours.mkv"
for f in (INSIDE, UNMONITORED, OUTSIDE):
    f.write_bytes(b"x" * 32)
LINK = LIB / "Movies" / "A Film (2001)" / "link.mkv"
LINK.symlink_to(INSIDE)

E.LIBRARY_ROOT = LIB
E.MONITOR_DIRS = [str(LIB / "Movies")]
E.OUTPUT_DIR = BASE / "out"
E.OUTPUT_DIR.mkdir(exist_ok=True)
E.LOGFILE = E.OUTPUT_DIR / "lastrun.log"
E.LOGFILE.write_text("", encoding="utf-8")

# ── The path gate ───────────────────────────────────────────────────────────
check("a real file under a monitored root may be deleted", E.is_safe_to_delete(INSIDE))
check("a symlink is never deletable, even pointing at a deletable file",
      not E.is_safe_to_delete(LINK))
check("a file inside the library but OUTSIDE every monitored root is not deletable",
      not E.is_safe_to_delete(UNMONITORED))
check("a file outside the library boundary is not deletable",
      not E.is_safe_to_delete(OUTSIDE))
check("a path escaping the boundary with .. is not deletable",
      not E.is_safe_to_delete(LIB / "Movies" / ".." / ".." / "outside" / "Not Ours.mkv"))
check("None is not deletable", not E.is_safe_to_delete(None))
# The empty allow-list is a deliberate no-op: manage nothing until told to.
_saved_dirs = E.MONITOR_DIRS
E.MONITOR_DIRS = []
check("with no monitored directories, nothing is deletable at all",
      not E.is_safe_to_delete(INSIDE))
E.MONITOR_DIRS = _saved_dirs


def candidate(path):
    return {"path": path, "title": "A Film", "retention_score": 1.0,
            "imdb_rating": None, "imdb_votes": None, "release_year": 2001,
            "play_count": 0, "last_played": 0, "file_size": 32, "added_at": 0}


# ── The dry-run gate ────────────────────────────────────────────────────────
# Simulate and Debug Cleanup both carry the debug_ prefix; neither may unlink.
for mode in ("debug_sim", "debug_cleanup"):
    E.RUN_MODE = mode
    deleted = E.delete_candidate(candidate(INSIDE))
    check(f"{mode} reports no deletion", deleted is False)
    check(f"{mode} leaves the file on disk", INSIDE.exists())

# ── The gates hold on a LIVE run ────────────────────────────────────────────
E.RUN_MODE = "headroom"
for name, path in [("a symlink", LINK), ("an unmonitored file", UNMONITORED),
                   ("a file outside the library", OUTSIDE)]:
    before = path.exists()
    deleted = E.delete_candidate(candidate(path))
    check(f"a live run refuses to delete {name}", deleted is False)
    check(f"...and {name} is still on disk", path.exists() == before, str(path))

# Only when everything is right does the file actually go — otherwise these
# tests would pass against a delete_candidate that never deletes anything.
E.RUN_MODE = "headroom"
check("a live run DOES delete a legitimate candidate",
      E.delete_candidate(candidate(INSIDE)) is True)
check("...and the file is really gone", not INSIDE.exists())

# ── Folder tidy-up may not climb out of a movie folder ──────────────────────
movie_dir = LIB / "Movies" / "A Film (2001)"
E.remove_empty_movie_folder(movie_dir / "gone.mkv")
check("a folder still holding files is kept", movie_dir.exists(), "symlink remains in it")
LINK.unlink()
E.remove_empty_movie_folder(movie_dir / "gone.mkv")
check("an empty movie folder is removed", not movie_dir.exists())

# The roots themselves are never removable, even when empty.
(LIB / "Movies" / "tmp").mkdir(exist_ok=True)
(LIB / "Movies" / "tmp").rmdir()
E.remove_empty_movie_folder(LIB / "Movies" / "anything.mkv")
check("a monitored root is never removed, even when empty", (LIB / "Movies").exists())
E.remove_empty_movie_folder(LIB / "anything.mkv")
check("the library root is never removed", LIB.exists())

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
