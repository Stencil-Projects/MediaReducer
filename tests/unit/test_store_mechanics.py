"""db.py's own contracts: when the store is rebuilt, and how a reader knows
its copy is stale.

A mutation audit of db.py answered 7 of 19. It is the weakest-tested module in
the project, and the reason is that almost everything here is exercised
INDIRECTLY — a test writes rows and reads them back, which proves the round
trip and nothing about the machinery underneath it. That machinery decides
when to destroy the store and when a memoized reader may keep serving what it
has, and both failure directions are silent.

Pinned here, in the order they cost:

  • The damage probe's fail-open branches. Answering "damaged" for a store
    that is merely busy or unopenable hands a healthy store to the rebuild
    path, which deletes it. test_store_damage_recovery claims both of these,
    and reached neither: under WAL a reader is not blocked by a writer, so its
    "locked" case never fails the open, and its "cannot be opened" case uses a
    path that does not exist and returns at the absent-store line above. Both
    passed without entering the branch they name.
  • The generation counter and the fingerprint. Every memoized read in the app
    keys on these; a counter that stops moving serves a stale plan forever,
    and one that moves on every read throws the memo away.
  • The schema guard. It rebuilds the code-derived tables when db.py's own
    source changes. Rebuilding when nothing changed wipes the store on every
    connect; never rebuilding leaves rows in a layout the current code cannot
    read.
  • The queue's order. `ord` exists because the queue IS a deletion order —
    which movie goes first is the whole content of the plan.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-storemech.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import db  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


P = Path(_OUT) / "store.db"


def fresh():
    db.forget_initialized()
    for f in db.db_files(P):
        try: f.unlink()
        except FileNotFoundError: pass
    with db.transaction(P) as conn:
        db.set_meta(conn, "last_cleanup_date", "2026-08-01")
    return P


# ── The damage probe only ever answers False for PROVEN damage ─────────────
fresh()
check("a healthy store reads as readable", db.store_is_readable(P) is True)

# Unopenable for real: a DIRECTORY where the store should be. The path exists,
# so the absent-store shortcut does not fire, and sqlite raises OperationalError
# on the open — the branch that has to answer "undecidable", not "damaged".
_dirpath = Path(_OUT) / "as-a-directory.db"
_dirpath.mkdir(exist_ok=True)
try:
    sqlite3.connect(str(_dirpath)).execute("PRAGMA quick_check(1)")
    _raised = None
except Exception as e:
    _raised = e
check("a store path that is a directory really does fail the open",
      isinstance(_raised, sqlite3.OperationalError), repr(_raised))
check("...and an unopenable store is NOT called damaged",
      db.store_is_readable(_dirpath) is True)

# Busy: WAL readers are not blocked by a writer, so a held lock cannot produce
# this. The contract is about the ERROR, so the error is what is injected.
_real_connect = sqlite3.connect


def _locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")


sqlite3.connect = _locked
try:
    _busy = db.store_is_readable(P)
finally:
    sqlite3.connect = _real_connect
check("a store that answers 'database is locked' is NOT called damaged",
      _busy is True)


def _weird(*a, **k):
    raise MemoryError("out of memory")


sqlite3.connect = _weird
try:
    _unknown = db.store_is_readable(P)
finally:
    sqlite3.connect = _real_connect
check("...and neither is one that fails in a way nothing here anticipated",
      _unknown is True)

# The one answer that DOES destroy a store has to be reserved for real damage.
_bad = Path(_OUT) / "garbage.db"
_bad.write_bytes(b"this is not a database" * 40)
check("a file that is not a database at all IS called damaged",
      db.store_is_readable(_bad) is False)


# ── A reset that could not finish says so ──────────────────────────────────
# Clear Cache reports what this returns. A reset that left the .db behind and
# reported success leaves the old plan in place under a UI that says it is gone.
fresh()
_real_unlink = Path.unlink


def _refuse(self, *a, **k):
    if self.name.endswith(".db"):
        raise PermissionError(13, "Read-only file system")
    return _real_unlink(self, *a, **k)


Path.unlink = _refuse
try:
    db.reset_store(P)
    _raised = None
except OSError as e:
    _raised = e
finally:
    Path.unlink = _real_unlink
check("a reset that cannot remove the store raises rather than reporting success",
      isinstance(_raised, OSError) and "store.db" in str(_raised), repr(_raised))
check("...and the store it failed to remove is still there", P.exists())


# ── The generation counter: the app's only reliable change signal ──────────
fresh()
_g0 = db.data_generation(P)
with db.transaction(P) as conn:
    db.set_meta(conn, "probe", 1)
_g1 = db.data_generation(P)
check("a transaction that changed something advances the generation", _g1 > _g0,
      (_g0, _g1))

with db.transaction(P) as conn:
    db.get_meta(conn, "probe")          # a read, inside a write transaction
_g2 = db.data_generation(P)
check("a transaction that changed nothing does NOT advance it — the memo is "
      "the point", _g2 == _g1, (_g1, _g2))

_absent = Path(_OUT) / "never-created.db"
check("reading the generation of an absent store answers 0",
      db.data_generation(_absent) == 0)
check("...without creating it — a status poll must not conjure a store",
      not _absent.exists())

# The fingerprint pairs the counter with the file's identity, so a store that
# was deleted and rebuilt reads as new even if the counter restarts at the
# same number. That is exactly what Clear Cache does.
# A store that has been used has a generation above 1; a reset restarts it at
# 1, so the counter alone already reports the swap. The file-identity half
# (device, inode, size) is defence in depth and cannot be isolated here: an
# inode freed by the unlink is handed straight back to the file that replaces
# it, so a store rebuilt to the same size lands on the SAME identity. What is
# worth pinning is the outcome — Clear Cache is never invisible to a reader
# that memoized the old contents.
fresh()
for i in range(4):
    with db.transaction(P) as conn:
        db.set_meta(conn, "probe", i)
_f_before = db.data_fingerprint(P)
db.reset_store(P)
db.forget_initialized()
with db.transaction(P) as conn:
    db.set_meta(conn, "last_cleanup_date", "2026-08-01")
check("a Clear Cache is never invisible to a reader holding the old contents",
      db.data_fingerprint(P) != _f_before, (_f_before, db.data_fingerprint(P)))


# ── The schema guard: rebuild only when db.py's own shape changed ──────────
def seed_rows():
    with db.transaction(P) as conn:
        db.ensure_code_current(conn, "abc123")
        db.replace_movies(conn, [{"path": "/lib/A.mkv", "title": "A", "size_bytes": 1}])
        db.set_meta(conn, "last_cleanup_date", "2026-08-01")


def movie_count():
    with db.connect(P) as conn:
        return len(conn.execute("SELECT path FROM movies").fetchall())


fresh()
seed_rows()
check("the seeded row is there", movie_count() == 1)

# Reconnect with the SAME fingerprint: nothing is rebuilt.
db.forget_initialized()
check("an unchanged db.py leaves the store alone on reconnect", movie_count() == 1)

# Now the fingerprint moves, as it would after an edit to db.py.
# Read the counter BEFORE the fingerprint moves: data_generation opens a
# connection, and opening one is what runs the guard.
_g_before = db.data_generation(P)
_real_fp = db._SCHEMA_FINGERPRINT
db._SCHEMA_FINGERPRINT = "a-different-db-py"
db.forget_initialized()
try:
    check("a changed db.py rebuilds the code-derived tables", movie_count() == 0)
    with db.connect(P) as conn:
        # Two mechanisms deliver this today — the key is not in the set the
        # rebuild deletes, AND the rebuild saves and restores it — so no
        # single-line change can falsify it. The property is worth holding
        # whichever one is carrying it: losing the date lets a second
        # deleting run happen the same day.
        check("...but the daily-run date survives it — losing it would let a "
              "second deleting run happen the same day",
              db.get_meta(conn, "last_cleanup_date") == "2026-08-01")
    check("...and the rebuild advances the generation, so a memoized reader "
          "cannot keep serving the pre-rebuild snapshot",
          db.data_generation(P) > _g_before, (_g_before, db.data_generation(P)))
finally:
    db._SCHEMA_FINGERPRINT = _real_fp
    db.forget_initialized()


# ── The queue is an ORDER, not a set ───────────────────────────────────────
# `ord` is what makes reading it back give the same deletion order it was
# planned in. Without it the plan still holds the same movies and deletes a
# different one first.
fresh()
ORDER = ["/lib/first.mkv", "/lib/second.mkv", "/lib/third.mkv", "/lib/fourth.mkv"]
with db.transaction(P) as conn:
    db.set_meta(conn, "pending_schema", 1)     # the stamp a real plan write leaves
    db.replace_queue(conn, {p: {"title": Path(p).stem, "score": 10.0,
                                "size_bytes": 1} for p in ORDER})
doc = db.read_pending_doc(P)
check("the queue reads back in exactly the order it was written",
      list(doc.get("entries") or {}) == ORDER, list(doc.get("entries") or {}))

# ...and a rewrite in a different order takes, rather than keeping the old one.
with db.transaction(P) as conn:
    db.replace_queue(conn, {p: {"title": Path(p).stem, "score": 10.0,
                                "size_bytes": 1} for p in reversed(ORDER)})
check("...and re-planning in a new order replaces the old one",
      list(db.read_pending_doc(P).get("entries") or {}) == list(reversed(ORDER)),
      list(db.read_pending_doc(P).get("entries") or {}))

# The two checks above cannot fail on `ord` alone: replace_queue writes rows in
# the same order it numbers them, so rowid order agrees with `ord` and a reader
# that dropped its ORDER BY would still look right. Rows are inserted here in a
# DIFFERENT order from their `ord` to separate the two, which is the case a
# store rewritten in place can really produce.
with db.transaction(P) as conn:
    db.set_meta(conn, "pending_schema", 1)
    conn.execute("DELETE FROM queue")
    for path, o in (("/lib/third.mkv", 2), ("/lib/first.mkv", 0), ("/lib/second.mkv", 1)):
        conn.execute("INSERT INTO queue(path, ord, title) VALUES(?,?,?)",
                     (path, o, Path(path).stem))
check("the queue is read by ord, not by the order the rows happen to sit in",
      list(db.read_pending_doc(P).get("entries") or {})
      == ["/lib/first.mkv", "/lib/second.mkv", "/lib/third.mkv"],
      list(db.read_pending_doc(P).get("entries") or {}))

shutil.rmtree(_OUT, ignore_errors=True)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
