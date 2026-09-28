"""A show out of scope because its folder is AMBIGUOUS is labelled as that,
not as "off monitored paths".

Two things take a show out of scope: its folder is under no monitored directory,
or its folder is ambiguous — two inventory rows claim it (the servers title or
date the show differently), or its name is found under two monitored
directories. _resolve_tv_scope records the second as tv_scope_conflict, but the
store dropped the field, so the Filtering & Scoring page labelled every such
season "off monitored paths". Seen in the test lab with "The Office (2005)",
which sits in the monitored /library/tv beside four shows that are in scope:
the label sent the user to their path settings for a problem that is not there.

Pinned: the conflict survives the store; the season plan counts it apart from
off-path; the page payload says only THAT there was one (the conflict is a list
of folder paths, and paths never leave the server); and the page labels it.
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
_OUT = tempfile.mkdtemp(prefix="mr-tv-ambiguous.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A  # noqa: E402
import db       # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


NOW = 1_700_000_000
FOLDER = "/library/tv/The Office (2005)"
SEASONS = [{"n": 1, "eps": 3, "size_bytes": 3_700_000_000, "eps_watched": 0,
            "last_played": 0, "added_at": NOW - 400 * 86400},
           {"n": 2, "eps": 3, "size_bytes": 1_900_000_000, "eps_watched": 0,
            "last_played": 0, "added_at": NOW - 400 * 86400}]


def row(title, *, conflict=None, in_scope=0):
    return {"media_type": "tv", "title": title, "year": 2005, "tv_status": "ended",
            "tv_in_scope": in_scope, "path": None, "tv_scope_conflict": conflict,
            "protected": False, "favorite": False, "users": 0, "last_played": 0,
            "added_at": NOW - 400 * 86400, "rating": 9.0, "votes": 850000,
            "tv_episodes": 6, "tv_episodes_watched": 0, "tv_seasons": SEASONS,
            "size_bytes": 5_600_000_000}


# ── The store keeps it ────────────────────────────────────────────────────
store = A.db_path()
with db.transaction(store) as conn:
    db.replace_tv_series(conn, [row("The Office", conflict=[FOLDER]),
                                row("Unmonitored Show")])
    db.set_meta(conn, "snapshot_built_at", NOW)   # as a completed scan stamps it
with db.connect(store) as conn:
    tv = {r["title"]: r for r in (db.read_snapshot(conn) or {}).get("movies", [])
          if r.get("media_type") == "tv"}
check("an ambiguous show's conflict survives the store",
      tv["The Office"].get("tv_scope_conflict") == [FOLDER], tv["The Office"].get("tv_scope_conflict"))
check("...and a plainly unmonitored show carries none",
      not tv["Unmonitored Show"].get("tv_scope_conflict"), tv["Unmonitored Show"])

# ── The plan counts it apart ─────────────────────────────────────────────
CFG = {"SCORE_BALANCE": 0, "GRACE_PERIOD_DAYS": 0, "MAX_IMDB_RATING": None,
       "MAX_STALENESS_MONTHS": 36, "TV_SERIES_WATCH_BUMP": 10, "TV_WATCH_WEIGHT": 100,
       "SKIP_UNPLAYED_MOVIES": False, "TV_SEASON_ELIGIBILITY": "all"}
plan = A._tv_season_plan([row("The Office", conflict=[FOLDER]), row("Unmonitored Show")],
                         CFG, now=NOW)
ex = plan["excluded"]
check("neither contributes a season", not plan["order"], plan["order"])
check("the ambiguous show's seasons are counted as an ambiguous folder",
      ex.get("ambiguous_folder") == 2, ex)
check("...and only the unmonitored show's as off-path", ex.get("off_path") == 2, ex)

# ── The page is told THAT, never WHERE ────────────────────────────────────
A._read_library_snapshot = lambda: ({"built_at": NOW, "movies": [
    row("The Office", conflict=[FOLDER]), row("Unmonitored Show")]}, None)
A.app.config["TESTING"] = True
body = A.app.test_client().get("/api/library-snapshot").get_json()
by = {m["title"]: m for m in body["movies"]}
check("the payload marks the ambiguous show", by["The Office"].get("tv_scope_conflict") is True,
      by["The Office"].get("tv_scope_conflict"))
check("...and not the unmonitored one", not by["Unmonitored Show"].get("tv_scope_conflict"))
check("...without a single folder path in it", FOLDER not in json.dumps(body))

js = (ROOT / "static" / "js" / "explorer.js").read_text(encoding="utf-8")
check("the page labels an ambiguous folder as such",
      "return m.tvScopeConflict?'tv_ambiguous':'tv_oos';" in js
      and "tv_ambiguous:'ambiguous folder'" in js)
check("...and explains it rather than pointing at the path settings",
      "tv_ambiguous:'Two library entries claim this series folder" in js)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
