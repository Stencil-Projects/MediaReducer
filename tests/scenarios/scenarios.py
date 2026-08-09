"""The scenario table. One entry per thing that can change the outcome.

Deliberately NOT random. A random sweep covers whatever it happened to draw,
which is the wrong thing to gate a merge on: two runs of the same commit test
different ground, and a failure may not reproduce. Every scenario here is a
fixed dict, so CI runs the same ground every time and a red build names the
exact case.

Shape: one BASELINE that deletes both a movie and a season, then one variant
per factor, each changing a single thing so a failure points at that thing.
`expect` states what the run must do — mostly whether each media type may lose
files — which is what turns "it did not crash" into "it did the right thing".
"""

# Ratings sit either side of 6.0 so a MAX_IMDB_RATING of 6.0 splits the library
# rather than clearing or sparing all of it.
BASE_MOVIES = [
    {"name": "Old Unwatched", "mb": 900, "added": 900, "plays": 0, "imdb": 4.0},
    {"name": "Old Watched", "mb": 800, "added": 900, "plays": 6, "imdb": 5.0},
    {"name": "Acclaimed", "mb": 700, "added": 900, "plays": 1, "imdb": 8.5},
    {"name": "Just Added", "mb": 600, "added": 2, "plays": 0, "imdb": 4.5},
    {"name": "Favourited", "mb": 500, "added": 900, "plays": 0, "imdb": 4.2,
     "favorite": True},
    {"name": "No Rating", "mb": 400, "added": 900, "plays": 0, "imdb": None},
    {"name": "Unmanaged Movie", "folder": "unmanaged", "mb": 900, "added": 900,
     "plays": 0, "imdb": 4.0},
]
BASE_SHOWS = [
    {"name": "Ended Show", "status": "Ended", "imdb": 5.0, "added": 900,
     "seasons": [{"n": 1, "episodes": 6, "mb": 200, "added": 900, "plays": 2},
                 {"n": 2, "episodes": 6, "mb": 200, "added": 800, "plays": 1}]},
    {"name": "Running Show", "status": "Continuing", "imdb": 7.0, "added": 900,
     "seasons": [{"n": 1, "episodes": 5, "mb": 150, "added": 900, "plays": 3},
                 {"n": 2, "episodes": 5, "mb": 150, "added": 100, "plays": 1}]},
    {"name": "Unmanaged Show", "folder": "unmanaged", "status": "Ended",
     "imdb": 5.0, "added": 900,
     "seasons": [{"n": 1, "episodes": 4, "mb": 200, "added": 900, "plays": 1}]},
]


def base(cap_fraction=0.5, **over):
    spec = {"movies": [dict(m) for m in BASE_MOVIES],
            "shows": [dict(s, seasons=[dict(x) for x in s["seasons"]]) for s in BASE_SHOWS],
            "monitor": ["movies", "tv"], "config": {}, "cap_fraction": cap_fraction}
    spec["config"].update(over)
    return spec


