"""The walk that deletes: when it stops, what it marks, what it records.

This is the only code in the engine that removes a file, and the scenario
table can only reach part of it. Scenarios drive a real app against a real
library, so they can never pose a deletion that FAILS, and their targets are
covered by one or two files — which leaves the arithmetic that decides when to
stop walking barely exercised. A sweep of nineteen mutations over this block
made that plain: breaking the stop condition, disconnecting the byte counters,
and counting a refused deletion as done all went through the whole table.

Two of those are fixed at the scenario layer now, where a run that empties a
library instead of freeing a target belongs. The rest need what a fixture
cannot supply: a delete that returns False without removing the file, a file
that vanishes underneath the run, a Stop mid-walk, and a target that several
candidates could cover so the choice between them is visible.

Reachable here because the phase is now its own function. Everything the walk
reaches out to is stubbed except the four helpers that ARE the behaviour under
test — the pick, the mark ageing, the per-mark delay, and the delete-on date.
The library is real files in a temp tree, because the walk stats them.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)

ok = True
TMP = Path(tempfile.mkdtemp(prefix="mr-delete-pass-"))
_case = [0]


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def library(sizes_scores):
    """A throwaway library, one real file per movie. Each case gets its own
    tree so a deletion in one cannot change what the next one sees."""
    _case[0] += 1
    root = TMP / f"case{_case[0]}"
    out = []
    for i, (size, score) in enumerate(sizes_scores):
        title = f"M{i}"
        p = root / title / f"{title}.mkv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * size)
        out.append({"path": p, "title": title, "retention_score": float(score),
                    "file_size": size, "tmdb_id": f"tm{i}", "section_id": "3"})
    return out


class Run:
    """What one walk did, as seen from outside it."""

    def __init__(self):
        self.saves, self.logs, self.report = [], [], {}
        self.summary, self.progress = {}, []
        self.stopped = False


def walk(cands, *, target, delay_days=0, immediate=False, manual=False,
         queue=None, delete=None, near_tie=None):
    """Run the live delete/mark phase over `cands` and report what happened.

    `delete` stands in for delete_candidate, which is where every way a
    deletion can go wrong lives: it may refuse (return False, file intact), it
    may find the file already gone, or it may raise. The default succeeds.
    """
    r = Run()

    def default_delete(c):
        c["path"].unlink()
        return True

    saved = {k: getattr(E, k) for k in (
        "DELETE_DELAY_DAYS", "NEAR_TIE_PTS", "load_pending", "save_pending",
        "delete_candidate", "log_usage", "emit_progress", "log", "log_blank",
        "get_usage_info", "log_run_summary", "summary_message",
        "write_run_report", "_redline_only_mode")}
    E.DELETE_DELAY_DAYS = delay_days
    E.NEAR_TIE_PTS = near_tie
    E.load_pending = lambda: dict(queue or {})
    E.save_pending = lambda q, **kw: r.saves.append((q, kw))
    E.delete_candidate = delete or default_delete
    E.log_usage = lambda *a, **k: None
    E.emit_progress = lambda **k: r.progress.append(k)
    E.log = lambda msg="", *a, **k: r.logs.append(str(msg))
    E.log_blank = lambda *a, **k: None
    E.get_usage_info = lambda: {"used_gb": 10.0, "free": 5_000_000_000,
                                "total": 20_000_000_000, "max_gb": 15.0}
    E.log_run_summary = lambda **kw: r.summary.update(kw)
    E.summary_message = lambda *a, **k: "done"
    E.write_run_report = lambda **kw: r.report.update(kw)
    E._redline_only_mode = lambda: False
    try:
        E._delete_and_report(
            candidates=cands, build_stats={}, total_scanned=len(cands),
            used_gb=10.0, max_gb=15.0, free_gb=5.0, effective_library_gb=1.0,
            to_free_gb=target / 1e9, to_free_bytes=target, trigger="LIBRARY CAP",
            immediate_trigger=immediate, _manual_cleanup=manual)
    except SystemExit:
        # Recorded rather than propagated: a Stop has to leave evidence behind,
        # and letting it escape would take the record with it.
        r.stopped = True
    finally:
        for k, v in saved.items():
            setattr(E, k, v)
    return r


DAY = 86400.0


def mark(title, *, age_days, delay_days=None, size=100):
    """A queue row with a running clock, aged by hand."""
    e = {"title": title, "score": 1.0, "size_bytes": size,
         "marked_at": time.time() - age_days * DAY}
    if delay_days is not None:
        e["delay_days"] = delay_days
    return e


# ── Stopping ─────────────────────────────────────────────────────────────────
# The walk tests its target BEFORE taking the next candidate, so it stops on
# the first file that carries the total over. Five 100-byte movies and a
# 250-byte target is three files, not five: the two cheapest survive.
cands = library([(100, s) for s in (1, 5, 9, 13, 17)])
r = walk(cands, target=250)
check("the walk stops as soon as the target is covered",
      r.report["deleted_count"] == 3, r.report.get("deleted_count"))
check("...having freed at least the target",
      r.report["bytes_freed"] == 300, r.report.get("bytes_freed"))
check("...and left the rest of the library alone",
      [c["path"].exists() for c in cands] == [False, False, False, True, True],
      [c["path"].exists() for c in cands])
check("...taking them worst-score first",
      [d["title"] for d in r.report["deleted_items"]] == ["M0", "M1", "M2"],
      r.report["deleted_items"])

r = walk(library([(100, s) for s in (1, 5, 9)]), target=0)
check("a zero target deletes nothing at all", r.report["deleted_count"] == 0,
      r.report.get("deleted_count"))

# A target larger than the pool takes the pool and stops there, rather than
# looping or failing.
cands = library([(100, 1), (100, 5)])
r = walk(cands, target=10_000)
check("a target bigger than the library takes everything and stops",
      r.report["deleted_count"] == 2 and r.report["bytes_freed"] == 200,
      (r.report["deleted_count"], r.report["bytes_freed"]))

# ── Marking counts against the same target ───────────────────────────────────
# A delayed run frees nothing today, so if marks did not count the walk would
# never reach its target and would mark the whole library.
cands = library([(100, s) for s in (1, 5, 9, 13, 17)])
r = walk(cands, target=250, delay_days=30)
check("a delayed run marks up to the target and no further",
      r.report["marked_count"] == 3, r.report.get("marked_count"))
check("...and deletes nothing today",
      r.report["deleted_count"] == 0 and all(c["path"].exists() for c in cands))
check("...recording when each one will go",
      len(r.report["marked_items"]) == 3
      and all(m.get("delete_on") for m in r.report["marked_items"]),
      r.report["marked_items"])
q, kw = r.saves[-1]
# `is not None`, not truthiness: an epoch of 0 is exactly the mistake being
# guarded against here, and a falsy test would filter it out of its own check.
clocked = [e for e in q.values() if e.get("marked_at") is not None]
check("...starting each new mark's clock NOW",
      len(clocked) == 3 and all(abs(e["marked_at"] - time.time()) < 60
                                for e in clocked), q)
check("...under the delay in force today",
      all(e["delay_days"] == 30 for e in clocked), q)
check("...and stamping the rebuilt queue against its thresholds",
      kw.get("stamp_thresholds") is True, kw)

# ── The delay clock ──────────────────────────────────────────────────────────
cands = library([(100, 1), (100, 5)])
r = walk(cands, target=1000, delay_days=7,
         queue={str(cands[0]["path"]): mark("M0", age_days=9),
                str(cands[1]["path"]): mark("M1", age_days=2)})
check("a mark past its delay is collected", not cands[0]["path"].exists())
check("...while one still inside it is kept", cands[1]["path"].exists())
check("...and only the collected one counts as deleted",
      r.report["deleted_count"] == 1 and r.report["marked_count"] == 1,
      (r.report["deleted_count"], r.report["marked_count"]))

# An existing mark ages against the delay it was MADE under. Raising the
# setting afterwards must not re-date marks already waiting, or every setting
# change would silently restart everyone's clock.
cands = library([(100, 1)])
walk(cands, target=1000, delay_days=30,
     queue={str(cands[0]["path"]): mark("M0", age_days=5, delay_days=1)})
check("a mark keeps the delay it was made under", not cands[0]["path"].exists())

cands = library([(100, 1)])
walk(cands, target=1000, delay_days=1,
     queue={str(cands[0]["path"]): mark("M0", age_days=5, delay_days=30)})
check("...in the lenient direction too", cands[0]["path"].exists())

# A queue row with no clock is eligible, not scheduled: being visible in the
# deletion order earns no grace, so its wait starts now.
cands = library([(100, 1)])
r = walk(cands, target=1000, delay_days=7,
         queue={str(cands[0]["path"]): {"title": "M0", "score": 1.0,
                                        "marked_at": None}})
check("an eligible queue row starts its clock now rather than deleting",
      cands[0]["path"].exists() and r.report["marked_count"] == 1,
      r.report.get("marked_count"))

# A manual Cleanup and a Redline emergency skip the wait entirely.
for label, kw in (("a manual Cleanup", {"manual": True}),
                  ("a redline emergency", {"immediate": True})):
    cands = library([(100, 1)])
    walk(cands, target=50, delay_days=30, **kw)
    check(f"{label} deletes without waiting out the delay",
          not cands[0]["path"].exists())

# ── A deletion that does not happen ──────────────────────────────────────────
# No fixture can pose this: delete_candidate refusing while the file stays put
# is a permission error, a safety abort, a mount hiccup.
cands = library([(100, 1)])
r = walk(cands, target=1000, delay_days=7, delete=lambda c: False,
         queue={str(cands[0]["path"]): mark("M0", age_days=9)})
check("a refused deletion is not counted as done",
      r.report["deleted_count"] == 0 and r.report["bytes_freed"] == 0,
      (r.report["deleted_count"], r.report["bytes_freed"]))
check("...and says so", any("did not remove" in x for x in r.logs), r.logs[-3:])
kept = r.saves[-1][0][str(cands[0]["path"])]
check("...keeping the ORIGINAL clock so the retry does not re-arm the wait",
      kept["marked_at"] < time.time() - 8 * DAY, kept.get("marked_at"))

# Gone, but not by us: the file vanished between the scan and the walk. It is
# not a deletion this run can claim — nothing was freed and deleted.log has no
# record — but its snapshot row is now a phantom and has to be pruned.
cands = library([(100, 1), (100, 5)])


def vanish(c):
    if c["title"] == "M0":
        c["path"].unlink()
        return False
    c["path"].unlink()
    return True


r = walk(cands, target=1000, delete=vanish)
check("a file that vanished on its own is not claimed as deleted",
      r.report["deleted_count"] == 1, r.report.get("deleted_count"))
check("...and its stale snapshot row is pruned",
      str(cands[0]["path"]) in r.saves[-1][1].get("snapshot_delete_paths", set()),
      r.saves[-1][1])

# The space it took is gone all the same, so it counts against the target —
# otherwise a run whose first candidate vanished goes on to delete a file it
# did not need, having already reached its goal.
cands = library([(100, s) for s in (1, 5, 9)])
r = walk(cands, target=100, delete=vanish)
check("a vanished file still counts toward the target",
      cands[1]["path"].exists() and r.report["deleted_count"] == 0,
      (cands[1]["path"].exists(), r.report.get("deleted_count")))

# ── The queue the walk leaves behind ─────────────────────────────────────────
cands = library([(100, s) for s in (1, 5, 9)])
r = walk(cands, target=150)          # two 100-byte files cover it; one survives
q, kw = r.saves[-1]
check("survivors stay in the queue as eligible",
      sorted(q) == [str(cands[2]["path"])], sorted(q))
check("...with no clock running", all(e["marked_at"] is None for e in q.values()))
check("...and deleted files are not rebuilt back into it",
      str(cands[0]["path"]) not in q)
check("the rebuilt queue carries the Radarr identity",
      all(e["tmdb_id"] and e["section_id"] for e in q.values()), q)
check("...and is stamped against the thresholds it was built under",
      kw.get("stamp_thresholds") is True, kw)

# A mark that left the plan loses its clock and reverts to eligible. Without
# it, a movie protected since it was marked would keep counting down.
cands = library([(100, 1)])
r = walk(cands, target=50, delay_days=30,
         queue={"/gone/Old.mkv": mark("Old", age_days=1)})
check("a mark no longer in the plan is unmarked",
      "/gone/Old.mkv" not in r.saves[-1][0], r.saves[-1][0].keys())
check("...and the run says how many", any("Unmarked 1 movie" in x for x in r.logs),
      [x for x in r.logs if "nmark" in x])

# ── Stop mid-walk ────────────────────────────────────────────────────────────
# A Stop must not throw away the bookkeeping done so far, or marks made this
# run would restart from day 0 and deleted files would linger as marked.
cands = library([(100, s) for s in (1, 5, 9)])


def stop_on_second(c):
    if c["title"] == "M1":
        raise SystemExit(0)
    c["path"].unlink()
    return True


r = walk(cands, target=1000, delay_days=7, delete=stop_on_second,
         queue={str(c["path"]): mark(c["title"], age_days=9) for c in cands})
check("a Stop still stops the run", r.stopped)
check("...after saving the marks it had already changed", bool(r.saves), r.saves)
check("...and saying so", any("Stopped mid-run" in x for x in r.logs), r.logs[-2:])
if r.saves:
    q = r.saves[-1][0]
    check("...with the file it did delete dropped from the queue",
          str(cands[0]["path"]) not in q, sorted(q))
    check("...and the ones it never reached still in it",
          str(cands[2]["path"]) in q, sorted(q))
    check("...without stamping a partial pass as a fresh plan",
          r.saves[-1][1].get("stamp_thresholds") is not True, r.saves[-1][1])

# ── File-size optimization ───────────────────────────────────────────────────
# Inside a group of near-tied scores the walk is free to choose, and it picks
# the cheapest cover so one big movie can spare several small near-ties. What
# it must NOT do is choose against the ORIGINAL target once it is part way
# through — the remaining target is what is left, not what it started with.
#
# Three near-tied movies of 100, 200 and 900 bytes against a 1000-byte target.
# Nothing single covers it, so the largest goes first and 100 bytes are left to
# find — which the 100-byte movie covers exactly, sparing the 200. Judging that
# second pick against the ORIGINAL 1000 instead would find nothing that covers
# it, fall back to "take the largest", and delete the 200 rather than the 100:
# the same count of movies, a different movie, and 100 bytes over.
cands = library([(100, 1.0), (200, 1.5), (900, 2.0)])
r = walk(cands, target=1000, near_tie=2)
check("the biggest near-tie goes first when none covers the target alone",
      not cands[2]["path"].exists(), r.report["deleted_items"])
check("...then the remainder is covered exactly, not the original target",
      not cands[0]["path"].exists() and cands[1]["path"].exists(),
      [c["path"].exists() for c in cands])
check("...for the smallest overshoot the group allows",
      r.report["bytes_freed"] == 1000, r.report["bytes_freed"])
check("...and the choice is explained",
      any("File size optimization" in x for x in r.logs),
      [x for x in r.logs if "optimi" in x])

print("RESULT:", "PASS" if ok else "FAIL")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok else 1)
