"""Series-level watch facts on the TV inventory, from Tautulli + Jellyfin.

The rule under test is the same one the movie viewer count follows: counts
take the MAX across servers, never the sum. Both servers watch the same
audience, so a household using both would otherwise be counted twice — and an
inflated engagement number is a deletion decision made on invented data.

The join is by normalized title, because that is the one name all three
systems (Sonarr, Tautulli, Jellyfin) share for a series they each id their
own way.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-tv-watch.")
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


CFG = {"USE_PLEX": True, "USE_JELLYFIN": True,
       "TAUTULLI_URL": "http://t:8181", "TAUTULLI_API_KEY": "tk",
       "PLEX_URL": "http://p:32400", "PLEX_TOKEN": "pt",
       "JELLYFIN_URL": "http://j:8096", "JELLYFIN_API_KEY": "jk",
       "PROTECTED_COLLECTIONS": ["Keep Shows"],
       "JELLYFIN_PROTECTED_COLLECTIONS": ["JF Keep"],
       "OUTPUT_DIR": _OUT}

# Tautulli: The Wire watched by 3 users, 2 distinct episodes (one episode seen
# twice); punctuated title. One page, recordsFiltered terminates the sweep.
TAUT_ROWS = [
    {"grandparent_title": "The Wire!", "user_id": 1, "rating_key": "e1", "date": 1_700_000_000,
     "parent_media_index": 1},
    {"grandparent_title": "The Wire!", "user_id": 2, "rating_key": "e1", "date": 1_700_100_000,
     "parent_media_index": 1},
    {"grandparent_title": "The Wire!", "user_id": 3, "rating_key": "e2", "date": 1_600_000_000,
     "parent_media_index": 2},
    {"grandparent_title": "Only On Plex", "user": "alice", "rating_key": "p1", "date": 1_650_000_000},
]
def fake_tautulli(conn, cmd, timeout=8, **params):
    assert cmd == "get_history" and params.get("media_type") == "episode"
    if int(params.get("start") or 0) > 0:
        return {"data": [], "recordsFiltered": len(TAUT_ROWS)}
    return {"data": list(TAUT_ROWS), "recordsFiltered": len(TAUT_ROWS)}

# Jellyfin: two users; THE WIRE watched by both (3 distinct episodes for one
# of them), later last-played than Tautulli's. PlayCount 2 on one episode.
JF_USERS = [{"Id": "u-a"}, {"Id": "u-b"}]
JF_ITEMS = {
    "u-a": [{"SeriesName": "THE WIRE", "Id": "j1", "ParentIndexNumber": 1,
             "UserData": {"PlayCount": 2, "LastPlayedDate": "2024-01-01T00:00:00Z"}},
            {"SeriesName": "THE WIRE", "Id": "j2", "ParentIndexNumber": 1,
             "UserData": {"PlayCount": 1, "LastPlayedDate": "2024-02-01T00:00:00Z"}},
            {"SeriesName": "THE WIRE", "Id": "j3", "ParentIndexNumber": 1,
             "UserData": {"PlayCount": 1, "LastPlayedDate": "2023-01-01T00:00:00Z"}}],
    "u-b": [{"SeriesName": "THE WIRE", "Id": "j1", "ParentIndexNumber": 1,
             "UserData": {"PlayCount": 1, "LastPlayedDate": "2023-06-01T00:00:00Z"}}],
}
def fake_jellyfin_get(url, key, path, params=None, timeout=15):
    if path == "/Users":
        return list(JF_USERS)
    if (params or {}).get("IncludeItemTypes") == "BoxSet":
        return {"Items": [{"Name": "JF Keep", "Id": "box1"},
                          {"Name": "Unrelated Set", "Id": "box2"}]}
    if (params or {}).get("ParentId") == "box1":
        return {"Items": [{"Type": "Series", "Name": "The Wire"},
                          {"Type": "Movie", "Name": "Some Movie"}]}
    if (params or {}).get("ParentId") == "box2":
        return {"Items": [{"Type": "Series", "Name": "Nobody Watched This"}]}
    # The favorites sweep shares the per-user Items path; only the filter differs.
    if (params or {}).get("Filters") == "IsFavorite":
        return {"Items": [{"Name": "THE WIRE"}]} if "u-a" in path else {"Items": []}
    for uid, items in JF_ITEMS.items():
        if uid in path:
            return {"Items": list(items)}
    return {"Items": []}

def fake_plex_get(url, token, path, timeout=15):
    if path == "/library/sections":
        return {"MediaContainer": {"Directory": [
            {"type": "movie", "key": "1"}, {"type": "show", "key": "2"}]}}
    if path == "/library/sections/2/collections":
        return {"MediaContainer": {"Metadata": [
            {"title": "Keep Shows", "ratingKey": "c9"},
            {"title": "Other", "ratingKey": "c8"}]}}
    if path == "/library/collections/c9/children":
        return {"MediaContainer": {"Metadata": [{"title": "Plex Kept Show"}]}}
    return {}

A._tautulli_api_request = fake_tautulli
A._jellyfin_get = fake_jellyfin_get
A._plex_get = fake_plex_get

def rows():
    return [
        {"media_type": "tv", "title": "The Wire", "plays": 0, "users": 0,
         "last_played": 0, "tv_episodes": 10, "tv_episodes_watched": 0,
         "tv_seasons": [{"n": 1, "eps": 6, "size_bytes": 1, "eps_watched": 0, "last_played": 0},
                        {"n": 2, "eps": 4, "size_bytes": 1, "eps_watched": 0, "last_played": 0}]},
        {"media_type": "tv", "title": "Nobody Watched This", "plays": 0,
         "users": 0, "last_played": 0, "tv_episodes": 5, "tv_episodes_watched": 0},
        {"media_type": "tv", "title": "Plex Kept Show", "plays": 0,
         "users": 0, "last_played": 0, "tv_episodes": 5, "tv_episodes_watched": 0},
    ]

r = rows()
A._annotate_tv_watch(r, CFG)
wire, unwatched = r[0], r[1]

# Tautulli: 3 users, 3 plays, 2 eps. Jellyfin: 2 users, 5 plays, 3 eps.
check("users take the max across servers (3 vs 2 -> 3), never the sum",
      wire["users"] == 3, wire["users"])
check("plays take the max across servers (3 vs 5 -> 5)", wire["plays"] == 5, wire["plays"])
check("episodes watched are DISTINCT per server, then max (2 vs 3 -> 3)",
      wire["tv_episodes_watched"] == 3, wire["tv_episodes_watched"])
check("last watched is the latest either server saw (Jellyfin's 2024-02-01)",
      wire["last_played"] == 1706745600, wire["last_played"])
check("punctuation and case differences still join ('The Wire!' / 'THE WIRE' / 'The Wire')",
      wire["plays"] > 0)
check("a series ANY Jellyfin user favorited is flagged, joined by title",
      wire.get("favorite") is True)
check("a series in a protected Jellyfin box set is flagged protected",
      wire.get("protected") is True)
check("a series in a protected PLEX collection is flagged protected",
      r[2].get("protected") is True)
check("a series in an UNSELECTED collection is not protected",
      not unwatched.get("protected"))
check("...and an unfavorited one is not", not unwatched.get("favorite"))
check("a series nobody watched keeps zeros",
      unwatched["plays"] == 0 and unwatched["users"] == 0
      and unwatched["tv_episodes_watched"] == 0)

# Seasons: S1 has Tautulli 1 distinct ep vs Jellyfin 3 -> 3; S2 only Tautulli's
# 1. Season last-played is per season, not the series-wide latest.
s1, s2 = wire["tv_seasons"][0], wire["tv_seasons"][1]
check("a season's watched episodes take the max across servers (1 vs 3 -> 3)",
      s1["eps_watched"] == 3, s1)
check("a season only one server saw still counts (S2: 1)",
      s2["eps_watched"] == 1, s2)
check("season last-played is that SEASON's latest, not the series-wide latest",
      s1["last_played"] == 1706745600 and s2["last_played"] == 1_600_000_000,
      (s1["last_played"], s2["last_played"]))

# ── One server only: the other contributes nothing, not an error ────────────
r = rows()
A._annotate_tv_watch(r, {**CFG, "USE_JELLYFIN": False})
check("Plex-only: Tautulli's numbers stand alone",
      r[0]["users"] == 3 and r[0]["plays"] == 3 and r[0]["tv_episodes_watched"] == 2,
      (r[0]["users"], r[0]["plays"], r[0]["tv_episodes_watched"]))

# ── A server that fails to answer blocks nothing ────────────────────────────
def boom(*a, **k):
    raise RuntimeError("down")
A._tautulli_api_request = boom
r = rows()
A._annotate_tv_watch(r, CFG)
check("a dead Tautulli leaves Jellyfin's answers standing",
      r[0]["users"] == 2 and r[0]["plays"] == 5, (r[0]["users"], r[0]["plays"]))
A._jellyfin_get = boom
r = rows()
A._annotate_tv_watch(r, CFG)
check("both servers dead: rows keep zeros and nothing raises", r[0]["plays"] == 0)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
