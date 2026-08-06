"""The TV inventory comes from the MEDIA SERVERS — Jellyfin and/or Plex —
never from Sonarr (optional, cleanup-only). Pinned here: each fetcher's row
shape from its server's wire format (per-season episode counts and real
on-disk sizes, added date, status, IMDb id, the server-namespace series
path); the title-merge that folds two servers watching the same files into
one row carrying BOTH identities; and _tv_inventory_rows' strictness — the
deletion path raises when a configured server fails, the refresh path just
takes what answered.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-tv-inv.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

GB = 1_000_000_000

# ── Jellyfin: two admin queries → rows ──────────────────────────────────────
JF_SERIES = [
    {"Id": "j1", "Name": "Alpha Show", "Path": "/media/tv/Alpha Show",
     "ProductionYear": 2010, "DateCreated": "2020-01-01T00:00:00Z",
     "Status": "Ended", "ProviderIds": {"Imdb": "tt0001"}},
    {"Id": "j2", "Name": "Ghost Show", "Path": "/media/tv/Ghost Show",
     "ProductionYear": 2011},   # no episodes on disk → skipped
]
JF_EPISODES = [
    {"SeriesId": "j1", "ParentIndexNumber": 1, "MediaSources": [{"Size": 2 * GB}],
     "DateCreated": "2020-02-01T00:00:00Z"},
    {"SeriesId": "j1", "ParentIndexNumber": 1, "MediaSources": [{"Size": 3 * GB}],
     "DateCreated": "2020-03-01T00:00:00Z"},
    {"SeriesId": "j1", "ParentIndexNumber": 2, "MediaSources": [{"Size": 4 * GB}],
     "DateCreated": "2021-06-01T00:00:00Z"},
]
def fake_jf_get(url, key, path, params=None, timeout=15):
    kinds = (params or {}).get("IncludeItemTypes")
    if kinds == "Series":
        return {"Items": [dict(s) for s in JF_SERIES]}
    if kinds == "Episode":
        return {"Items": [dict(e) for e in JF_EPISODES]}
    raise AssertionError("unexpected Jellyfin request: " + path)
A._jellyfin_get = fake_jf_get

jf = A._jellyfin_series_inventory("http://jf.test", "k")
check("Jellyfin yields one row per series WITH files on disk",
      [r["title"] for r in jf] == ["Alpha Show"], jf)
row = jf[0]
check("the row carries the Jellyfin identity, never a Sonarr one",
      row["jf_source_id"] == "jellyfin:j1" and row["source_id"] is None, row)
check("seasons carry real episode counts and on-disk sizes",
      [(s["n"], s["eps"], s["size_bytes"]) for s in row["tv_seasons"]]
      == [(1, 2, 5 * GB), (2, 1, 4 * GB)], row["tv_seasons"])
check("each season's added date is its NEWEST episode file's",
      row["tv_seasons"][0]["added_at"] == 1583020800
      and row["tv_seasons"][1]["added_at"] > row["tv_seasons"][0]["added_at"],
      [s["added_at"] for s in row["tv_seasons"]])
check("series totals sum the seasons",
      row["size_bytes"] == 9 * GB and row["tv_episodes"] == 3, row)
check("status, IMDb id, year, added date and the server path ride along",
      row["tv_status"] == "ended" and row["imdb_id"] == "tt0001"
      and row["year"] == 2010 and row["added_at"] > 0
      and row["path"] == "/media/tv/Alpha Show", row)

# ── Plex: sections → shows → episodes-with-parts ────────────────────────────
PLEX_ROUTES = {
    "/library/sections": {"MediaContainer": {"Directory": [
        {"type": "movie", "key": "1"}, {"type": "show", "key": "2"}]}},
    "/library/sections/2/all?type=2&includeGuids=1": {"MediaContainer": {"Metadata": [
        {"ratingKey": "77", "title": "Alpha Show", "year": 2010, "addedAt": 1_600_000_000,
         "Location": [{"path": "/data/tv/Alpha Show"}],
         "Guid": [{"id": "tvdb://9"}, {"id": "imdb://tt0001"}]},
        {"ratingKey": "78", "title": "Beta Show", "year": 2012, "addedAt": 1_650_000_000},
    ]}},
    "/library/sections/2/all?type=4": {"MediaContainer": {"Metadata": [
        {"grandparentRatingKey": "77", "parentIndex": 1, "addedAt": 1_700_000_000,
         "Media": [{"Part": [{"size": 6 * GB, "file": "/data/tv/Alpha Show/Season 01/e1.mkv"}]}]},
        {"grandparentRatingKey": "77", "parentIndex": 3,
         "Media": [{"Part": [{"size": 7 * GB, "file": "/data/tv/Alpha Show/Season 03/e1.mkv"}]}]},
        {"grandparentRatingKey": "78", "parentIndex": 1,
         "Media": [{"Part": [{"size": 1 * GB, "file": "/data/tv/Beta Show/Season 01/e1.mkv"}]}]},
    ]}},
}
def fake_plex_get(url, token, path, timeout=15):
    if path in PLEX_ROUTES:
        return json.loads(json.dumps(PLEX_ROUTES[path]))
    raise AssertionError("unexpected Plex request: " + path)
A._plex_get = fake_plex_get

px = A._plex_series_inventory({"plex_url": "http://plex.test", "plex_token": "t"})
by_title = {r["title"]: r for r in px}
check("Plex yields a row per show with files, from the show sections only",
      sorted(by_title) == ["Alpha Show", "Beta Show"], sorted(by_title))
alpha = by_title["Alpha Show"]
check("the row carries the Plex identity and NO status (Plex states none)",
      alpha["source_id"] == "plex:77" and alpha["tv_status"] is None, alpha)
check("seasons sum their parts",
      [(s["n"], s["eps"], s["size_bytes"]) for s in alpha["tv_seasons"]]
      == [(1, 1, 6 * GB), (3, 1, 7 * GB)], alpha["tv_seasons"])
check("the IMDb guid is read out of the guid list", alpha["imdb_id"] == "tt0001")
check("a show with no Location derives its folder from an episode file",
      by_title["Beta Show"]["path"] == "/data/tv/Beta Show", by_title["Beta Show"])

# ── The merge: two servers, one library, one row ────────────────────────────
merged = A._merge_tv_sources(jf, px)
m = {r["title"]: r for r in merged}
check("a series both servers see becomes ONE row", len(merged) == 2, [r["title"] for r in merged])
both = m["Alpha Show"]
check("...carrying BOTH identities for the deletion pass",
      both["source_id"] == "plex:77" and both["jf_source_id"] == "jellyfin:j1", both)
check("...with the known status filled from the side that has one",
      both["tv_status"] == "ended", both)
check("...season sizes keep the larger measurement, and a season only one "
      "side lists is kept",
      [(s["n"], s["size_bytes"]) for s in both["tv_seasons"]]
      == [(1, 6 * GB), (2, 4 * GB), (3, 7 * GB)], both["tv_seasons"])
check("...and a merged season keeps the LATER added date",
      both["tv_seasons"][0]["added_at"] == 1_700_000_000, both["tv_seasons"][0])
check("...and the series totals follow the merged seasons",
      both["size_bytes"] == 17 * GB and both["tv_episodes"] == 4, both)

# ── Strictness: the deletion path never works from a partial inventory ──────
CFG = {"USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test",
       "JELLYFIN_API_KEY": "k", "USE_PLEX": True,
       "PLEX_URL": "http://plex.test", "PLEX_TOKEN": "t",
       "TAUTULLI_URL": "", "TAUTULLI_API_KEY": "",
       "SONARR_URL": "", "SONARR_API_KEY": ""}
A._jellyfin_series_inventory = lambda url, key: None          # server down
A._plex_series_inventory = lambda conn: px
check("non-strict: a dead server contributes nothing, the other still answers",
      [r["title"] for r in A._tv_inventory_rows(CFG)] == ["Alpha Show", "Beta Show"])
try:
    A._tv_inventory_rows(CFG, strict=True)
    strict_raised = False
except RuntimeError:
    strict_raised = True
check("strict: a dead CONFIGURED server raises instead", strict_raised)
A._plex_series_inventory = lambda conn: None
check("no server answering at all reads as None, never as an empty library",
      A._tv_inventory_rows(CFG) is None)
check("Sonarr alone can never supply the inventory",
      A._tv_inventory_rows(dict(CFG, USE_JELLYFIN=False, USE_PLEX=False,
                                SONARR_URL="http://sonarr.test", SONARR_API_KEY="k")) is None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
