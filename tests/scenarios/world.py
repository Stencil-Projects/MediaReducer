"""Build an exactly-specified MediaReducer world: library on disk, the matching
inventory the mock serves, and a config. No randomness anywhere — a scenario is
a dict, the same dict every run, on every machine.

Sizes on disk MUST equal what the mock reports, or the wrong-library tripwire
fails the run by design. Files are wholly SPARSE: the library measure is
st_blocks*512 falling back to st_size when blocks is zero, so a sparse file
counts at its apparent size. That is the only way a scenario holds a 40 GB
library on a runner with a few GB free — and without GB-scale libraries no
threshold is ever breached and the deletion path is never reached.

Dates are given in DAYS AGO rather than absolute epochs: the grace period and
staleness are measured against today, so "400 days ago" is the stable fact and
a fixed timestamp would drift into and out of every filter as the year passes.
"""
import json, time
from pathlib import Path

MB = 1024 * 1024


def iso(epoch): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def days_ago(n): return int(time.time()) - int(n * 86400)


def sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)


def build(base: Path, spec: dict) -> dict:
    """Realize `spec` on disk. Returns the world the mock serves.

    spec keys:
      movies   [{name, folder, mb, added, plays, imdb, favorite}]
      shows    [{name, folder, status, imdb, added,
                 seasons: [{n, episodes, mb, added, plays}]}]
      monitor  ["movies", "tv"]     config MONITOR_DIRS
      config   {...}                merged over the defaults below
    """
    lib, cfg_dir, ratings = base / "library", base / "config", base / "ratings"
    for d in (lib, cfg_dir, ratings):
        d.mkdir(parents=True, exist_ok=True)

    movies, series, episodes = [], [], []
    userdata = {"u1": [], "u2": []}
    imdb_rows = {}

    def watch(uid, item_id, plays, favorite=False, played_days=30):
        userdata[uid].append({"Id": item_id, "UserData": {
            "PlayCount": plays, "Played": plays > 0,
            "LastPlayedDate": iso(days_ago(played_days)) if plays else None,
            "IsFavorite": bool(favorite)}})

    for i, m in enumerate(spec.get("movies", []), 1):
        folder, name = m.get("folder", "movies"), m["name"]
        size = int(m.get("mb", 500)) * MB
        sparse(lib / folder / name / f"{name}.mkv", size)
        mid = f"m{i}"
        prov = {"Imdb": f"tt9{i:06d}"} if m.get("imdb") is not None else {}
        if m.get("imdb") is not None:
            imdb_rows[f"tt9{i:06d}"] = (m["imdb"], m.get("votes", 5000))
        movies.append({"Id": mid, "Name": name, "ProductionYear": 2000,
                       "Path": f"/{folder}/{name}/{name}.mkv",
                       "DateCreated": iso(days_ago(m.get("added", 400))),
                       "ProviderIds": prov,
                       "MediaSources": [{"Size": size, "MediaStreams": []}]})
        watch("u1", mid, int(m.get("plays", 0)), m.get("favorite", False))
        watch("u2", mid, 0)

    for s, sh in enumerate(spec.get("shows", []), 1):
        folder, name = sh.get("folder", "tv"), sh["name"]
        sid = f"s{s}"
        prov = {"Imdb": f"tt8{s:06d}"} if sh.get("imdb") is not None else {}
        if sh.get("imdb") is not None:
            imdb_rows[f"tt8{s:06d}"] = (sh["imdb"], sh.get("votes", 5000))
        series.append({"Id": sid, "Name": name, "ProductionYear": 2000,
                       "Path": f"/{folder}/{name}",
                       "DateCreated": iso(days_ago(sh.get("added", 800))),
                       "Status": sh.get("status", "Ended"), "ProviderIds": prov})
        for sn in sh["seasons"]:
            n, n_eps = sn["n"], int(sn.get("episodes", 6))
            per = int(sn.get("mb", 300)) * MB
            for ep in range(1, n_eps + 1):
                fn = f"{name} - S{n:02d}E{ep:02d}.mkv"
                sparse(lib / folder / name / f"Season {n}" / fn, per)
                eid = f"e{s}_{n}_{ep}"
                episodes.append({"Id": eid, "SeriesId": sid, "ParentIndexNumber": n,
                                 "IndexNumber": ep, "Name": fn,
                                 "Path": f"/{folder}/{name}/Season {n}/{fn}",
                                 "DateCreated": iso(days_ago(sn.get("added", 700))),
                                 "MediaSources": [{"Size": per}]})
                watch("u1", eid, int(sn.get("plays", 0)))
                watch("u2", eid, 0)

    with open(ratings / "title.ratings.tsv", "w", encoding="utf-8") as fh:
        fh.write("tconst\taverageRating\tnumVotes\n")
        for tid, (rating, votes) in sorted(imdb_rows.items()):
            fh.write(f"{tid}\t{rating}\t{votes}\n")

    world = {"movies": movies, "series": series, "episodes": episodes,
             "userdata": userdata}
    (base / "world.json").write_text(json.dumps(world), encoding="utf-8")

    monitor = spec.get("monitor", ["movies", "tv"])
    monitored_bytes = sum(f.stat().st_size for d in monitor
                          for f in (lib / d).rglob("*") if f.is_file()) if monitor else 0
    cfg = {
        "RUN_MODE": "paused", "USE_PLEX": False, "USE_JELLYFIN": True,
        "JELLYFIN_URL": "http://127.0.0.1:0", "JELLYFIN_API_KEY": "k",
        "MONITOR_DIRS": list(monitor),
        "HEADROOM_GB": 0, "REDLINE_GB": None,
        # A fraction of what is monitored, so a run has a real, reachable
        # deficit. Scenarios that need BOTH media types to be taken ask for a
        # deeper one — at half, the movies alone often cover it and the season
        # side is never reached, which reads as "TV did not delete" when the
        # truth is "TV was not needed".
        "MAX_LIBRARY_GB": (monitored_bytes * float(spec.get("cap_fraction", 0.5)) / 1e9)
                          if monitored_bytes else None,
        "DELETE_DELAY_DAYS": 1,
        "MOVIE_CLEANUP_ENABLED": True, "TV_CLEANUP_ENABLED": True,
        "SCORE_BALANCE": 50, "NEAR_TIE_PTS": 2, "GRACE_PERIOD_DAYS": 30,
        "MAX_IMDB_RATING": None, "SKIP_UNPLAYED_MOVIES": False,
        "PROTECT_JELLYFIN_FAVORITES": False, "MAX_STALENESS_MONTHS": 12,
        "TV_SEASON_RULE": "all", "TV_MAX_SEASON_EPISODES": 0,
        "TV_WATCH_WEIGHT": 100, "TV_SERIES_WATCH_BUMP": 10,
        "MAX_HEADROOM_PCT": 100, "OUTPUT_DIR": str(cfg_dir),
        "IMDB_RATINGS_URL": "http://127.0.0.1:9/never",
    }
    cfg.update(spec.get("config", {}))
    if cfg.get("MAX_LIBRARY_GB") == "__none__":
        cfg["MAX_LIBRARY_GB"] = None
    (cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return world
