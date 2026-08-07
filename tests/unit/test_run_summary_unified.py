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
  • a stale season report is ignored rather than printed as this run's;
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


def _store_season_report(rep):
    with db.transaction(E.DB_FILE) as conn:
        db.set_meta(conn, "tv_cleanup", {"last_pass": rep} if rep else {})


def _summary(stats=None):
    lines = []
    _log_raw, _log = E.log_raw, E.log
    E.log_raw = lambda m="": lines.append(str(m))
    E.log = lambda m="", **k: lines.append(str(m))
    try:
        E.log_run_summary(
            is_sim=True, trigger="scheduled daily", to_free_gb=0.0,
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
check("what the run did to seasons is reported too",
      "Seasons marked: 4 new" in text and "Seasons waiting: 2" in text
      and "1 season(s)" in text, text)

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
movies_off = _summary(dict(STATS, eligible=0, movie_cleanup_off=2571))
check("the movie switch reads the same way",
      "Cleanup off: 2571 (movie cleanup is turned off" in movies_off, movies_off)

# ── A stale report is not this run's ────────────────────────────────────────
# The app writes the season report in-process moments before the engine runs.
# Anything older is a PREVIOUS run's, and printing it would credit this run
# with work it did not do.
_store_season_report({
    "at": time.time() - 7200, "seasons_seen": 221, "eligible_seasons": 189,
    "cleanup_off": 0, "marked_new": 4, "held_by_delay": 2,
    "deleted_seasons": [], "aborted": None})
stale = _summary()
check("a stale season report is ignored",
      "Seasons scanned" not in stale and "Eligible: 2571" in stale
      and "= 2760" not in stale, stale)
_store_season_report(None)
none = _summary()
check("no season report at all reads as a movie-only run",
      "Seasons scanned" not in none and "Eligible: 2571" in none, none)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
