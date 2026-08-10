"""Measuring the library, and what the dashboard is told about it.

The size is read from disk, not from a media server's catalogue, because a
server's cached size lags a deletion for a long time — long enough that the
Library Size Cap would keep firing on space that had already been freed.

The interesting answer is None: the disk read failed. That is not zero. A
library reported as 0 GB reads as massively under cap, which quietly disables
the very thing the number is for, and on the dashboard it replaces a good
figure with a blank. Both are pinned here, along with the wording of the size
line, since that line is where anyone checks whether the cap is about to fire.

Nothing covered any of it — neither the read nor the stats emission appeared
in a single test.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


TB = 1_000_000_000_000


def measure(*, size_gb=200.0, cap=150.0, total=2 * TB, used=1200.0, free=800.0,
            info=False, sections=None):
    """Run the measurement with a canned disk read and report everything it
    produced: the two return values, the log lines, and the stats payload the
    dashboard receives. The stored measure is cleared first: a Summary would
    otherwise reuse whatever the previous case's walk remembered, and these
    cases are about the walk itself."""
    import db as _db
    with _db.transaction(E.DB_FILE) as _c:
        _db.set_meta(_c, "library_size", None)
    logs, stats, progress = [], [], []
    saved = (E.MAX_LIBRARY_GB, E.get_library_size_gb, E.get_movie_section_ids,
             E.emit_stats, E.emit_progress, E.log)
    E.MAX_LIBRARY_GB = cap
    E.get_library_size_gb = lambda: size_gb
    E.get_movie_section_ids = sections or (lambda: None)
    E.emit_stats = lambda **kw: stats.append(kw)
    E.emit_progress = lambda **kw: progress.append(kw)
    E.log = lambda msg="", *a, **k: logs.append(str(msg))
    try:
        library_gb, total_gb = E._read_library_size(
            usage_info={"total": total}, used_gb=used, free_gb=free, _is_info=info)
    finally:
        (E.MAX_LIBRARY_GB, E.get_library_size_gb, E.get_movie_section_ids,
         E.emit_stats, E.emit_progress, E.log) = saved
    return library_gb, total_gb, logs, stats[-1] if stats else None


def size_line(logs):
    # Empty rather than None when the line is missing: a missing size line is a
    # failure to report, and it should read as a failed check rather than take
    # the whole file down with a TypeError on the next `in`.
    return next((x for x in logs if x.startswith("Library size:")), "")


# ── The size line ────────────────────────────────────────────────────────────
lib, total, logs, _ = measure(size_gb=200.0, cap=150.0)
check("the measured size is returned", lib == 200.0, lib)
check("the filesystem total comes back in GB", total == 2000.0, total)
check("a library over its cap says so", "OVER cap by 50.0 GB" in size_line(logs),
      size_line(logs))
check("...alongside the size and the cap",
      "200.0 GB" in size_line(logs) and "150.0 GB" in size_line(logs), size_line(logs))

_, _, logs, _ = measure(size_gb=100.0, cap=150.0)
check("a library under its cap says how much room is left",
      "under cap by 50.0 GB" in size_line(logs), size_line(logs))
check("...phrased as headroom, not a negative overage",
      "-" not in size_line(logs).split("|")[-1], size_line(logs))

# Exactly on the cap is not over it — the cap fires on `library > cap`.
_, _, logs, _ = measure(size_gb=150.0, cap=150.0)
check("a library exactly on its cap is not reported as over",
      "under cap by 0.0 GB" in size_line(logs), size_line(logs))

_, _, logs, _ = measure(size_gb=100.0, cap=None)
check("no cap set is reported as disabled", "cap: disabled" in size_line(logs),
      size_line(logs))

# ── A failed disk read ───────────────────────────────────────────────────────
# The whole point: unavailable is not zero.
lib, total, logs, st = measure(size_gb=None, cap=150.0)
check("a failed read returns None, not 0", lib is None, lib)
check("...and says it is unavailable rather than quoting a size",
      "unavailable" in size_line(logs) and "0.0 GB" not in size_line(logs),
      size_line(logs))
check("...and does not claim the library is under its cap",
      "under cap" not in size_line(logs), size_line(logs))
check("...leaving the dashboard's last good figure in place",
      "library_gb" not in st, st)
check("...while still refreshing the disk figures", st["disk"]["used_gb"] == 1200.0, st)
check("...and still reporting the cap that is set", st["library_cap_gb"] == 150.0, st)

# ── What the dashboard is handed ─────────────────────────────────────────────
_, _, _, st = measure(size_gb=201.44, cap=150.0, total=2 * TB, used=1200.0, free=800.0)
check("a known size is published", st["library_gb"] == 201.4, st.get("library_gb"))
check("the disk figures are published together",
      st["disk"]["used_gb"] == 1200.0 and st["disk"]["free_gb"] == 800.0
      and st["disk"]["total_gb"] == 2000.0, st["disk"])
check("percent used is computed from used and total",
      st["disk"]["pct_used"] == 60.0, st["disk"]["pct_used"])

# A zero-capacity filesystem is a division by zero waiting to happen, and it
# reaches here from a disk_usage read on a vanished mount. The used figure is
# passed in separately from the total, so the two can disagree — which is what
# makes this more than a guard against ZeroDivisionError: a nonzero used
# against a zero total must report 0%, not a percentage in the billions.
_, _, _, st = measure(size_gb=100.0, total=0, used=1200.0)
check("a zero-capacity filesystem reports 0% rather than raising or exploding",
      st["disk"]["pct_used"] == 0, st["disk"])

# ── Summary mode's extra step ────────────────────────────────────────────────
called = []
measure(info=True, sections=lambda: called.append(True))
check("Summary mode also surfaces the movie section IDs", called == [True], called)

_, _, logs, _ = measure(info=False, sections=lambda: called.append("again"))
check("...and other modes do not pay for that query", "again" not in called, called)

# An unreachable Tautulli must not take the size read down with it — the size
# comes from disk and needs no server at all.
lib, _, logs, st = measure(
    size_gb=100.0, info=True,
    sections=lambda: (_ for _ in ()).throw(RuntimeError("tautulli down")))
check("a failed section query does not stop the size read", lib == 100.0, lib)
check("...and is reported rather than swallowed",
      any("Movie section IDs unavailable" in x for x in logs), logs)
check("...with the dashboard still refreshed", st is not None and "library_gb" in st, st)

# ── The quiet Summary does not re-walk a 23 TB library every 15 minutes ──────
# The tick runs Summary ~96 times a day, and the walk stats every movie file —
# enough to keep an array's standby disks awake around the clock for a number
# that only changes when files come or go. Summary reuses a measure younger
# than the window; every mode that can DELETE still measures fresh, so no
# deletion decision ever rides on the reused figure.
import time as _time                               # noqa: E402


def count_walks(*, info, age=None, walk_result=123.0):
    """Run the measurement with the stored measure aged as asked, and report
    how many times the disk walk actually ran."""
    walks = []
    saved = (E.get_library_size_gb, E._recent_library_size_gb if False else None)
    E.get_library_size_gb = lambda: walks.append(1) or walk_result
    if age is None:
        E._remember_library_size(None)   # not a number: stores nothing
        try:
            import db as _db
            with _db.transaction(E.DB_FILE) as _c:
                _db.set_meta(_c, "library_size", None)
        except Exception:
            pass
    else:
        E._remember_library_size(200.0)
        import db as _db
        with _db.transaction(E.DB_FILE) as _c:
            _db.set_meta(_c, "library_size",
                         {"gb": 200.0, "at": _time.time() - age})
    stats, logs = [], []
    o_stats, o_prog, o_log, o_secs = E.emit_stats, E.emit_progress, E.log, E.get_movie_section_ids
    E.emit_stats = lambda **kw: stats.append(kw)
    E.emit_progress = lambda **kw: None
    E.log = lambda msg="", *a, **k: logs.append(str(msg))
    E.get_movie_section_ids = lambda: None
    try:
        lib, _total = E._read_library_size(
            usage_info={"total": 2 * TB}, used_gb=100.0, free_gb=500.0, _is_info=info)
    finally:
        E.get_library_size_gb = saved[0]
        E.emit_stats, E.emit_progress, E.log, E.get_movie_section_ids = (
            o_stats, o_prog, o_log, o_secs)
    return len(walks), lib, stats[-1] if stats else None


walks, lib, st = count_walks(info=True, age=120)
check("a Summary two minutes after a measure does NOT walk the library",
      walks == 0, walks)
check("...and uses the stored figure", lib == 200.0, lib)
check("...which still reaches the dashboard", st and st.get("library_gb") == 200.0, st)

walks, lib, _ = count_walks(info=True, age=E.LIBRARY_SIZE_REUSE_SECONDS + 60)
check("a Summary past the reuse window walks fresh", walks == 1 and lib == 123.0,
      (walks, lib))
walks, lib, _ = count_walks(info=True, age=None)
check("a Summary with no stored measure walks fresh", walks == 1, walks)
walks, lib, _ = count_walks(info=True, age=-3600)
check("a clock that moved backwards reads as stale, not fresh-forever",
      walks == 1, walks)

# The modes that can delete never reuse: their cap trigger and deficit are
# computed from what is actually on disk at run start.
walks, lib, _ = count_walks(info=False, age=120)
check("a non-Summary run measures fresh even with a fresh stored figure",
      walks == 1 and lib == 123.0, (walks, lib))

# A fresh walk stores its measure for the Summaries that follow.
import db as _db                                    # noqa: E402
with _db.connect(E.DB_FILE) as _c:
    rec = _db.get_meta(_c, "library_size")
check("a fresh walk stores its measure", isinstance(rec, dict) and rec.get("gb") == 123.0,
      rec)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
