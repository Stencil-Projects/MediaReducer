"""What delete_candidate does when the filesystem says no.

The happy path — a real file, unlinked, logged, counted — is exercised by every
run test there is. Coverage found that none of the unhappy ones were: the file
vanishing between the scan and the unlink, the unlink being refused, the stat
that measures what was freed failing, and a Stop arriving mid-deletion.

Each of those decides a NUMBER the run then reports, and the invariant they all
serve is one thing: the counters and deleted.log must say the same. A deletion
that half-happened and is counted as whole leaves the dashboard claiming space
the disk does not have; one that happened and is not counted leaves the run
believing it still needs to free it, and taking another film to get there.
"""
import errno
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E  # noqa: E402
import _tmpout  # noqa: E402

BASE = Path(tempfile.mkdtemp(prefix="mr-delfail."))
LIB = BASE / "lib"
(LIB / "Movies").mkdir(parents=True)
E.LIBRARY_ROOT = LIB
E.MONITOR_DIRS = [str(LIB / "Movies")]
_tmpout.redirect_engine(E, BASE / "out")
E.OUTPUT_DIR.mkdir(exist_ok=True)
E.LOGFILE = E.OUTPUT_DIR / "lastrun.log"
E.LOGFILE.write_text("", encoding="utf-8")
E.RUN_MODE = "cleanup"
E.cleanup_radarr = lambda c: None

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


logged, issues, deleted_log = [], [], []
E.log = lambda msg, *a, **k: logged.append(str(msg))
E.record_issue = lambda kind, detail="": issues.append((kind, detail))
E.log_deleted = lambda title, path, size, **kw: deleted_log.append((title, str(path), size))

n = 0


def candidate(size=4096, exists=True):
    """A deletable candidate with a real file under a monitored dir."""
    global n
    n += 1
    folder = LIB / "Movies" / f"Film {n} (2020)"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Film {n}.mkv"
    if exists:
        path.write_bytes(b"x" * size)
    del logged[:], issues[:], deleted_log[:]
    return {"path": path, "title": f"Film {n}", "retention_score": 12.5,
            "imdb_rating": 6.0, "imdb_votes": 900, "release_year": 2020,
            "play_count": 0, "last_played": None, "file_size": size,
            "added_at": 1600000000}


# ── The ordinary deletion, so the failures below are read against it ────────
c = candidate(size=4096)
out = E.delete_candidate(c)
check("a real file is deleted and reported as deleted", out is True and not c["path"].exists())
check("...and deleted.log records the size the DISK had, not the scan's",
      deleted_log == [(c["title"], str(c["path"]), 4096)], deleted_log)
check("...and the now-empty folder is tidied away", not c["path"].parent.exists())

# ── The file went away between the scan and the unlink ──────────────────────
# Someone deleted it in Plex, or a second run got there first. Nothing was
# freed BY THIS CALL, so nothing may be counted: the space is already back in
# the free figure the next measurement reads, and counting it again would have
# the run believe it had freed twice what it did.
c = candidate(exists=False)
out = E.delete_candidate(c)
check("a file already gone reports False, not a deletion", out is False)
check("...and writes nothing to deleted.log", not deleted_log, deleted_log)
check("...and says so in the log", any("already gone" in m for m in logged), logged)

# ── The unlink itself is refused ────────────────────────────────────────────
# A read-only mount, a permission the container does not have, an immutable
# flag. The file is still there afterwards and the run has to know: this is the
# failure that repeats every night until someone is told about it.
c = candidate()
real_unlink = Path.unlink


def _refuse(self, *a, **kw):
    raise PermissionError(errno.EACCES, "Read-only file system")


Path.unlink = _refuse
try:
    out = E.delete_candidate(c)
finally:
    Path.unlink = real_unlink
check("a refused unlink reports False", out is False)
check("...leaves the file where it was", c["path"].is_file())
check("...records an issue naming the path and the reason",
      len(issues) == 1 and issues[0][0] == "delete_failed"
      and str(c["path"]) in issues[0][1] and "Read-only" in issues[0][1], issues)
check("...and writes nothing to deleted.log", not deleted_log, deleted_log)
real_unlink(c["path"])

# ── The stat that measures what was freed fails ─────────────────────────────
# The unlink still has to happen — a file that cannot be measured is not a file
# that must be kept — and the scan's figure stands in for the size.
#
# The safety gate stats the same path a moment earlier, so it is stubbed out
# here rather than made to fail too: what is under test is the size fallback,
# and its own test above already proves the gate refuses on its own terms.
real_stat = Path.stat
real_safe = E.is_safe_to_delete
target = None


def _no_stat(self, *a, **kw):
    if self == target:
        raise OSError(errno.EIO, "I/O error")
    return real_stat(self, *a, **kw)


def unmeasurable(cand):
    """Delete `cand` with its size unreadable, and return deleted.log's line."""
    global target
    target = cand["path"]
    Path.stat, E.is_safe_to_delete = _no_stat, lambda p: True
    try:
        return E.delete_candidate(cand)
    finally:
        Path.stat, E.is_safe_to_delete = real_stat, real_safe


c = candidate(size=8192)
out = unmeasurable(c)
check("a file that cannot be stat'd is still deleted", out is True and not c["path"].exists())
check("...and falls back to the size the scan measured",
      deleted_log == [(c["title"], str(c["path"]), 8192)], deleted_log)

# A negative scan size is not a size: recording it would SUBTRACT from what the
# run reports freeing. Unknown is the honest answer, and deleted.log takes it.
#
# It is also the only junk that gets this far. The "Selected for deletion" line
# above formats candidate['file_size'] as GB, so a None or a string has already
# raised by here — the int()/TypeError arm of the fallback cannot be reached.
c = candidate(size=4096)
c["file_size"] = -1
unmeasurable(c)
check("a negative scan size records no size rather than a wrong one",
      deleted_log and deleted_log[0][2] is None, deleted_log)

# ── A Stop arriving mid-deletion ────────────────────────────────────────────
# SIGTERM between unlink() and the deleted.log append is deferred so the record
# is written; the exit then happens on the way out. The file is gone and the
# history knows it — the opposite order loses the run's last deletion.
c = candidate()
E._SIGTERM_DEFERRED = True
try:
    E.delete_candidate(c)
    stopped = False
except SystemExit as e:
    stopped = e.code == 143
finally:
    E._SIGTERM_DEFERRED = False
check("a Stop during the unlink exits with the signal's code", stopped)
check("...after the file is actually gone", not c["path"].exists())
check("...and after deleted.log has the line", len(deleted_log) == 1, deleted_log)

# ── The safety gate still wins over all of it ──────────────────────────────
c = candidate()
E.MONITOR_DIRS = [str(BASE / "somewhere-else")]
try:
    out = E.delete_candidate(c)
finally:
    E.MONITOR_DIRS = [str(LIB / "Movies")]
check("a path outside every monitored dir is refused before any of the above",
      out is False and c["path"].is_file())
check("...and says which check stopped it",
      any("safety_check_failed" in m for m in logged), logged)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
