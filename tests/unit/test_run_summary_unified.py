"""The run summary describes the WHOLE run, movies and seasons together.

A run handles both, but only the movie half wrote the log, so a library with
189 eligible seasons reported "Eligible: 2571" while the Marked & Eligible
window listed 2760. The same split showed up in the switches: movie cleanup
off is counted and explained as movie_cleanup_off, while TV cleanup off simply
removed seasons from the order with nothing said.

Pinned here:

  • the season half's numbers reach the summary — scanned, eligible, marked,
    waiting, deleted — beside the movie ones, and the eligible totals agree
    with the merged window;
  • a report from another run is ignored rather than printed as this run's;
  • out-of-scope rows are reported separately from path issues. A film under a
    directory you chose not to monitor is working as configured, and folding
    those 106 into "Path/disk issues: 125" made a healthy library read as
    hundreds of faults.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
_OUT = tempfile.mkdtemp(prefix="mr-summary.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import engine as E  # noqa: E402
import _tmpout  # noqa: E402
import db  # noqa: E402
_tmpout.redirect_engine(E, _OUT)

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

STATS = {"eligible": 2571, "protected": 98, "identity_mismatch": 43,
         "recently_added": 28, "no_imdb_data": 14, "no_file_path": 1,
         "bad_extension": 18, "missing_on_disk": 0, "outside_monitored_dirs": 106,
         "duplicates_merged": 0, "movie_cleanup_off": 0, "jellyfin_favorite": 0}


# Pin this "run" so the stored season reports below can claim it. The engine
# matches the season report to the RUN that produced it, not to a recent
# clock — an unstamped or foreign report is correctly ignored, which is
# test_season_side_reporting's subject. These cases are about what the summary
# RENDERS once it has this run's numbers, so they stamp themselves as current.
os.environ["MEDIAREDUCER_RUN_STARTED_AT"] = "1000.5"


def _store_season_report(rep):
    if rep:
        rep = {**rep, "run_started_at": E._run_started_at()}
    with db.transaction(E.DB_FILE) as conn:
        db.set_meta(conn, "tv_cleanup", {"last_pass": rep} if rep else {})


EMITTED = []


def _summary(stats=None, is_sim=True):
    lines = []
    EMITTED.clear()
    _log_raw, _log = E.log_raw, E.log
    E.log_raw = lambda m="": lines.append(str(m))
    E.log = lambda m="", **k: lines.append(str(m))
    E.emit_progress = lambda **k: EMITTED.append(k)
    try:
        E.log_run_summary(
            is_sim=is_sim, trigger="scheduled daily", to_free_gb=0.0,
            used_gb=36518.4, free_before_gb=17475.8, final_gb=36518.4,
            final_free_gb=17475.8, freed_bytes=0, removed_count=0,
            skipped_under_limit=0, effective_library_gb=23207.1, max_gb=52994.2,
            build_stats=dict(stats or STATS), total_scanned=2879,
            queued_count=2571, queued_bytes=11_231_800_000_000)
    finally:
        E.log_raw, E.log = _log_raw, _log
    return "\n".join(lines)


# ── The season half reaches the summary ─────────────────────────────────────
_store_season_report({
    "at": time.time(), "seasons_seen": 221, "eligible_seasons": 189,
    "cleanup_off": 0, "marked_new": 4, "held_by_delay": 2,
    "deleted_seasons": [{"title": "X"}], "aborted": None})
text = _summary()
check("seasons are reported as scanned, beside the movies",
      "Movies scanned: 2879" in text and "Seasons scanned: 221" in text, text)
check("the eligible line totals both types",
      "Eligible: 2571 movie(s) + 189 season(s) = 2760" in text, text)
check("...and the standing queue count matches the merged window",
      "Eligible queue: 2760" in text, text)
# The dashboard tile reads progress.json, whose scan frames counted movies
# (all the scan sees) — with seasons in the plan the tile said 2,571 against
# this very line's 2,760. The summary restates the merged total to progress.
check("...and the panel's Eligible tile is told the same merged total",
      any(e.get("eligible") == 2760 for e in EMITTED), EMITTED)
# Its Scanned tile was left counting movies alone: "Scanned 2,879" beside
# "Eligible 2,760" is fine, but a small library read "Scanned 20 · Eligible 24"
# — more eligible than scanned. Scanned counts the seasons seen too.
check("...and its Scanned tile counts the seasons seen, beside the movies",
      any(e.get("scanned") == 2879 + 221 for e in EMITTED), EMITTED)
check("what the run did to seasons is reported too",
      "Seasons marked: 4 new" in text and "Seasons waiting: 2" in text, text)
# What a run deleted (or would) counts its seasons too. A Cleanup counts the
# seasons its season side deleted; a dry run's season side deletes nothing,
# so its "Would delete" counts what this run's merge TOOK — the seasons marked
# as the run ends. (It used to read the pass's deletions, always none in a dry
# run, so a Simulate whose plan was all seasons said it would delete nothing.)
check("a Cleanup's Deleted line counts the seasons it deleted",
      "Deleted: 0 movie(s) + 1 season(s)" in _summary(is_sim=False))
with db.transaction(E.DB_FILE) as conn:
    db.set_meta(conn, "tv_takes", {"run_started_at": E._run_started_at(), "at": time.time(),
                                   "entries": [{"key": "a|S1", "size_bytes": 4_000_000_000},
                                               {"key": "b|S1", "size_bytes": 4_100_000_000}]})
check("a dry run's Would delete counts the seasons its merge took",
      "Would delete: 0 movie(s) + 2 season(s)" in _summary())
with db.transaction(E.DB_FILE) as conn:
    db.set_meta(conn, "tv_takes", {})

# ── A dry run's disk-after counts the seasons its merge took ──────────────
# The movie plan covers only the movies' share of the deficit once seasons take
# part, so used-after from the movies alone sat above the Headroom limit and
# the summary warned "headroom unreachable" for a target movies and seasons
# together reached.
_usage = {"used": 100_000_000_000, "free": 50_000_000_000}
check("a dry run's disk-after spends its movies and the seasons its merge took",
      E._sim_disk_after(_usage, 6_000_000_000, 4_000_000_000) == (90.0, 60.0),
      E._sim_disk_after(_usage, 6_000_000_000, 4_000_000_000))
_src = (ROOT / "engine.py").read_text(encoding="utf-8")
check("...and the Simulate path reads it from there, takes included",
      "final_gb, final_free_gb = _sim_disk_after(usage_info, _would_bytes, _take_b)" in _src)

# ── Out-of-scope is not a fault ─────────────────────────────────────────────
check("path issues count only real problems",
      "Path/disk issues: 19" in text, text)
check("...and out-of-scope rows say what they are, separately",
      "Outside monitored paths: 106" in text and "not a fault" in text, text)

# ── The TV switch explains itself, like the movie switch ────────────────────
_store_season_report({
    "at": time.time(), "seasons_seen": 221, "eligible_seasons": 0,
    "cleanup_off": 189, "marked_new": 0, "held_by_delay": 0,
    "deleted_seasons": [], "aborted": None})
off = _summary()
check("TV cleanup off is stated, not silent",
      "Cleanup off: 189 season(s) (TV cleanup is turned off" in off, off)
check("...and the eligible line then counts movies only",
      "Eligible: 2571" in off and "season(s) = " not in off, off)
check("...with no merged restatement — the scan frames were already right",
      not any("eligible" in e for e in EMITTED), EMITTED)
movies_off = _summary(dict(STATS, eligible=0, movie_cleanup_off=2571))
check("the movie switch reads the same way",
      "Cleanup off: 2571 (movie cleanup is turned off" in movies_off, movies_off)

# ── Another run's report is not this run's ──────────────────────────────────
# The app writes the season report in-process moments before the engine runs,
# and stamps it with the run. When a run's season side sits one out it leaves
# the previous report in place, SECONDS old — so age cannot tell "mine" from
# "recent", and a window let the previous run's counts (deleted_seasons and
# all) print under this run's numbers. Identity can tell them apart, so this
# report is fresh by the clock and still correctly refused.
with db.transaction(E.DB_FILE) as conn:
    db.set_meta(conn, "tv_cleanup", {"last_pass": {
        "at": time.time(), "run_started_at": 999.25,
        "seasons_seen": 221, "eligible_seasons": 189, "cleanup_off": 0,
        "marked_new": 4, "held_by_delay": 2,
        "deleted_seasons": [{"title": "X"}], "aborted": None}})
stale = _summary()
check("a fresh report from a DIFFERENT run is ignored",
      "Seasons scanned" not in stale and "Eligible: 2571" in stale
      and "= 2760" not in stale, stale)
check("...so its season deletions stay off this run's Deleted line",
      "season(s)" not in stale.split("Would delete:")[-1].split("\n")[0], stale)
_store_season_report(None)
none = _summary()
check("no season report at all reads as a movie-only run",
      "Seasons scanned" not in none and "Eligible: 2571" in none, none)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
