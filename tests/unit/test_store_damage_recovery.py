"""A damaged store must never become a permanent fail state.

The store is opened early in every run, so a malformed page killed the engine
with an unhandled sqlite3.DatabaseError — and since nothing repaired it, every
following run died the same way. Worse, the app itself survives (its reads
swallow errors and read empty), so the UI just kept asking for a Simulate that
could never succeed.

Everything in the store is reproducible by a Simulate, so the fix is to detect
an unreadable store and rebuild it empty — at startup and again before every
run launch, since damage can appear mid-session (a full disk, a power loss).
Deletion history, run logs and config.json live outside the store.
"""
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-damage.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A
import db

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


def seed_store():
    """A real store with content, so corruption has something to damage."""
    p = A.db_path()
    db.reset_store(p)          # also drops the schema-init memo for this path
    with db.transaction(p) as conn:
        db.set_meta(conn, "last_cleanup_date", "2026-08-01")
        db.set_meta(conn, "dashboard_stats", {"library_gb": 123.0})
    return p


def corrupt(p, *, mode="page"):
    if mode == "page":
        d = bytearray(Path(p).read_bytes())
        mid = max(len(d) // 2, 1)
        d[mid:mid + 2048] = b"\xde\xad\xbe\xef" * 512
        Path(p).write_bytes(bytes(d))
    elif mode == "truncated":          # a write cut short by a full disk
        d = Path(p).read_bytes()
        Path(p).write_bytes(d[:len(d) // 3])
    else:                              # not a database at all
        Path(p).write_bytes(b"this is not a sqlite file\n" * 40)


# ── The readability probe ────────────────────────────────────────────────────
p = seed_store()
check("a healthy store reads as readable", db.store_is_readable(p) is True)
check("an ABSENT store reads as readable (nothing is wrong with it)",
      db.store_is_readable(Path(_OUT, "no-such.db")) is True)

for _mode in ("page", "truncated", "garbage"):
    seed_store()
    corrupt(p, mode=_mode)
    check(f"a {_mode}-damaged store reads as unreadable",
          db.store_is_readable(p) is False)


# ── Recovery rebuilds it, and only when it is actually damaged ───────────────
seed_store()
_untouched = A._rebuild_store_if_damaged(reason="test")
with db.connect(p) as conn:
    _kept = db.get_meta(conn, "last_cleanup_date")
check("a healthy store is left alone (no needless wipe)",
      _untouched is False and _kept == "2026-08-01")

seed_store()
corrupt(p, mode="page")
check("a damaged store is rebuilt", A._rebuild_store_if_damaged(reason="test") is True)
check("the rebuilt store is usable again", db.store_is_readable(p) is True)
with db.connect(p) as conn:
    check("the rebuilt store drops the library snapshot (a Simulate refills it)",
          db.get_meta(conn, "dashboard_stats") is None)
    # The once-per-day cleanup window lived in the store too, so the rebuild
    # handed today's slot back — the same free catch-up run a restart would have
    # granted. It is re-burned for the same reason, not left open.
    check("the rebuild re-burns today's daily-run window",
          db.get_meta(conn, "last_cleanup_date") == time.strftime("%Y-%m-%d"))

# The app's own reads work against the rebuilt store rather than raising.
check("app reads work after the rebuild",
      A.pending_count() == 0 and A.library_stats() == {})


# ── Why a structural check, not "wait for a query to fail" ──────────────────
# Damage doesn't announce itself on every read: on a small store a corrupted
# region can sit in pages a given query never touches, so the query succeeds
# while the file is still broken — and a later, larger read (the engine's, mid
# run) is the one that dies. quick_check sees the damage either way, which is
# why the guard uses it rather than trusting a probe query.
seed_store()
corrupt(p, mode="page")
_query_survived = True
try:
    with db.connect(p) as conn:
        conn.execute("SELECT * FROM meta").fetchall()
except Exception:
    _query_survived = False
check("damage is caught even when a sample query happens to survive",
      db.store_is_readable(p) is False)
print(f"   (for reference: the sample query "
      f"{'survived' if _query_survived else 'raised'} on this store)")
A._rebuild_store_if_damaged(reason="test cleanup")


# ── Only PROVEN damage counts as damage ─────────────────────────────────────
# The probe has to answer "is this file broken", not "did opening it work".
# Busy, locked, permission-denied and out-of-space all fail the open while
# saying nothing about the contents — and answering False there hands a
# perfectly good store to the rebuild path, which deletes it. One transient
# lock would silently throw away the library snapshot and force a re-scan.
seed_store()
_held = sqlite3.connect(str(p), isolation_level=None)
_held.execute("BEGIN EXCLUSIVE")
check("a store locked by another writer is NOT called damaged",
      db.store_is_readable(p) is True)
check("and so it is not rebuilt out from under that writer",
      A._rebuild_store_if_damaged(reason="test") is False)
_held.execute("ROLLBACK")
_held.close()
with db.connect(p) as conn:
    check("the locked store still holds its contents",
          db.get_meta(conn, "last_cleanup_date") == "2026-08-01")

check("a store that cannot be opened at all is NOT called damaged",
      db.store_is_readable(Path(_OUT, "no-such-dir", "x.db")) is True)

# ── A rebuild never runs while the engine has the store open ────────────────
# The pre-run check has to sit INSIDE the run lock and past the busy guards:
# unlinking the file under a live engine subprocess breaks the run it is
# trying to protect.
seed_store()
corrupt(p, mode="page")
A._run_active = True
_started, _msg = A.run_script()
check("a run is refused while another is active, without touching the store",
      _started is False and db.store_is_readable(p) is False)
A._run_active = False
A._rebuild_store_if_damaged(reason="test cleanup")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
