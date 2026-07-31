"""Queue/snapshot upkeep safety: (1) files confirmed physically GONE are pruned
from the snapshot and queue by every upkeep path; (2) an OFFLINE library (NAS
down, mount vanished) must never read as "everything was deleted" — every upkeep
and scan path fails closed until the mount returns."""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
import db
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.log_stage = lambda *a, **k: None
E.log_blank = lambda *a, **k: None
E.SCORE_BALANCE = 0
E.HISTORY_WEIGHT, E.QUALITY_WEIGHT = E.score_balance_weights(0)
E.MAX_STALENESS_MONTHS = 36
E.fetch_protected_paths = lambda: ([], None, None, None)
E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
E.USE_JELLYFIN = False
E.PROTECT_JELLYFIN_FAVORITES = False

MB = 1_000_000


def _snap_paths():
    return {m["path"] for m in (E.load_cache().get("library_snapshot") or {}).get("movies") or []}


def _queue_paths():
    return set((E.load_cache().get("pending") or {}).get("entries", {}))


def _built_at():
    return (E.load_cache().get("library_snapshot") or {}).get("built_at")


def _seed_disk_snapshot_queue(td, *, marked_prefix=2):
    """Four real 2 MB movies, a matching library snapshot (each 0 plays) and a queue
    with the first `marked_prefix` marked. Returns the paths in plan order."""
    lib = Path(td, "library"); movies = lib / "movies"
    paths = []
    for n in ("A", "B", "C", "D"):
        d = movies / f"Movie {n}"; d.mkdir(parents=True)
        f = d / f"Movie {n}.mkv"; f.write_bytes(b"\0" * (2 * MB))
        paths.append(f)
    out = Path(td, "out"); out.mkdir()
    E.LIBRARY_ROOT = lib
    E.CHECK_PATH = lib
    E._RESOLVED_MONITORED_ROOTS = None
    E.MONITOR_DIRS = [str(movies)]
    E.OUTPUT_DIR = out
    E.LOGFILE = out / "lastrun.log"
    E.DELETED_LOG = out / "deleted.log"
    E.PROGRESS_FILE = out / "progress.json"
    E.DB_FILE = out / "mediareducer.db"
    entries = {str(p): {"title": p.stem, "score": i + 1.0, "size_bytes": 2 * MB,
                        "marked_at": (1_000_000_000 + i) if i < marked_prefix else None}
               for i, p in enumerate(paths)}
    snap = [E._snapshot_entry(p.stem, 2000, 6.0, 1000, 0, 0, 0, 1_500_000_000, 2 * MB,
                              source_id=p.stem, path=str(p)) for p in paths]
    _dbstate.seed(E.DB_FILE, {
        "code_checksum": E.code_checksum(),
        "library_snapshot": {"movies": snap, "built_at": 1},
        "pending": {"schema": 1, "entries": entries},
    })
    return paths


# ── 1. db.delete_movies removes only the named rows ───────────────────────────
with tempfile.TemporaryDirectory() as td:
    _seed_disk_snapshot_queue(td)
    all_paths = _snap_paths()
    victim = sorted(all_paths)[2]
    with db.transaction(E.DB_FILE) as conn:
        db.delete_movies(conn, [victim])
    after = _snap_paths()
    check("delete_movies drops the named snapshot row", victim not in after)
    check("delete_movies leaves the other rows", after == all_paths - {victim})
    # Empty / None input is a harmless no-op.
    with db.transaction(E.DB_FILE) as conn:
        db.delete_movies(conn, [])
        db.delete_movies(conn, None)
    check("delete_movies([]) / (None) is a no-op", _snap_paths() == after)

# ── 2. save_pending(snapshot_delete_paths=…): atomic queue + snapshot prune ────
with tempfile.TemporaryDirectory() as td:
    paths = _seed_disk_snapshot_queue(td)
    gone = str(paths[2])
    store = dict((E.load_cache().get("pending") or {}).get("entries", {}))
    store.pop(gone, None)                       # caller already dropped it from the queue
    E.save_pending(store, snapshot_delete_paths={gone})
    check("save_pending prunes the gone row from the snapshot", gone not in _snap_paths())
    check("save_pending drops it from the queue too", gone not in _queue_paths())
    check("save_pending keeps the surviving snapshot rows",
          _snap_paths() == {str(p) for p in paths} - {gone})
    check("save_pending preserves built_at (no rescan)", _built_at() == 1)

