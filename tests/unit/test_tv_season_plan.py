"""The TV season plan: every in-scope season scored on the movie 0-100 scale
(season_retention_score) and ordered worst-first — the order that intersplices
with the movie deletion order.

Pinned here: who steps aside entirely (protected, favorited, off-path, the
latest season of a continuing show, and now the SHARED eligibility filters at
season grain — grace period, IMDb cutoff, no-IMDb-data, skip-unplayed); and
that the order is ascending by the unified score with ties going to the larger
season. Sizing the take prefix against the pool deficit belongs to
_merged_pool_takes and is pinned in test_pool_split.py.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-tv-plan.")
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


NOW = 1_700_000_000
DAY = 86400
GB = 1_000_000_000

# 100% watch history: the scores are the pure season recipe, no IMDb terms,
# and the no-IMDb-data / cutoff filters are off (mirroring the movie rules).
CFG = {"SCORE_BALANCE": 0, "GRACE_PERIOD_DAYS": 0, "MAX_IMDB_RATING": None,
       "MAX_STALENESS_MONTHS": 36, "TV_SERIES_WATCH_BUMP": 10,
       "TV_WATCH_WEIGHT": 100, "SKIP_UNPLAYED_MOVIES": False,
       # Favorites shield TV under the SAME eligibility toggle movies use.
       "PROTECT_JELLYFIN_FAVORITES": True,
       # The general sections run with every season eligible; the
       # season-eligibility modes (default: oldest) have their own section.
       "TV_SEASON_ELIGIBILITY": "all"}

def series(title, seasons, *, status="ended", in_scope=1, protected=False,
           favorite=False, users=0, last=0, added=NOW - 800 * DAY,
           rating=None, votes=0, s_eps=None, s_watched=None):
    eps = s_eps if s_eps is not None else sum(s.get("eps") or 0 for s in seasons)
    watched = s_watched if s_watched is not None else sum(s.get("eps_watched") or 0 for s in seasons)
    return {"media_type": "tv", "title": title, "tv_status": status,
            "tv_in_scope": in_scope, "protected": protected, "favorite": favorite,
            "users": users, "last_played": last, "added_at": added,
            "rating": rating, "votes": votes, "tv_episodes": eps,
            "tv_episodes_watched": watched, "tv_seasons": seasons}

def season(n, *, eps=10, watched=0, size=10 * GB, last=0):
    return {"n": n, "eps": eps, "eps_watched": watched, "size_bytes": size,
            "last_played": last}

ROWS = [
    # Half-consumed dead show: S1 fully watched 100 days ago, S2 untouched.
    series("Dead Show", [season(1, watched=10, last=NOW - 100 * DAY),
                         season(2, watched=0)], users=1),
    # Alive continuing show: S3 current (excluded), S1 watched long ago, S2 untouched.
    series("Alive Show", [season(1, watched=10, last=NOW - 400 * DAY),
                          season(2, watched=0),
                          season(3, watched=1, last=NOW - 1 * DAY)],
           status="continuing", users=2, last=NOW - 1 * DAY),
    # Whole-series exclusions.
    series("Protected Show", [season(1), season(2)], protected=True),
    series("Favorite Show", [season(1)], favorite=True),
    series("Off Path Show", [season(1), season(2), season(3)], in_scope=0),
    # Two shows nobody has ever touched, added long past the staleness cliff:
    # both score 0 — the tie goes to the LARGER season.
    series("Tiny Dead", [season(1, size=1 * GB)], added=NOW - 3000 * DAY),
    series("Huge Dead", [season(1, size=60 * GB)], added=NOW - 3000 * DAY),
]

plan = A._tv_season_plan(ROWS, CFG, now=NOW)
order, ex = plan["order"], plan["excluded"]
key = [(e["title"], e["season"]) for e in order]

# ── Who steps aside ─────────────────────────────────────────────────────────
check("a protected series contributes no seasons",
      all(t != "Protected Show" for t, _ in key) and ex["protected"] == 2)
check("a favorited series contributes no seasons",
      all(t != "Favorite Show" for t, _ in key) and ex["favorite"] == 1)
check("an off-path series contributes no seasons",
      all(t != "Off Path Show" for t, _ in key) and ex["off_path"] == 3)
check("a continuing show's latest season is never in the order",
      ("Alive Show", 3) not in key and ex["latest_of_continuing"] == 1)
check("...but its OLD seasons are", ("Alive Show", 1) in key)
check("an ENDED show's latest season has no such shield", ("Dead Show", 2) in key)
unfav = A._tv_season_plan(ROWS, dict(CFG, PROTECT_JELLYFIN_FAVORITES=False), now=NOW)
check("with the favorites toggle off a favorited series is fair game",
      any(e["title"] == "Favorite Show" for e in unfav["order"]),
      [e["title"] for e in unfav["order"]])
# Plex states no continuing/ended status; unknown gets the shield too.
nostatus = A._tv_season_plan(
    [series("Mystery Status", [season(1), season(2)], status=None)], CFG, now=NOW)
check("a show with UNKNOWN status shields its latest season like a continuing one",
      [(e["title"], e["season"]) for e in nostatus["order"]] == [("Mystery Status", 1)]
      and nostatus["excluded"]["latest_of_continuing"] == 1, nostatus["excluded"])

# ── The shared filters, at season grain ─────────────────────────────────────
graced = A._tv_season_plan(
    [series("New Show", [season(1)], added=NOW - 5 * DAY)],
    dict(CFG, GRACE_PERIOD_DAYS=30), now=NOW)
check("the grace period holds back a freshly-added show",
      not graced["order"] and graced["excluded"]["recently_added"] == 1, graced["excluded"])
# Grace is judged per SEASON on its own added date: a brand-new season of an
# old show is graced while its old sibling stays in the order.
new_season = A._tv_season_plan(
    [series("Old Show", [dict(season(1), added_at=NOW - 700 * DAY),
                         dict(season(2), added_at=NOW - 3 * DAY)],
            added=NOW - 700 * DAY)],
    dict(CFG, GRACE_PERIOD_DAYS=30), now=NOW)
check("a brand-new season of an OLD show is graced; its old sibling is not",
      [(e["title"], e["season"]) for e in new_season["order"]] == [("Old Show", 1)]
      and new_season["excluded"]["recently_added"] == 1, new_season["excluded"])

imdb_cfg = dict(CFG, SCORE_BALANCE=70, MAX_IMDB_RATING=7.5)
cut = A._tv_season_plan(
    [series("Prestige", [season(1)], rating=8.4, votes=100_000),
     series("Mystery", [season(1)]),                         # no rating at all
     series("Junk", [season(2)], rating=4.1, votes=50_000)],
    imdb_cfg, now=NOW)
check("the IMDb cutoff protects a high-rated show's seasons",
      ("Prestige", 1) not in [(e["title"], e["season"]) for e in cut["order"]]
      and cut["excluded"]["high_rated"] == 1, cut["excluded"])
check("no IMDb data means not enough to judge — held back when IMDb is in use",
      cut["excluded"]["no_imdb_data"] == 1
      and [(e["title"], e["season"]) for e in cut["order"]] == [("Junk", 2)],
      cut["excluded"])

unplayed = A._tv_season_plan(
    [series("Half", [season(1, watched=3, last=NOW - 50 * DAY), season(2)], users=1)],
    dict(CFG, SKIP_UNPLAYED_MOVIES=True), now=NOW)
check("skip-unplayed holds back the untouched season but not the watched one",
      [(e["title"], e["season"]) for e in unplayed["order"]] == [("Half", 1)]
      and unplayed["excluded"]["unplayed"] == 1, unplayed["excluded"])

# ── The season-eligibility rule ─────────────────────────────────────────────
# An ended show, seasons 1-3; S2 is the most recently ADDED — the "newest"
# season even though it is not the latest.
def elig_rows():
    return [series("Elig Show", [dict(season(1), added_at=NOW - 700 * DAY),
                                 dict(season(2), added_at=NOW - 2 * DAY),
                                 dict(season(3), added_at=NOW - 600 * DAY)])]
no_mode = {k: v for k, v in CFG.items() if k != "TV_SEASON_ELIGIBILITY"}
oldest = A._tv_season_plan(elig_rows(), no_mode, now=NOW)
check("the DEFAULT is oldest-only: one season per show, the lowest on disk",
      [(e["title"], e["season"]) for e in oldest["order"]] == [("Elig Show", 1)]
      and oldest["excluded"]["season_rule"] == 2, oldest["excluded"])
newest = A._tv_season_plan(elig_rows(), dict(CFG, TV_SEASON_ELIGIBILITY="except_newest"),
                           now=NOW)
check("except-newest holds back the most recently ADDED season — not the latest",
      sorted(e["season"] for e in newest["order"]) == [1, 3]
      and newest["excluded"]["season_rule"] == 1, newest["excluded"])
all_mode = A._tv_season_plan(elig_rows(), CFG, now=NOW)
check("all-seasons mode frees every season of an ended show",
      sorted(e["season"] for e in all_mode["order"]) == [1, 2, 3], all_mode["excluded"])
check("an unknown value reads as the strictest — oldest",
      [e["season"] for e in A._tv_season_plan(
          elig_rows(), dict(CFG, TV_SEASON_ELIGIBILITY="bogus"), now=NOW)["order"]] == [1])
# Specials never count as "the oldest": a show with a Season 0 folder would
# otherwise spend forever on a tiny extras dir while every real season hides.
specials = A._tv_season_plan(
    [series("Extras Show", [season(0, size=1 * GB), season(1), season(2)])],
    dict(CFG, TV_SEASON_ELIGIBILITY="oldest"), now=NOW)
check("Season 0 does not poison oldest-only — S1 is the eligible season",
      [e["season"] for e in specials["order"]] == [1], specials["excluded"])

# ── The order among the rest ────────────────────────────────────────────────
pos = {k: i for i, k in enumerate(key)}
check("a recently-watched season sorts far above an untouched one of the same show",
      pos[("Dead Show", 2)] < pos[("Dead Show", 1)], key)
check("the untouched shows' seasons go first of all",
      {key[0], key[1]} == {("Huge Dead", 1), ("Tiny Dead", 1)}, key)
check("a zero-score tie goes to the LARGER season",
      pos[("Huge Dead", 1)] < pos[("Tiny Dead", 1)], key)
check("scores are ascending through the whole order",
      all(order[i]["score"] <= order[i + 1]["score"] for i in range(len(order) - 1)),
      [e["score"] for e in order])
check("every entry carries what the views show: size, eps, watched, score",
      all(e["size_bytes"] > 0 and "eps" in e and "eps_watched" in e
          and isinstance(e["score"], float) for e in order))

# The series watch bump, end to end: the untouched season of the watched show
# outscores the untouched seasons of the never-watched shows, and turning the
# knob to 0 shrinks exactly that lift.
d2 = next(e for e in order if (e["title"], e["season"]) == ("Dead Show", 2))
huge = next(e for e in order if e["title"] == "Huge Dead")
check("an untouched season of a WATCHED show outscores one of an unwatched show",
      d2["score"] > huge["score"], (d2["score"], huge["score"]))
nobump = A._tv_season_plan(ROWS, dict(CFG, TV_SERIES_WATCH_BUMP=0), now=NOW)
d2_nb = next(e for e in nobump["order"] if (e["title"], e["season"]) == ("Dead Show", 2))
check("the bump knob at 0 removes the series lift", d2_nb["score"] < d2["score"],
      (d2_nb["score"], d2["score"]))

# ── The season episode cap ──────────────────────────────────────────────────
# Not every show splits its run into seasons. Anime that never re-numbers,
# long-running dailies, a library whose folders were flattened: the whole show
# arrives as one season, and deleting "a season" deletes all of it. The cap is
# the one thing that tells those apart from a real season — an episode count no
# real season reaches. Ordinary seasons run to about 26, so the shipped 50 has
# roughly a season of headroom before it could touch one.
def flat_rows(eps):
    return [series("Flat Show", [season(1, eps=eps, size=200 * GB)])]

capped = A._tv_season_plan(flat_rows(400), CFG, now=NOW)
check("a 400-episode season is held back by the shipped cap",
      not capped["order"] and capped["excluded"]["oversized_season"] == 1,
      capped["excluded"])
normal = A._tv_season_plan(flat_rows(24), CFG, now=NOW)
check("...while an ordinary 24-episode season is untouched by it",
      [e["season"] for e in normal["order"]] == [1]
      and normal["excluded"]["oversized_season"] == 0, normal["excluded"])
edge = A._tv_season_plan(flat_rows(50), dict(CFG, TV_MAX_SEASON_EPISODES=50), now=NOW)
check("the cap is a ceiling, not a limit: exactly 50 still deletes",
      [e["season"] for e in edge["order"]] == [1], edge["excluded"])
over = A._tv_season_plan(flat_rows(51), dict(CFG, TV_MAX_SEASON_EPISODES=50), now=NOW)
check("...and 51 does not", not over["order"], over["excluded"])

# Negative control for the default itself: with the cap OFF the same row is
# eligible, so the check above is the cap acting and not some other rung.
off = A._tv_season_plan(flat_rows(400), dict(CFG, TV_MAX_SEASON_EPISODES=0), now=NOW)
check("0 turns the cap off — the same season is then eligible",
      [e["season"] for e in off["order"]] == [1]
      and off["excluded"]["oversized_season"] == 0, off["excluded"])

# A config written before this setting existed still gets the protection. The
# shield only ever holds deletions back, so absent must mean the shipped
# default, not "no cap" — 0 is the off switch and 0 is a value someone chose.
no_key = {k: v for k, v in CFG.items() if k != "TV_MAX_SEASON_EPISODES"}
check("a config with no cap key still gets the shipped default",
      A._tv_season_plan(flat_rows(400), no_key, now=NOW)["excluded"]["oversized_season"] == 1)

# Unknown is not oversized. A server that stops reporting episode counts would
# otherwise shield the entire library at once — the failure mode of a shield
# that fires on missing data.
blank = A._tv_season_plan(
    [series("No Counts", [dict(season(1), eps=0)], s_eps=0)], CFG, now=NOW)
check("a season with no episode count is judged by the other rules, not the cap",
      [e["season"] for e in blank["order"]] == [1]
      and blank["excluded"]["oversized_season"] == 0, blank["excluded"])

# The cap outranks the season-eligibility rule, and that ordering is the point:
# a flattened show IS its own oldest season, so under oldest-only it would
# report "waiting its turn" — a reason that reads as "later", for something
# that must never happen.
both = A._tv_season_plan(flat_rows(400), dict(CFG, TV_SEASON_ELIGIBILITY="oldest"), now=NOW)
check("an oversized season reports its size, not the eligibility rule",
      both["excluded"]["oversized_season"] == 1 and both["excluded"]["season_rule"] == 0,
      both["excluded"])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
