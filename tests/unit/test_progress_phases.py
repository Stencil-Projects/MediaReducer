"""Per-stage progress bar: each step fills 0→100 ONCE, and never restarts.

Two ways that breaks, both pinned here:

  • A stage emitting the wrong shape. The Plex+Jellyfin path-resolution loop
    must report under the indeterminate "library" step, not as denominatored
    "scanning" progress — otherwise the Scanning bar fills twice (once for
    resolution, once for the real candidate scan).
  • Two writers disagreeing about WHICH RUN this is. The web app stamps the
    start stub with a started_at and hands the same value to the engine; the
    dashboard treats a changed started_at as a whole new run and restarts its
    per-stage bar. When the engine minted its own instead, the very first
    stage — Checking — visibly filled, snapped back to empty, and filled
    again, because the app's stub and the engine's first frame disagreed by
    the seconds between them.
"""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E
import _tmpout
# engine fixes its path constants at import from the /config default,
# so without this the test writes into the real data directory.
_tmpout.redirect_engine(E)

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

calls = []
E.emit_progress = lambda **f: calls.append(f)
E._QUIET_PROGRESS = False

# Force the merge path (both servers), with Plex rows missing 'file' so the
# path-resolution loop does real work and emits progress.
E.USE_PLEX = True
E.USE_JELLYFIN = True
E.get_all_movies_from_tautulli = lambda: [
    {"rating_key": str(1000 + i), "title": f"Movie {i}"} for i in range(250)
]
E.get_all_movies_from_jellyfin = lambda: [
    {"rating_key": f"jf:{i}", "title": f"JF {i}", "file": f"/library/movies/J{i}/J{i}.mkv"} for i in range(5)
]
E._tag_jellyfin_metadata = lambda r: r
E.extract_file_path = lambda row, quiet=False: Path(f"/library/movies/{row.get('title', 'X').replace(' ', '')}/f.mkv")
E._match_keys = lambda p, size=0: {str(p)}

merged = E.get_all_movies()
resolve = [c for c in calls if str(c.get("message", "")).startswith("Resolving")]
denominatored = [c for c in calls if "total" in c or "scanned" in c]

check("merge produced rows", len(merged) > 0)
check("path resolution emitted progress", len(resolve) > 0)
check("resolution reports under the library step, not scanning",
      all(c.get("phase") == "library" for c in resolve))
check("resolution is indeterminate (no scanned/total denominator)",
      all("total" not in c and "scanned" not in c for c in resolve))
# The merge+resolve path must emit only indeterminate progress: a denominatored
# bar (scanned/total) belongs to the real scan loop, which this path never runs.
# Asserting over EVERY emit (not just phase=="scanning") catches a regression
# that mislabels the phase but still carries a denominator.
check("no denominatored progress bar anywhere in the merge",
      len(denominatored) == 0)

# ── One run, one identity ───────────────────────────────────────────────────
# The engine takes the app's stamp when it is handed one, and mints its own
# only when nothing did (a cron or CLI run with no app in front of it).
_STAMP = 1_700_000_000.5
os.environ["MEDIAREDUCER_RUN_STARTED_AT"] = repr(_STAMP)
check("the engine adopts the run identity the app handed it",
      E._run_started_at() == _STAMP)
for _junk in ("", "   ", "not-a-time", "0", "-5"):
    os.environ["MEDIAREDUCER_RUN_STARTED_AT"] = _junk
    _own = E._run_started_at()
    if not (_own > 0 and _own != _STAMP):
        break
else:
    _junk = None
check("an absent or unusable stamp falls back to its own clock", _junk is None)
os.environ.pop("MEDIAREDUCER_RUN_STARTED_AT", None)
check("no stamp at all still yields a usable start time", E._run_started_at() > 0)

# The app half of the same contract: the start stub REPORTS the stamp it
# wrote, which is what the worker passes down. A stub that writes one and
# returns nothing silently reopens the gap.
import json  # noqa: E402
_tmpout.config()          # a config with OUTPUT_DIR, before app's startup runs
import app as A  # noqa: E402
_returned = A._write_progress_start_stub(None, manual=False)
_written = json.loads(Path(A.progress_path()).read_text()).get("started_at")
check("the start stub returns the started_at it wrote",
      _returned is not None and _returned == _written)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