# ── 3. The 15-minute tick prunes a marked movie whose file is gone ────────────
with tempfile.TemporaryDirectory() as td:
    paths = _seed_disk_snapshot_queue(td)
    E.REDLINE_ONLY_MODE = False; E.HEADROOM_GB = 100; E.REDLINE_GB = None
    E._snapshot_by_store_key = lambda store: {
        k: {"source_id": Path(k).stem, "jf_source_id": None, "plays": 0, "last_played": 0}
        for k in store}
    E._fresh_watch_data = lambda ids: {str(i): {"play_count": 0, "last_played": 0,
                                                 "favorite": False} for i in ids}
    gone = paths[0]                             # Movie A, a MARKED entry
    gone.unlink()                               # vanish it outside MediaReducer
    E._revalidate_pending_marks(0)             # within limits: upkeep still runs
    check("tick drops the gone file from the queue", str(gone) not in _queue_paths())
    check("tick prunes the gone file from the snapshot too", str(gone) not in _snap_paths())
    check("tick keeps the surviving snapshot rows",
          _snap_paths() == {str(p) for p in paths[1:]})
    check("tick preserves built_at (a watch refresh, not a rescan)", _built_at() == 1)

# ── 4. The redline fast path prunes an already-gone queued file ───────────────
with tempfile.TemporaryDirectory() as td:
    paths = _seed_disk_snapshot_queue(td, marked_prefix=4)
    E.RUN_MODE = "headroom"
    E.REDLINE_ONLY_MODE = True
    E.HEADROOM_GB = 0; E.REDLINE_GB = 200
    E._PLAN_CONFIG_RAW = {k: None for k in E._PLAN_CONFIG_KEYS}
    E._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200, "REDLINE_ONLY_MODE": True})
    # Re-stamp the queue so the fast path treats the plan as current.
    store = dict((E.load_cache().get("pending") or {}).get("entries", {}))
    E.save_pending(store, stamp_thresholds=True)
    E._snapshot_by_store_key = lambda store: {
        k: {"source_id": Path(k).stem, "jf_source_id": None, "plays": 0, "last_played": 0}
        for k in store}
    E._fresh_watch_data = lambda ids: {str(i): {"play_count": 0, "last_played": 0,
                                                "favorite": False} for i in ids}
    gone = paths[0]                             # first in plan order — already gone
    gone.unlink()
    handled = E._redline_fast_path(3 * MB)      # needs ~2 files; skips the dead one
    check("fast path handled the emergency", handled is True)
    check("fast path pruned the already-gone file from the snapshot",
          str(gone) not in _snap_paths())
    check("fast path left it out of the queue", str(gone) not in _queue_paths())


# ══════════════════════════════════════════════════════════════════════════
# A storage outage must NOT be read as "every movie was deleted." When the
# /library volume (or every monitored branch) is unmounted, every file stats as
# missing — and without a guard the 15-minute upkeep drops the whole marked queue
# and prunes every snapshot row, a full scan overwrites the snapshot with empty,
# and the fast path clears the queue. All of that is real data loss on a routine
# NAS-offline event.
# 
# Guards under test:
#   • _revalidate_pending_marks keeps the queue + snapshot when the monitored root
#     is absent, but still prunes a genuinely-gone file when the root IS mounted.
#   • _write_library_snapshot([]) keeps the last good snapshot while unmounted.
#   • build_candidates aborts (fail closed) instead of scanning an unmounted library.
# 
# Hermetic: temp DB + temp library; 'unmount' = remove the monitored dir.
# ══════════════════════════════════════════════════════════════════════════

E.log = lambda *a, **k: None
E.log_stage = lambda *a, **k: None
E.log_blank = lambda *a, **k: None
E.emit_progress = lambda *a, **k: None
E.SCORE_BALANCE = 0
E.HISTORY_WEIGHT, E.QUALITY_WEIGHT = E.score_balance_weights(0)
E.MAX_STALENESS_MONTHS = 36
E.fetch_protected_paths = lambda: (set(), set(), set(), set())
E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
E.USE_JELLYFIN = False
E.PROTECT_JELLYFIN_FAVORITES = False
E.REDLINE_ONLY_MODE = False
E.HEADROOM_GB = 100
E.REDLINE_GB = None
MB = 1_000_000


def _snap_paths():
    return {m["path"] for m in (E.load_cache().get("library_snapshot") or {}).get("movies") or []}

def _queue_paths():
    return set((E.load_cache().get("pending") or {}).get("entries", {}))


