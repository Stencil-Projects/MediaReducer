"""Which files a season deletion is about to remove, asked of the server that
indexed the series.

_tv_season_relpaths is the list that becomes an rm. It asks the media server
for the series folder and the season's episode files, and returns each one
RELATIVE to that folder — so the deleter can only ever reach inside the series
it was given, whatever the server said.

Coverage found the Plex half of it had never run. The fixtures index TV through
Jellyfin, so every line from "elif sid.startswith('plex:')" to the end of that
branch was dead as far as the suite was concerned — including the fallback that
infers the series folder from the first episode's path when Plex reports no
Location, which is the one piece of arithmetic here that can point the deleter
at the wrong directory.

The fail-closed returns were unrun too. Each one is a case where the server did
not tell us enough, and the season has to be skipped rather than guessed at;
they are checked here one at a time, because "returns an error" is only useful
if it is the RIGHT error — the caller shows it to whoever has to fix it.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
_tmpout.config()
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


CFG = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok",
       "JELLYFIN_URL": "http://jf:8096", "JELLYFIN_API_KEY": "jfkey"}

# The two servers, stubbed at the one call each branch makes. Neither is
# reachable from a test and neither needs to be: what is under test is how the
# reply is turned into a delete list.
plex_replies: dict = {}
jf_replies: dict = {}


def _plex(url, token, path, timeout=15):
    for frag, reply in plex_replies.items():
        if frag in path:
            if isinstance(reply, Exception):
                raise reply
            return reply
    return {}


def _jf(url, key, path, params=None, timeout=15):
    for frag, reply in jf_replies.items():
        if frag in path:
            if isinstance(reply, Exception):
                raise reply
            return reply
    return {}


A._plex_get = _plex
A._jellyfin_get = _jf


def rels(sid, season, cfg=CFG, series_name="The Show"):
    out, err = A._tv_season_relpaths(cfg, sid, season, series_name)
    return [(str(p), n) for p, n in out], err


def plex_show(location, episodes):
    """A Plex metadata reply for the series, and an allLeaves reply holding
    `episodes` as (season number, path, size)."""
    show = {"MediaContainer": {"Metadata": [{}]}}
    if location is not None:
        show["MediaContainer"]["Metadata"][0]["Location"] = location
    leaves = {"MediaContainer": {"Metadata": [
        {"parentIndex": s, "Media": [{"Part": [{"file": f, "size": n}]}]}
        for s, f, n in episodes]}}
    return {"/allLeaves": leaves, "/library/metadata/": show}


# ── Plex: the branch the suite never ran ────────────────────────────────────
plex_replies = plex_show([{"path": "/tv/The Show"}], [
    (1, "/tv/The Show/Season 01/s01e01.mkv", 100),
    (1, "/tv/The Show/Season 01/s01e02.mkv", 200),
    (2, "/tv/The Show/Season 02/s02e01.mkv", 300),
])
files, err = rels("plex:1234", 1)
check("a Plex season lists only its own episodes",
      err is None and files == [("Season 01/s01e01.mkv", 100),
                                ("Season 01/s01e02.mkv", 200)], (files, err))
files, err = rels("plex:1234", "2")
check("...and the season number matches across a string and an int",
      err is None and files == [("Season 02/s02e01.mkv", 300)], (files, err))
files, err = rels("plex:1234", 3)
check("a season the server lists nothing for is refused, not emptied",
      files == [] and err == "the server lists no files for this season", (files, err))

# An episode whose parentIndex is missing or junk cannot be attributed to a
# season. Skipping it is right; counting it into whichever season is asking
# would delete an episode of a different one.
plex_replies = plex_show([{"path": "/tv/The Show"}], [
    (1, "/tv/The Show/Season 01/s01e01.mkv", 100),
    (None, "/tv/The Show/Specials/extra.mkv", 50),
    ("x", "/tv/The Show/Specials/other.mkv", 50),
])
files, err = rels("plex:1234", 1)
check("an episode with no usable season number is left alone",
      files == [("Season 01/s01e01.mkv", 100)], (files, err))

# Plex reporting no Location: the series folder is identified by NAME from the
# episode's own path, not by counting levels. Both library shapes have to land
# on the series folder, and a fixed two-levels-up only ever suits one of them.
plex_replies = plex_show(None, [(1, "/tv/The Show/Season 01/s01e01.mkv", 100)])
files, err = rels("plex:1234", 1)
check("with no Location, the series folder is found by name in the path",
      err is None and files == [("Season 01/s01e01.mkv", 100)], (files, err))
plex_replies = plex_show(None, [(1, "/tv/The Show/s01e01.mkv", 100)])
files, err = rels("plex:1234", 1)
check("...and a season-less layout lands on the series, not the folder above it",
      err is None and files == [("s01e01.mkv", 100)], (files, err))
# Deeper still, and the name is what finds it rather than the depth.
plex_replies = plex_show(None, [(1, "/m/tv/T/The Show/S01/Disc 1/e1.mkv", 100)])
files, err = rels("plex:1234", 1)
check("...at any depth, since the name is what is being looked for",
      err is None and files == [("S01/Disc 1/e1.mkv", 100)], (files, err))
# No ancestor answers to the name: the paths cannot be made relative to a
# folder we have not identified, so the season is refused rather than joined
# to whatever a level count landed on.
plex_replies = plex_show(None, [(1, "/tv/A Different Show/Season 01/e1.mkv", 100)])
files, err = rels("plex:1234", 1)
check("a path that never mentions our series folder is refused",
      files == [] and err == "the server states no path for the series", (files, err))

# ── Jellyfin: the same shape from the other server ─────────────────────────
jf_replies = {
    "/Items": {"Items": [{"Path": "/tv/The Show"}]},
    "/Episodes": {"Items": [
        {"Path": "/tv/The Show/Season 01/e1.mkv", "ParentIndexNumber": 1,
         "MediaSources": [{"Size": 111}]},
        {"Path": "/tv/The Show/Season 02/e1.mkv", "ParentIndexNumber": 2,
         "MediaSources": [{"Size": 222}]},
        {"ParentIndexNumber": 1},                       # no path at all
        {"Path": "/tv/The Show/Season 01/e2.mkv", "ParentIndexNumber": None},
    ]},
}
files, err = rels("jellyfin:abc", 1)
check("a Jellyfin season lists only its own episodes, skipping the pathless",
      err is None and files == [("Season 01/e1.mkv", 111)], (files, err))

# A size the server states as junk is a size we do not have. Zero means "ask
# the disk", which is what the deleter does anyway; raising here would abort a
# season over a metadata typo.
jf_replies["/Episodes"] = {"Items": [
    {"Path": "/tv/The Show/Season 01/e1.mkv", "ParentIndexNumber": 1,
     "MediaSources": [{"Size": "not a number"}, {"Size": 999}]},
]}
files, err = rels("jellyfin:abc", 1)
check("an unreadable size is skipped in favour of a readable one",
      files == [("Season 01/e1.mkv", 999)], (files, err))
jf_replies["/Episodes"]["Items"][0]["MediaSources"] = [{"Size": "nope"}]
files, err = rels("jellyfin:abc", 1)
check("...and with no readable size at all the file still lists, at zero",
      files == [("Season 01/e1.mkv", 0)], (files, err))

# ── Fail closed, and say which gap it was ──────────────────────────────────
files, err = rels("plex:1", 1, {"JELLYFIN_URL": "u", "JELLYFIN_API_KEY": "k"})
check("a Plex row with the Plex connection gone is refused",
      files == [] and err == "the Plex connection that indexed this series is gone",
      (files, err))
files, err = rels("jellyfin:1", 1, {"PLEX_URL": "u", "PLEX_TOKEN": "k"})
check("a Jellyfin row with the Jellyfin connection gone is refused",
      files == [] and err == "the Jellyfin connection that indexed this series is gone",
      (files, err))
files, err = rels("sonarr:1", 1)
check("a row from no media server at all is refused",
      files == [] and err == "no usable media-server id", (files, err))

plex_replies = plex_show([], [])
files, err = rels("plex:1234", 1)
check("a series the server states no path for is refused",
      files == [] and err == "the server states no path for the series", (files, err))

plex_replies = {"/library/metadata/": ConnectionError("plex went away")}
files, err = rels("plex:1234", 1)
check("the server failing mid-listing is refused, with the reason carried",
      files == [] and err and err.startswith("could not list the season's files")
      and "plex went away" in err, (files, err))

# The last line of defence, and the reason every path comes back relative: a
# server that reports a file outside the series folder must not produce a
# relative path that climbs back out of it.
plex_replies = plex_show([{"path": "/tv/The Show"}],
                         [(1, "/tv/Another Show/Season 01/s01e01.mkv", 100)])
files, err = rels("plex:1234", 1)
check("a file outside the series folder is refused, not made relative to it",
      files == [] and err and err.startswith("unsafe file path from the media server"),
      (files, err))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
