"""A run that deleted seasons says so, however it ends.

The app's season side deletes due seasons in-process, BEFORE the engine is
launched; the engine then measures and decides what the movie side still owes.
Every closing line the engine wrote spoke for the movie half alone, so a manual
Cleanup whose whole deficit was covered by seasons (found in the test lab: two
seasons, 8.1 GB, deleted) ended on "Nothing to do — space limits are
satisfied.", Deleted 0, Freed 0.0 GB, with the Deleting step struck through —
and the run log said the same.

Pinned here: each within-limits exit names this run's season deletions and keeps
Deleting ticked; a run that deleted nothing still says "Nothing to do"; the run
REPORT stays movie-only (the app attaches the season numbers itself and the
notification adds the two, so folding them in would count each season twice);
and the dashboard counts the seasons' own progress fields into its tiles.

Hermetic: the season report and every writer are stubbed.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-season-report.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import engine as E  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


GB = 1_000_000_000
frames, reports, lines = [], [], []
E.emit_progress = lambda **f: frames.append(f)
E.write_run_report = lambda **f: reports.append(f)
E.log = lambda msg: lines.append(str(msg))
E.log_blank = lambda: None

THIS_RUN = {"deleted_seasons": [{"title": "Manifest", "season": 1},
                                {"title": "Riverdale", "season": 1}],
            "freed_bytes": int(8.14 * GB)}


def gate(**over):
    """One call of the run gate as a within-limits run reaches it."""
    frames.clear(); reports.clear(); lines.clear()
    args = dict(_manual_cleanup=True, _is_sim=False, daily_breach=False,
                redline_hit=False, immediate_trigger=False, trigger="LIBRARY CAP",
                to_free_gb=0.0, to_free_bytes=0, used_gb=42.6, max_gb=66.2,
                free_gb=20.2, library_gb=108.4)
    args.update(over)
    return E._run_gate(**args)


# ── The helper reads THIS run's season report ─────────────────────────────
E._season_side_report = lambda: dict(THIS_RUN)
check("the run's season deletions are counted with their bytes",
      E._season_deletions_this_run() == (2, int(8.14 * GB)), E._season_deletions_this_run())
E._season_side_report = lambda: {}
check("a run whose season side deleted nothing counts none",
      E._season_deletions_this_run() == (0, 0))

# ── Manual Cleanup, seasons covered the deficit ───────────────────────────
E._season_side_report = lambda: dict(THIS_RUN)
check("the gate still stops the movie side", gate() is True)
msg = frames[-1].get("message", "")
check("the closing line names the seasons it deleted",
      msg.startswith("Deleted 2 season(s), ~8.1 GB"), msg)
check("...and does not claim there was nothing to do", "Nothing to do" not in msg, msg)
check("Deleting keeps its tick — a deletion pass did happen",
      frames[-1].get("skipped_stages") == [1, 2], frames[-1].get("skipped_stages"))
check("the log says the same, not 'Nothing to do'",
      any("season side deleted 2 season(s)" in ln for ln in lines)
      and not any("Nothing to do" in ln for ln in lines), lines)
check("the run report carries the same line", reports and reports[-1].get("message") == msg,
      reports[-1:] and reports[-1].get("message"))
check("...but its counts stay movie-only, so the notification adds seasons once",
      reports[-1].get("deleted_count") == 0 and reports[-1].get("bytes_freed") == 0,
      {k: reports[-1].get(k) for k in ("deleted_count", "bytes_freed")})

# ── Manual Cleanup that genuinely had nothing to do ──────────────────────
E._season_side_report = lambda: {}
gate()
check("a run that deleted nothing still says 'Nothing to do'",
      frames[-1].get("message") == "Nothing to do — space limits are satisfied.",
      frames[-1].get("message"))
check("...and greys Deleting", frames[-1].get("skipped_stages") == [1, 2, 3])

# ── A scheduled Cleanup stopped within limits, both of its branches ──────
E._season_side_report = lambda: dict(THIS_RUN)
E.read_last_cleanup_date = lambda: "2000-01-01"   # a new day
gate(_manual_cleanup=False)
check("a scheduled run's within-limits exit names the seasons too",
      frames[-1].get("message", "").startswith("Deleted 2 season(s)"), frames[-1].get("message"))
import time as _t  # noqa: E402
E.read_last_cleanup_date = lambda: _t.strftime("%Y-%m-%d")   # window already used
gate(_manual_cleanup=False)
check("...on the window-already-used branch as well",
      frames[-1].get("message", "").startswith("Deleted 2 season(s)"), frames[-1].get("message"))

# ── Every closing line that could follow a season deletion reads it ──────
src = (ROOT / "engine.py").read_text(encoding="utf-8")
check("the first progress frame carries the season deletions in their own fields",
      "emit_progress(seasons_deleted=_seasons_n, seasons_bytes_freed=_seasons_b)" in src)
check("the fast path's closing line counts seasons beside movies, in the full scan's shape",
      "_fp_msg = (summary_message(f\"Cleanup finished — freed {bytes_to_gb(bytes_freed + _sb):.1f} GB.\"," in src
      and "[f\"{_sn} season{'' if _sn == 1 else 's'}\"] if _sn else [])" in src)
check("the full scan's closing line adds the seasons' bytes to the total",
      "freed {bytes_to_gb(bytes_freed + _sb):.1f} GB." in src)
check("a run with no eligible movie still leads with the seasons it deleted",
      "— no eligible movies to remove." in src)

js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
check("the Deleted tile adds the seasons",
      "(Number(p.deleted) || 0) + (Number(p.seasons_deleted) || 0)" in js)
check("the Freed tile adds the seasons' bytes",
      "(Number(p.bytes_freed) || 0) + (Number(p.seasons_bytes_freed) || 0)" in js)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
