"""season_retention_score — one TV season on the movie 0-100 scale.

The recipe pinned here is the product spec: a season's plays convert to
movie-watch equivalents (plays ÷ episodes × the watch weight) and run
through the MOVIE play curve, so playing a whole season once counts like one
movie watch at the default weight and like two at 200%; a season's own
distinct users count exactly what a movie's do; a season someone watched
scores above the untouched seasons of the same show; the untouched seasons
of a watched show still earn a boost over the seasons of a show nobody
touches; the boost is the TV_SERIES_WATCH_BUMP knob, scaled by how much of
the show is watched, and 0 turns it off exactly; the IMDb side reads the
series rating through the same balance dial movies use; and the history side
clamps at 100 so a boosted season can never leave the movie scale. Rows from
before seasons carried plays/users fall back to watched-episode count and
the series' users, never to zero."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scoring_constants import SCORING, season_retention_score

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

NOW = 1_700_000_000
DAY = 86400

def score(season, series, *, hw=1.0, qw=0.0, bump=10.0, stale=36, weight=1.0):
    s, _ = season_retention_score(season, series, history_weight=hw,
                                  quality_weight=qw, max_staleness_months=stale,
                                  series_watch_bump=bump, watch_weight=weight,
                                  now=NOW)
    return s

def movie_usage(plays):
    """The movie play curve's usage points — what a season's watch-equivalents
    must earn to sit on the same scale."""
    return (SCORING["USAGE_MAX_PTS"]
            * min(1.0, math.log1p(plays) / math.log1p(SCORING["USAGE_FULL_PLAYS"])))

# One show, five seasons; S1 and S5 watched, S2-4 untouched. The series is
# half consumed. Scored at 100% watch history so the numbers are pure recipe.
WATCHED_SHOW = {"eps": 50, "eps_watched": 20, "users": 1,
                "added_at": NOW - 800 * DAY, "rating": None, "votes": 0}
s_watched = {"eps": 10, "eps_watched": 10, "plays": 10, "users": 1,
             "last_played": NOW - 100 * DAY}
s_untouched = {"eps": 10, "eps_watched": 0, "plays": 0, "users": 0, "last_played": 0}

NOBODY_SHOW = {"eps": 50, "eps_watched": 0, "users": 0,
               "added_at": NOW - 800 * DAY, "rating": None, "votes": 0}

w = score(s_watched, WATCHED_SHOW)
u = score(s_untouched, WATCHED_SHOW)
n = score(s_untouched, NOBODY_SHOW)
check("a watched season outscores its show's untouched seasons", w > u, (w, u))
check("an untouched season of a WATCHED show outscores one of an unwatched show",
      u > n, (u, n))

# ── The watch weight: plays are movie-watch equivalents ─────────────────────
# Identical seasons except plays: 12/12 plays = ONE movie watch, 6/12 = half.
# Same recency and users, so the score deltas are pure usage-curve deltas.
full = {"eps": 12, "eps_watched": 12, "plays": 12, "users": 0,
        "last_played": NOW - 100 * DAY}
half = {"eps": 12, "eps_watched": 6, "plays": 6, "users": 0,
        "last_played": NOW - 100 * DAY}
sf, sh = score(full, NOBODY_SHOW, bump=0), score(half, NOBODY_SHOW, bump=0)
check("a fully-played season earns exactly a ONE-movie-watch of usage",
      abs((sf - sh) - (movie_usage(1.0) - movie_usage(0.5))) < 1e-9, (sf, sh))
sf2 = score(full, NOBODY_SHOW, bump=0, weight=2.0)
check("at 200% weight the same season counts like TWO movie watches",
      abs((sf2 - sf) - (movie_usage(2.0) - movie_usage(1.0))) < 1e-9, (sf2, sf))
check("more weight never lowers a played season's score", sf2 > sf, (sf2, sf))

# ── Season users count what a movie's users count ───────────────────────────
# Recent watch (top recency tier regardless of the decay stretch), so adding
# two distinct season watchers moves the score by exactly the movie
# multi-user points.
lone = {"eps": 10, "eps_watched": 5, "plays": 5, "users": 0,
        "last_played": NOW - 5 * DAY}
duo = dict(lone, users=2)
delta = score(duo, NOBODY_SHOW, bump=0) - score(lone, NOBODY_SHOW, bump=0)
check("two season watchers add the movie MULTI_USER points exactly",
      abs(delta - SCORING["MULTI_USER_PTS"] * 2) < 1e-9, delta)

# ── A season's OWN added date is its never-watched freshness ────────────────
# The fallback chain is last watch → season added → series added: a fresh
# season of an old show reads fresh (top recency tier), not stale.
fresh_season = {"eps": 10, "eps_watched": 0, "plays": 0, "users": 0,
                "last_played": 0, "added_at": NOW - 5 * DAY}
old_season = {"eps": 10, "eps_watched": 0, "plays": 0, "users": 0,
              "last_played": 0, "added_at": NOW - 800 * DAY}
OLD_SHOW = {"eps": 50, "eps_watched": 0, "users": 0,
            "added_at": NOW - 3000 * DAY, "rating": None, "votes": 0}
check("a fresh season of an OLD show reads fresh, not the show's age",
      score(fresh_season, OLD_SHOW) > score(old_season, OLD_SHOW),
      (score(fresh_season, OLD_SHOW), score(old_season, OLD_SHOW)))
no_date = {"eps": 10, "eps_watched": 0, "plays": 0, "users": 0, "last_played": 0}
check("a season with no date of its own falls back to the series added date",
      abs(score(no_date, OLD_SHOW)
          - score(dict(no_date, added_at=NOW - 3000 * DAY), OLD_SHOW)) < 1e-9)

# ── Pre-plays rows fall back, never to zero ─────────────────────────────────
legacy = {"eps": 10, "eps_watched": 10, "last_played": NOW - 100 * DAY}
modern = {"eps": 10, "eps_watched": 10, "plays": 10, "users": 1,
          "last_played": NOW - 100 * DAY}
check("a row without season plays scores as plays = watched episodes",
      abs(score(legacy, WATCHED_SHOW) - score(modern, WATCHED_SHOW)) < 1e-9)

# The boost is knob x a LOG curve of the show's watched episodes against its
# total — one watched episode is a visible nudge, not watched/total ≈ zero.
u0 = score(s_untouched, WATCHED_SHOW, bump=0)
check("boost 0 removes the series lift exactly",
      abs((u - u0) - 10.0 * (math.log1p(20) / math.log1p(50))) < 1e-9, (u, u0))
u25 = score(s_untouched, WATCHED_SHOW, bump=25)
check("a bigger knob lifts more", u25 > u, (u25, u))
ONE_EP = dict(WATCHED_SHOW, eps_watched=1)
one = score(s_untouched, ONE_EP)
check("ONE watched episode already nudges every season of the show",
      one - u0 > 0.1 * 10.0, one - u0)
check("...subtly — well below a half-consumed show's lift", one < u, (one, u))
check("...and the lift keeps growing with more episodes watched",
      score(s_untouched, dict(WATCHED_SHOW, eps_watched=40)) > u)

# The IMDb side reads the SERIES rating through the balance dial, movie-style.
RATED = dict(WATCHED_SHOW, rating=8.5, votes=500_000)
LOW = dict(WATCHED_SHOW, rating=4.0, votes=500_000)
hi = score(s_untouched, RATED, hw=0.3, qw=0.7)
lo = score(s_untouched, LOW, hw=0.3, qw=0.7)
check("a better-rated show's seasons score higher when IMDb has weight", hi > lo, (hi, lo))
check("at 100% watch history the rating changes nothing",
      abs(score(s_untouched, RATED) - score(s_untouched, WATCHED_SHOW)) < 1e-9)

# The history side clamps at 100: a just-watched season of a fully-watched
# multi-user show cannot leave the movie scale.
MAXED = {"eps": 10, "eps_watched": 10, "plays": 40, "users": 4,
         "last_played": NOW - 1 * DAY}
BIG = {"eps": 50, "eps_watched": 50, "users": 4,
       "added_at": NOW - 10 * DAY, "rating": None, "votes": 0}
check("scores never exceed 100", score(MAXED, BIG, bump=25, weight=2.0) <= 100.0,
      score(MAXED, BIG, bump=25, weight=2.0))

# Degenerate inputs stay sane: no episodes, no facts, nothing to divide by.
z = score({"eps": 0, "eps_watched": 0, "last_played": 0},
          {"eps": 0, "eps_watched": 0, "users": 0, "added_at": 0,
           "rating": None, "votes": 0})
check("a fact-free season scores 0, not an error", z == 0.0, z)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