def _seed(td):
    lib = Path(td, "library"); movies = lib / "movies"
    paths = []
    for n in ("A", "B", "C", "D"):
        d = movies / f"Movie {n}"; d.mkdir(parents=True)
        f = d / f"Movie {n}.mkv"; f.write_bytes(b"\0" * (2 * MB))
        paths.append(f)
    out = Path(td, "out"); out.mkdir()
    E.LIBRARY_ROOT = lib
    E.CHECK_PATH = lib
    E._RESOLVED_MONITORED_ROOTS = None
    E.MONITOR_DIRS = [str(movies)]
    E.OUTPUT_DIR = out
    E.DB_FILE = out / "mediareducer.db"
    entries = {str(p): {"title": p.stem, "score": i + 1.0, "size_bytes": 2 * MB,
                        "marked_at": None} for i, p in enumerate(paths)}
    snap = [E._snapshot_entry(p.stem, 2000, 6.0, 1000, 0, 0, 0, 1_500_000_000, 2 * MB,
                              source_id=p.stem, path=str(p)) for p in paths]
    _dbstate.seed(E.DB_FILE, {
        "code_checksum": E.code_checksum(),
        "library_snapshot": {"movies": snap, "built_at": 1},
        "pending": {"schema": 1, "entries": entries},
    })
    return lib, movies, paths


# ── Helper predicates ─────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    lib, movies, paths = _seed(td)
    check("_monitored_storage_present: True when mounted", E._monitored_storage_present() is True)
    check("_monitored_root_present: True for a file under a mounted root",
          E._monitored_root_present(str(paths[0])) is True)
    shutil.rmtree(movies)   # unmount the monitored branch
    check("_monitored_storage_present: False when the monitored root is gone",
          E._monitored_storage_present() is False)
    check("_monitored_root_present: False when the root is gone",
          E._monitored_root_present(str(paths[0])) is False)


# ── 1. Tick KEEPS queue + snapshot while unmounted ────────────────────────────
with tempfile.TemporaryDirectory() as td:
    lib, movies, paths = _seed(td)
    before_q, before_s = _queue_paths(), _snap_paths()
    E._snapshot_by_store_key = lambda store: {}
    shutil.rmtree(movies)   # storage offline — every file now ENOENT
    E._revalidate_pending_marks(0)
    check("unmounted tick keeps the whole marked queue", _queue_paths() == before_q)
    check("unmounted tick prunes NO snapshot rows", _snap_paths() == before_s)


# ── 2. Tick STILL prunes a genuinely-gone file when mounted (no regression) ────
with tempfile.TemporaryDirectory() as td:
    lib, movies, paths = _seed(td)
    E._snapshot_by_store_key = lambda store: {}
    paths[0].unlink()   # one real deletion; the root is still mounted
    E._revalidate_pending_marks(0)
    check("mounted tick drops the genuinely-gone file from the queue",
          str(paths[0]) not in _queue_paths())
    check("mounted tick prunes the genuinely-gone snapshot row",
          str(paths[0]) not in _snap_paths())
    check("mounted tick keeps the surviving rows",
          _snap_paths() == {str(p) for p in paths[1:]})


# ── 3. _write_library_snapshot([]) keeps the snapshot while unmounted ──────────
with tempfile.TemporaryDirectory() as td:
    lib, movies, paths = _seed(td)
    before_s = _snap_paths()
    shutil.rmtree(movies)
    E._write_library_snapshot([])
    check("unmounted empty snapshot write is refused (last snapshot kept)",
          _snap_paths() == before_s and len(before_s) == 4)

# A truly-empty but MOUNTED library still writes the empty snapshot.
with tempfile.TemporaryDirectory() as td:
    lib, movies, paths = _seed(td)
    E._write_library_snapshot([])   # movies dir still present, just no movies passed
    check("mounted empty library writes an empty snapshot", _snap_paths() == set())


# ── 4. build_candidates aborts (fail closed) on an unmounted library ──────────
with tempfile.TemporaryDirectory() as td:
    lib, movies, paths = _seed(td)
    shutil.rmtree(movies)
    aborted = False
    try:
        E.build_candidates()
    except SystemExit:
        aborted = True
    check("build_candidates aborts instead of scanning an unmounted library", aborted)
    check("build_candidates abort left the snapshot intact", len(_snap_paths()) == 4)
    check("build_candidates abort left the queue intact", len(_queue_paths()) == 4)


print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
