"""The guards on the season deletion path, one at a time.

test_tv_cleanup drives the happy path and the big refusals through a whole
pass. A mutation audit of _delete_tv_season found nine of its fourteen guards
survive that: the pass only ever reaches them with well-formed input, so the
line that refuses malformed input never runs and could be deleted without a
single test noticing.

The ones that decide which FILE is removed matter most, because the paths
being guarded against come from a media server — not from this app, and not
from the user:

  • a series folder outside every monitored directory;
  • a relative path carrying `..` or a leading slash;
  • a file that resolves outside the series folder because a season folder is
    a symlink to somewhere else — the per-file symlink check does not catch
    that one, since the FILE is not a link;
  • the series folder itself, which must survive its own last season.

The Sonarr guards are next. They cannot delete the wrong file, but they can
unmonitor the wrong show in someone else's Sonarr, and the existing fixture
cannot reach them: it disambiguates two same-title series by year, so the
checks that run AFTER the year check — an ambiguity the year could not settle,
a lone match whose year is wrong, a season Sonarr does not list — are all
downstream of a filter that has already succeeded.

Hermetic: a real temp tree, the media server's file list and Sonarr's API
stubbed per case.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
OUT = Path(_tmpout.config())
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


LIB = OUT / "library"
TV = LIB / "TV Shows"
OUTSIDE = OUT / "not-monitored"
SHOW = TV / "A Show"


def reset():
    """A two-file season under a monitored dir, and a decoy outside it."""
    shutil.rmtree(LIB, ignore_errors=True)
    shutil.rmtree(OUTSIDE, ignore_errors=True)
    (SHOW / "Season 01").mkdir(parents=True)
    (SHOW / "Season 01" / "ep1.mkv").write_bytes(b"x" * 1000)
    (SHOW / "Season 01" / "ep2.mkv").write_bytes(b"y" * 2000)
    (SHOW / "Season 02").mkdir(parents=True)
    (SHOW / "Season 02" / "keeper.mkv").write_bytes(b"z" * 3000)
    OUTSIDE.mkdir(parents=True)
    (OUTSIDE / "treasure.mkv").write_bytes(b"!" * 4000)


CFG = {"OUTPUT_DIR": str(OUT), "MONITOR_DIRS": [str(TV)],
       "SONARR_URL": "", "SONARR_API_KEY": "", "SONARR_CLEANUP_ENABLED": False}
ENTRY = {"sid": "jellyfin:5", "title": "A Show", "season": 1,
         "path": str(SHOW), "year": 2001, "size_bytes": 3000}

# The season's file list, as the media server reports it. Every case below
# swaps this for the shape it is about; _tv_season_relpaths has its own test.
RELS = [(PurePosixPath("Season 01/ep1.mkv"), 1000),
        (PurePosixPath("Season 01/ep2.mkv"), 2000)]
A._tv_season_relpaths = lambda cfg, sid, n, name="": (list(RELS), None)
A._tv_log_deleted = lambda *a, **k: None


def run(cfg=None, entry=None):
    report = {"skipped": [], "deleted_seasons": [], "deleted_files": 0,
              "freed_bytes": 0, "vanished_seasons": 0, "vanished_bytes": 0}
    done = A._delete_tv_season(cfg or dict(CFG), dict(entry or ENTRY), report)
    return done, report


def why(report):
    return (report["skipped"][0]["why"] if report["skipped"] else "")


# ── Where the series folder is ─────────────────────────────────────────────
reset()
done, rep = run()
check("the ordinary case still deletes the season",
      done and rep["deleted_files"] == 2 and not (SHOW / "Season 01").exists(), rep)
check("...and the sibling season survives", (SHOW / "Season 02" / "keeper.mkv").exists())
check("...and so does the series folder — the servers key on it", SHOW.is_dir())

reset()
done, rep = run(entry={**ENTRY, "path": str(OUTSIDE)})
check("a series folder outside every monitored directory is refused",
      done is False and "not under a monitored directory" in why(rep), rep["skipped"])
check("...with nothing removed from it", (OUTSIDE / "treasure.mkv").exists())

reset()
done, rep = run(entry={**ENTRY, "path": str(TV)})
check("a series folder that IS a monitored directory is refused",
      done is False and "monitored directory itself" in why(rep), rep["skipped"])

reset()
done, rep = run(entry={**ENTRY, "path": str(TV / "No Such Show")})
check("a series folder that is gone is refused, not deleted from",
      done is False and "series folder is gone" in why(rep), rep["skipped"])


# ── What the media server says the files are ───────────────────────────────
# These paths are joined to the series folder. They arrive over HTTP from a
# server this app does not control, so they are the untrusted half of the
# deletion and each shape has to be refused rather than normalised.
reset()
RELS = [(PurePosixPath("../../not-monitored/treasure.mkv"), 4000)]
done, rep = run()
check("a path climbing out with .. is refused",
      done is False and "unsafe file path" in why(rep), rep["skipped"])
check("...and the file it pointed at is untouched", (OUTSIDE / "treasure.mkv").exists())

reset()
RELS = [(PurePosixPath("/etc/hostname"), 10)]
done, rep = run()
check("an absolute path is refused", done is False and "unsafe file path" in why(rep),
      rep["skipped"])

reset()
RELS = [(PurePosixPath("."), 0)]
done, rep = run()
check("a path that is just the series folder is refused",
      done is False and "unsafe file path" in why(rep), rep["skipped"])

# The one the per-file symlink check cannot catch: the FILE is a real file,
# its parent directory is the link. Only resolving the whole path finds it.
reset()
shutil.rmtree(SHOW / "Season 01")
(SHOW / "Season 01").symlink_to(OUTSIDE, target_is_directory=True)
RELS = [(PurePosixPath("Season 01/treasure.mkv"), 4000)]
done, rep = run()
check("a file inside a SYMLINKED season folder is refused",
      done is False and "escapes the series folder" in why(rep), rep["skipped"])
check("...and the file outside is still there", (OUTSIDE / "treasure.mkv").exists())
(SHOW / "Season 01").unlink()

# A season folder emptied by the deletion goes; the series folder never does,
# even when the season it held was its last.
reset()
shutil.rmtree(SHOW / "Season 02")
RELS = [(PurePosixPath("Season 01/ep1.mkv"), 1000),
        (PurePosixPath("Season 01/ep2.mkv"), 2000)]
done, rep = run()
check("the emptied season folder is tidied away", not (SHOW / "Season 01").exists())
check("...and the now-empty series folder still stands", SHOW.is_dir())

# The layout that puts the rule to work: no season folders at all, so the
# emptied directory IS the series folder. Removing it takes the thing both the
# media server and Sonarr key on, and the next scan reports the series gone —
# from a run that was only ever asked for one season.
reset()
shutil.rmtree(SHOW)
SHOW.mkdir(parents=True)
(SHOW / "ep1.mkv").write_bytes(b"x" * 1000)
RELS = [(PurePosixPath("ep1.mkv"), 1000)]
done, rep = run()
check("a season-less layout deletes its episode", done and rep["deleted_files"] == 1
      and not (SHOW / "ep1.mkv").exists(), rep)
check("...and the series folder it emptied is still there", SHOW.is_dir())


# ── Sonarr: the season must be unmonitored on the RIGHT show ───────────────
RELS = [(PurePosixPath("Season 01/ep1.mkv"), 1000),
        (PurePosixPath("Season 01/ep2.mkv"), 2000)]
SCFG = {**CFG, "SONARR_URL": "http://sonarr.test:8989",
        "SONARR_API_KEY": "k", "SONARR_CLEANUP_ENABLED": True}
SERIES = []
PUTS = []


def sonarr(url, headers=None, timeout=15, method=None, payload=None):
    if url.endswith("/api/v3/series") and method is None:
        return json.loads(json.dumps(SERIES))
    if method == "PUT":
        PUTS.append((url, payload))
        return payload
    return {}


A._json_request = sonarr


def sonarr_run(series, entry=None):
    global SERIES
    del PUTS[:]
    SERIES = series
    reset()
    return run(cfg=dict(SCFG), entry=entry)


# Two series sharing the title AND the year: the year cannot settle it, so
# the ambiguity survives and the season is skipped rather than unmonitored on
# a coin flip. The existing fixture never reaches this — its two shows differ
# in year, so the filter above always leaves exactly one.
done, rep = sonarr_run([
    {"id": 9, "title": "A Show", "year": 2001, "seasons": [{"seasonNumber": 1, "monitored": True}]},
    {"id": 5, "title": "A Show", "year": 2001, "seasons": [{"seasonNumber": 1, "monitored": True}]}])
check("two Sonarr series the year cannot tell apart are refused",
      done is False and "could not unmonitor" in why(rep), rep["skipped"])
check("...with nothing unmonitored", not PUTS, PUTS)
check("...and no file deleted", (SHOW / "Season 01" / "ep1.mkv").exists())

# Our entry has no year to disambiguate with: same outcome, fail closed.
done, rep = sonarr_run([
    {"id": 9, "title": "A Show", "year": 1965, "seasons": [{"seasonNumber": 1, "monitored": True}]},
    {"id": 5, "title": "A Show", "year": 2001, "seasons": [{"seasonNumber": 1, "monitored": True}]}],
    entry={**ENTRY, "year": None})
check("two same-title series and no year on our row are refused",
      done is False and "could not unmonitor" in why(rep), rep["skipped"])
check("...with nothing unmonitored", not PUTS, PUTS)

# A single title match whose year is not ours is a DIFFERENT show. Ours is
# simply not in Sonarr — nothing to unmonitor, no re-download risk, so the
# files still go. Unmonitoring it instead would silently reconfigure a show
# the user never asked this app to touch.
done, rep = sonarr_run([
    {"id": 9, "title": "A Show", "year": 1965, "seasons": [{"seasonNumber": 1, "monitored": True}]}])
check("a lone title match with the wrong year is not treated as ours",
      done is True and not PUTS, (done, PUTS, rep["skipped"]))
check("...and the season still deletes, since nothing manages it",
      rep["deleted_files"] == 2, rep)

# Sonarr has our show but not this season: writing the series back unchanged
# would report success having unmonitored nothing, and the re-download the
# unmonitor exists to prevent would happen anyway.
done, rep = sonarr_run([
    {"id": 5, "title": "A Show", "year": 2001, "seasons": [{"seasonNumber": 3, "monitored": True}]}])
check("a season Sonarr does not list is refused",
      done is False and "could not unmonitor" in why(rep), rep["skipped"])
check("...with no write to Sonarr", not PUTS, PUTS)
check("...and no file deleted", (SHOW / "Season 01" / "ep1.mkv").exists())

# The case that must still work: one match, right year, season present.
done, rep = sonarr_run([
    {"id": 9, "title": "A Show", "year": 1965, "seasons": [{"seasonNumber": 1, "monitored": True}]},
    {"id": 5, "title": "A Show", "year": 2001,
     "seasons": [{"seasonNumber": 1, "monitored": True},
                 {"seasonNumber": 2, "monitored": True}]}])
check("the right show, the right season, and only that season",
      done is True and len(PUTS) == 1 and PUTS[0][0].endswith("/series/5")
      and [s for s in PUTS[0][1]["seasons"] if s["seasonNumber"] == 1][0]["monitored"] is False
      and [s for s in PUTS[0][1]["seasons"] if s["seasonNumber"] == 2][0]["monitored"] is True,
      PUTS)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
