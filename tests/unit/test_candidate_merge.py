"""One file, listed twice, becomes one candidate — and its plays are right.

Two servers watching the same library is the normal case for anyone running
Plex and Jellyfin side by side, and each reports its own plays of the same
file. The merge has to add those, because they are different viewings, while
adding the same server's own repeat listing (one file in two sections) would
count the same viewings twice and make the file look twice as loved as it is.

Plays feed the retention score, and the retention score is deletion order, so
getting this wrong does not raise an error — it silently reorders what gets
deleted. Nothing covered it until now: no scenario puts one movie on both
servers with plays on each, which was found by breaking the sum and watching
the whole suite stay green.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
import engine as E                                 # noqa: E402

# AFTER the import, per _tmpout's own note: engine fixes its path constants at
# import time from the /config default, so redirecting the config file is not
# enough. Merging two rows logs a line, and without this that line is written
# into the deployment's real lastrun.log.
_tmpout.redirect_engine(E)
E.log = lambda *a, **k: None

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def row(**kw):
    """A candidate as build_candidates assembles one, with only the merge
    inputs spelled out — every field the merge reads has to be present or the
    test would be asserting against defaults it invented."""
    c = {"path": Path("/library/Movies/Bob (2020)/Bob.mkv"), "title": "Bob",
         "play_count": 0, "last_played": 0, "distinct_users": 0,
         "source": "plex", "resolution": "", "bitrate": 0,
         "section_id": None, "tmdb_id": None, "imdb_id": None,
         "imdb_rating": None, "imdb_votes": None}
    c.update(kw)
    return c


# ── Plays: sum across servers, max within one ────────────────────────────────
merged, removed = E._merge_candidates_by_path([
    row(source="plex", play_count=3, last_played=100),
    row(source="jellyfin", play_count=2, last_played=250),
])
check("two rows for one file collapse to one", len(merged) == 1 and removed == 1,
      (len(merged), removed))
check("...plays from DIFFERENT servers add up", merged[0]["play_count"] == 5,
      merged[0]["play_count"])
check("...and the most recent watch wins", merged[0]["last_played"] == 250,
      merged[0]["last_played"])

merged, _ = E._merge_candidates_by_path([
    row(source="plex", play_count=3),
    row(source="plex", play_count=2),
])
check("the SAME server listing a file twice is not double-counted",
      merged[0]["play_count"] == 3, merged[0]["play_count"])

# Three rows, two servers: the repeat must not add, the second server must.
merged, removed = E._merge_candidates_by_path([
    row(source="plex", play_count=3),
    row(source="plex", play_count=1),
    row(source="jellyfin", play_count=4),
])
check("a repeat and a second server together: 3 and 4, not 8",
      merged[0]["play_count"] == 7 and removed == 2,
      (merged[0]["play_count"], removed))

# ── The other scoring inputs take the better of the two ──────────────────────
merged, _ = E._merge_candidates_by_path([
    row(distinct_users=1, resolution="", bitrate=500),
    row(source="jellyfin", distinct_users=4, resolution="1080p", bitrate=9000),
])
m = merged[0]
check("distinct viewers take the higher count", m["distinct_users"] == 4, m["distinct_users"])
check("a blank field is filled from the other row", m["resolution"] == "1080p", m["resolution"])
check("bitrate takes the larger", m["bitrate"] == 9000, m["bitrate"])

# An IMDb id arriving on the second row brings its rating with it. They travel
# together on purpose: a rating without the id it came from cannot be checked.
merged, _ = E._merge_candidates_by_path([
    row(),
    row(source="jellyfin", imdb_id="tt1", imdb_rating=7.5, imdb_votes=900),
])
m = merged[0]
check("an IMDb id fills in from whichever row has it", m["imdb_id"] == "tt1", m["imdb_id"])
check("...and its rating and votes come with it",
      m["imdb_rating"] == 7.5 and m["imdb_votes"] == 900, (m["imdb_rating"], m["imdb_votes"]))

# A row that already has an id keeps it — the first one wins, so a merge can
# never silently repoint a file at a different film.
merged, _ = E._merge_candidates_by_path([
    row(imdb_id="tt1", imdb_rating=7.5),
    row(source="jellyfin", imdb_id="tt2", imdb_rating=2.0),
])
check("an existing IMDb id is never overwritten by the other row",
      merged[0]["imdb_id"] == "tt1" and merged[0]["imdb_rating"] == 7.5,
      (merged[0]["imdb_id"], merged[0]["imdb_rating"]))

# Radarr's section marks a movie as Radarr-managed; losing it in a merge would
# make the run stop telling Radarr about a deletion it needs to know about.
merged, _ = E._merge_candidates_by_path([
    row(section_id="7"),
    row(source="jellyfin", section_id=E.RADARR_OVERSEERR_SECTION_ID),
])
check("the Radarr section survives a merge",
      str(merged[0]["section_id"]) == str(E.RADARR_OVERSEERR_SECTION_ID),
      merged[0]["section_id"])

# ── Distinct files are left alone ────────────────────────────────────────────
merged, removed = E._merge_candidates_by_path([
    row(path=Path("/library/Movies/Bob (2020)/Bob.mkv"), play_count=1),
    row(path=Path("/library/Movies/Ann (2021)/Ann.mkv"), play_count=2),
])
check("two different files stay two candidates", len(merged) == 2 and removed == 0,
      (len(merged), removed))
check("...with their own play counts intact",
      [c["play_count"] for c in merged] == [1, 2], [c["play_count"] for c in merged])

merged, removed = E._merge_candidates_by_path([])
check("an empty list is not a special case", merged == [] and removed == 0, (merged, removed))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