# expect: movies / episodes — may this run remove files of that type?
#         "some" = at least one, "none" = not one.
SCENARIOS = [
    ("baseline", base(), {"movies": "some", "episodes": "some", "repeat": True}),

    # ── The two opt-in switches ──────────────────────────────────────────────
    ("movies-off", base(MOVIE_CLEANUP_ENABLED=False), {"movies": "none", "episodes": "some"}),
    ("tv-off", base(TV_CLEANUP_ENABLED=False), {"movies": "some", "episodes": "none"}),
    ("both-off", base(MOVIE_CLEANUP_ENABLED=False, TV_CLEANUP_ENABLED=False),
     {"movies": "none", "episodes": "none", "refused": True}),

    # ── Filters, each on its own ─────────────────────────────────────────────
    ("grace-blocks-all", base(GRACE_PERIOD_DAYS=3650), {"movies": "none", "episodes": "none"}),
    ("imdb-cutoff", base(MAX_IMDB_RATING=6.0), {"movies": "some", "episodes": "some"}),
    ("imdb-cutoff-blocks-all", base(MAX_IMDB_RATING=1.0), {"movies": "none", "episodes": "none"}),
    ("skip-unplayed", base(cap_fraction=0.15, SKIP_UNPLAYED_MOVIES=True),
     {"movies": "some", "episodes": "some"}),
    ("protect-favorites", base(PROTECT_JELLYFIN_FAVORITES=True),
     {"movies": "some", "episodes": "some"}),

    # ── Season rules ─────────────────────────────────────────────────────────
    ("season-oldest-only", base(TV_SEASON_RULE="oldest_only"), {"episodes": "some"}),
    ("season-all-but-newest", base(TV_SEASON_RULE="all_but_newest"), {"episodes": "some"}),
    ("episode-cap-holds-back", base(TV_MAX_SEASON_EPISODES=3),
     {"movies": "some", "episodes": "none"}),
    ("episode-cap-off", base(TV_MAX_SEASON_EPISODES=0), {"episodes": "some"}),

    # ── Scoring dials: the outcome may differ, a crash may not ───────────────
    ("balance-history", base(SCORE_BALANCE=0), {}),
    ("balance-imdb", base(SCORE_BALANCE=100), {}),
    ("near-tie-wide", base(NEAR_TIE_PTS=25), {}),
    ("near-tie-off", base(NEAR_TIE_PTS=None), {}),
    ("tv-weight-max", base(TV_WATCH_WEIGHT=200, TV_SERIES_WATCH_BUMP=25), {}),
    ("staleness-short", base(MAX_STALENESS_MONTHS=1), {}),

    # ── Thresholds ───────────────────────────────────────────────────────────
    ("no-threshold-armed", base(MAX_LIBRARY_GB="__none__"),
     {"movies": "none", "episodes": "none", "refused": True}),
    ("delay-30-manual-cleanup", base(DELETE_DELAY_DAYS=30),
     {"movies": "some", "episodes": "some", "repeat": True}),

    # ── Library shapes ───────────────────────────────────────────────────────
    ("movies-only-library", {"movies": BASE_MOVIES, "shows": [], "monitor": ["movies"],
                             "config": {}}, {"movies": "some", "episodes": "none"}),
    ("tv-only-library", {"movies": [], "shows": BASE_SHOWS, "monitor": ["tv"],
                         "config": {}}, {"movies": "none", "episodes": "some"}),
    ("empty-library", {"movies": [], "shows": [], "monitor": ["movies"], "config": {}},
     {"movies": "none", "episodes": "none", "refused": True}),
    ("nothing-monitored", base(), {"movies": "none", "episodes": "none", "refused": True}),
    ("all-media-out-of-scope",
     {"movies": [dict(BASE_MOVIES[0], folder="unmanaged")],
      "shows": [dict(BASE_SHOWS[2])], "monitor": ["movies", "tv"], "config": {}},
     {"movies": "none", "episodes": "none", "refused": True}),

    # ── Awkward but legal names, and the layouts that break naive path code ──
    ("hostile-names",
     {"movies": [{"name": "Quote's & <Bracket> [2020]", "mb": 900, "added": 900,
                  "plays": 0, "imdb": 4.0},
                 {"name": "Шоу 中文 \U0001F3AC", "mb": 800, "added": 900,
                  "plays": 0, "imdb": 4.1},
                 {"name": "A" * 90, "mb": 700, "added": 900, "plays": 0, "imdb": 4.2}],
      "shows": [{"name": "Show's [Odd] \U0001F3AC", "status": "Ended", "imdb": 5.0,
                 "added": 900,
                 "seasons": [{"n": 1, "episodes": 4, "mb": 200, "added": 900, "plays": 1},
                             {"n": 2, "episodes": 4, "mb": 200, "added": 900, "plays": 1}]}],
      "monitor": ["movies", "tv"], "config": {}, "cap_fraction": 0.15},
     {"movies": "some", "episodes": "some"}),
    ("flattened-mega-season",
     {"movies": [], "shows": [{"name": "Daily Show", "status": "Ended", "imdb": 5.0,
                               "added": 900,
                               "seasons": [{"n": 1, "episodes": 120, "mb": 50,
                                            "added": 900, "plays": 2}]}],
      "monitor": ["tv"], "config": {"TV_MAX_SEASON_EPISODES": 50}},
     {"episodes": "none"}),
    ("same-name-under-two-monitored-dirs",
     {"movies": [], "shows": [{"name": "Twin Show", "folder": "tv", "status": "Ended",
                               "imdb": 5.0, "added": 900,
                               "seasons": [{"n": 1, "episodes": 4, "mb": 300,
                                            "added": 900, "plays": 1}]}],
      "monitor": ["tv", "tv2"], "config": {}, "twin": ("tv", "tv2", "Twin Show")},
     {"episodes": "none"}),
]

# nothing-monitored is the baseline with its allow-list emptied, which the spec
# format cannot express through `base()`.
for _i, (_n, _s, _e) in enumerate(SCENARIOS):
    if _n == "nothing-monitored":
        _s["monitor"] = []
        _s["config"]["MAX_LIBRARY_GB"] = None
