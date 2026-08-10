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

def base(cap_fraction=0.5, redline_x_free=None, run_time_ahead=False, **over):
    spec = {"movies": [dict(m) for m in BASE_MOVIES],
            "shows": [dict(s, seasons=[dict(x) for x in s["seasons"]]) for s in BASE_SHOWS],
            "monitor": ["movies", "tv"], "config": {}, "cap_fraction": cap_fraction,
            "redline_x_free": redline_x_free,
            "run_time_ahead": run_time_ahead}
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
    # One Ended show, three seasons, TV only, and a deficit (half the library)
    # that WANTS more than the rule allows — the rule is only visible under
    # pressure. S3 is both the newest-added and big enough that the target
    # cannot be met without it, so a rule that leaks deletes it.
    #
    # These once posed a key that does not exist (TV_SEASON_RULE, with invented
    # values) and asserted only "some episodes" — which the default rule also
    # satisfies, so both scenarios passed while testing nothing. The config key
    # is TV_SEASON_ELIGIBILITY: oldest / except_newest / all, and the
    # expectations now name the seasons each rule must and must not touch.
    ("season-oldest-only",
     {"movies": [], "monitor": ["tv"], "config": {"TV_SEASON_ELIGIBILITY": "oldest"},
      "shows": [{"name": "Rule Show", "status": "Ended", "imdb": 4.0, "added": 900,
                 "seasons": [{"n": 1, "episodes": 4, "mb": 150, "added": 900, "plays": 0},
                             {"n": 2, "episodes": 4, "mb": 150, "added": 500, "plays": 0},
                             {"n": 3, "episodes": 4, "mb": 600, "added": 100, "plays": 0}]}]},
     {"episodes": "some", "gone_has": ["/Season 1/"],
      "gone_not": ["/Season 2/", "/Season 3/"]}),
    ("season-all-but-newest",
     {"movies": [], "monitor": ["tv"], "config": {"TV_SEASON_ELIGIBILITY": "except_newest"},
      "shows": [{"name": "Rule Show", "status": "Ended", "imdb": 4.0, "added": 900,
                 "seasons": [{"n": 1, "episodes": 4, "mb": 150, "added": 900, "plays": 0},
                             {"n": 2, "episodes": 4, "mb": 150, "added": 500, "plays": 0},
                             {"n": 3, "episodes": 4, "mb": 600, "added": 100, "plays": 0}]}]},
     {"episodes": "some", "gone_has": ["/Season 1/", "/Season 2/"],
      "gone_not": ["/Season 3/"]}),
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
    # The delay as it actually protects you. Every cleanup above is fired from
    # the API and is therefore MANUAL, and manual ignores the delay by design —
    # so until this landed, no scenario ever reached the mark-and-wait path and
    # the engine never logged "MARKED for deletion" once in a full sweep.
    # A scheduled run with a delay set must mark and delete NOTHING today.
    ("delay-30-scheduled-marks-only", base(DELETE_DELAY_DAYS=30),
     {"movies": "none", "episodes": "none", "scheduled": True, "marks": True}),
    # Marking is only half of it. A mark that has aged past its delay has to be
    # collected on the next scheduled run, or the delay is not a delay — it is
    # a library that is never cleaned.
    ("delay-marks-age-out-and-delete", base(DELETE_DELAY_DAYS=7),
     {"movies": "some", "scheduled": True, "marks": True, "marks_due": True}),
    # Under every limit, so a scheduled run must decline — and the two ways it
    # declines are different code with different wording. Neither was reached
    # by anything: a run that wrongly PROCEEDS here deletes from a library that
    # was never over its cap.
    ("under-cap-scheduled-nothing-to-do", base(cap_fraction=3.0),
     {"movies": "none", "episodes": "none", "scheduled": True,
      "log_has": "within all space limits. Nothing to do today."}),
    # Same, but the day's window has already been spent. A different branch,
    # and the one that must NOT claim the window was used up by this tick.
    ("day-used-scheduled-nothing-to-do", base(cap_fraction=3.0),
     {"movies": "none", "episodes": "none", "scheduled": True, "day_used": True,
      "log_has": "within all space limits. Nothing to do."}),
    # The same "nothing is over its limits" answer on the MANUAL side. The app
    # refuses this before the engine starts, so the engine's own check only
    # matters when the two disagree — which is the only time it matters at all.
    ("under-cap-manual-nothing-to-do", base(cap_fraction=3.0),
     {"movies": "none", "episodes": "none", "direct_manual": True,
      "log_has": "within all space limits. Nothing to do."}),
    # A scheduled run that arrives BEFORE the daily run time must wait, even
    # with a breach in front of it. Nothing exercised that refusal, and a run
    # firing early is a deletion nobody scheduled.
    ("scheduled-before-run-time", base(run_time_ahead=True),
     {"movies": "none", "episodes": "none", "scheduled": True, "waits": True}),
    # Stopping. Every scenario above arms a cap at half the library, which
    # makes the target BIGGER than everything the filters leave eligible — so
    # the run takes the whole pool and the rule that stops it never fires.
    # Deleting the stop condition outright therefore changed nothing anywhere,
    # and the engine could empty a library instead of freeing a few GB with the
    # entire table still green. These two arm a shallow cap so the target is a
    # fraction of the pool, and the run has to stop with movies still standing.
    # TV is off so the season side does not claim a share of the deficit and
    # the movie target is the whole of it.
    ("marks-stop-at-target",
     base(cap_fraction=0.9, DELETE_DELAY_DAYS=30, TV_CLEANUP_ENABLED=False),
     {"movies": "none", "episodes": "none", "scheduled": True, "marks": True}),
    # The same ceiling one run later, on the deletions the marks become.
    ("deletes-stop-at-target",
     base(cap_fraction=0.9, TV_CLEANUP_ENABLED=False),
     {"movies": "some", "episodes": "none", "scheduled": True, "marks": True,
      "marks_due": True}),

    # The safety floor, end to end. A cap below it would keep deleting until
    # the library fits inside less than MAX_HEADROOM_PCT% of what is there —
    # the one setting that can clear most of a library while working exactly as
    # configured. Every other scenario runs the percentage at 100, which puts
    # the floor at zero and no cap below it, so the refusal was never reached
    # by anything. Run directly as a manual cleanup because the app blocks this
    # state before the engine starts, and the engine's own check is the second
    # line of defence for a library that grew after the cap was set.
    ("cap-below-safety-floor-refuses",
     base(cap_fraction=0.3, MAX_HEADROOM_PCT=15),
     {"movies": "none", "episodes": "none", "direct_manual": True,
      "log_has": "ABORT: Library Size Cap"}),

    # Redline is the "space is critical, do not wait" override: it deletes
    # immediately, delay or no delay. Armed above the free space that actually
    # exists, so it fires on any machine.
    ("redline-deletes-immediately",
     base(DELETE_DELAY_DAYS=30, redline_x_free=2.0),
     {"movies": "some", "scheduled": True}),

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
