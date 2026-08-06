"""Every retention-scoring curve number in one place.

Both sides of the app read this file: the engine (engine.py,
compute_retention_score / imdb_vote_confidence) scores real runs with it, and
the web app (app.py) injects it into the Filtering & Scoring page so the
JavaScript preview uses the exact same numbers. Tweak a value here and both
stay in lockstep. The curve SHAPES still live in the two mirror functions,
but the tunables live only here.

Both score sides are normalized 0–100 before the balance dial blends them,
so a full-marks history side (USAGE + best RECENCY tier + MULTI_USER cap)
should sum to 100, and the IMDb side is rating × 10 × confidence.
"""

SCORING = {
    # ── Watch history side (sums to 100 at full marks) ──────────────────────
    # Play frequency: log curve worth USAGE_MAX_PTS, saturating at
    # USAGE_FULL_PLAYS plays.
    "USAGE_MAX_PTS": 45.0,
    "USAGE_FULL_PLAYS": 12,
    # Recency of the last watch, or for a never-watched movie how recently
    # it was ADDED. [max_days, points] tiers, first match wins; past the last
    # tier a movie is "fully stale" and earns 0. The default fades over ~3
    # years, so a movie still gets some credit up to that point (e.g. a
    # 6-month-old add is not judged as harshly as a 3-year-old one). Widen or
    # shrink the window by editing the last tier's day count.
    "RECENCY_TIERS": [
        [30, 35.0],     # ≤ 1 month
        [90, 22.0],     # ≤ 3 months
        [180, 16.0],    # ≤ 6 months
        [365, 10.0],    # ≤ 1 year
        [730, 5.0],     # ≤ 2 years
        [1095, 2.0],    # ≤ 3 years  (older than this → fully stale, 0)
    ],
    # The tiers above are authored for a 3-year (36-month) staleness window.
    # The "Max staleness" setting scales every tier's day threshold by
    # (configured months / this), so the same curve shape fades to 0 over the
    # chosen window. This is the reference the scaling divides by, NOT a cap.
    "RECENCY_DEFAULT_MONTHS": 36,
    # Distinct users who watched: points per user, capped.
    "MULTI_USER_PTS": 10.0,
    "MULTI_USER_MAX_PTS": 20.0,
    # Distinct watchers ALSO slow the age decay: each unique user who watched the
    # movie stretches the staleness window (the recency tiers and the shelf tail),
    # so a widely-watched movie's age score fades slower than a one-person or
    # never-watched one. Effective window = base window x (1 + USER_DECAY_PER_USER
    # x users), capped at USER_DECAY_MAX_MULT. 0 users leaves decay unchanged.
    "USER_DECAY_PER_USER": 0.25,
    "USER_DECAY_MAX_MULT": 2.0,

    # ── IMDb side (rating × 10 × vote confidence, capped at 100) ────────────
    # Vote-count confidence in the rating: log10 ramp from the floor to 1.0
    # at 10^VOTE_CONF_FULL_LOG10 votes (6.0 = one million votes). A missing
    # vote count gets the medium-low UNKNOWN value. Absence of data is not
    # evidence of a tiny film.
    "VOTE_CONF_FLOOR": 0.25,
    "VOTE_CONF_UNKNOWN": 0.4,
    "VOTE_CONF_FULL_LOG10": 6.0,

    # ── Added-date soft shelf (blended region only) ─────────────────────────
    # Past max staleness the recency tiers give 0, a hard cliff at 100% watch
    # history (unwatched-and-stale movies all tie at 0). Once IMDb is blended in,
    # a gentle shelf keeps a date-added-age gradient alive past the cliff so a
    # newer-added-but-stale movie ranks a little above an older one. It is scored
    # from the ADDED date only, and mostly matters for never-played movies (which
    # otherwise have nothing past the cliff). Its weight is a TENT: zero at 100%
    # watch history AND at 100% IMDb, peaking at the SHELF_RAMP_FULL_Q blend, so
    # 100% history stays a hard cliff and 100% IMDb stays pure quality.
    # Value continues the last recency tier: SHELF_MAX_PTS at the cliff edge,
    # fading linearly to 0 SHELF_SPAN_MULT staleness-windows past it.
    "SHELF_MAX_PTS": 2.0,       # shelf value right at the staleness cliff (matches the last recency tier)
    "SHELF_SPAN_MULT": 1.0,     # fade to 0 this many staleness-windows past the cliff (1.0 => gone by 2x the window)
    "SHELF_RAMP_FULL_Q": 0.5,   # IMDb fraction (SCORE_BALANCE/100) at which the shelf reaches full strength
}


# A fingerprint of the curve VALUES above (not this file's bytes, so comment and
# docstring edits are free). Both sides stamp/compare it as part of the deletion
# plan: the persisted queue carries the scores these curves produced, and the
# Redline fast path deletes worst-first by that stored score without re-scoring.
# Changing a number here therefore invalidates the plan: Cleanup and arming
# ghost until a fresh Simulate, and the fast path falls back to a full scan that
# re-scores, while leaving the expensive metadata cache intact (unlike the
# engine's code_checksum, which wipes it).
import hashlib as _hashlib
import json as _json
import math as _math

FINGERPRINT = _hashlib.sha256(
    _json.dumps(SCORING, sort_keys=True, default=str).encode("utf-8")
).hexdigest()[:16]


def _vote_confidence(votes) -> float:
    """Vote-count confidence in an IMDb rating — ONE implementation for the
    movie and season curves (and mirrored by the page JS): a MISSING count is
    the medium-low UNKNOWN (absence of data is not evidence of a tiny film),
    a counted zero is the floor, and a real count ramps log10 to 1.0 at
    10^VOTE_CONF_FULL_LOG10 votes."""
    if votes is None:
        return SCORING["VOTE_CONF_UNKNOWN"]
    try:
        v = int(votes)
    except (TypeError, ValueError):
        return SCORING["VOTE_CONF_UNKNOWN"]
    if v <= 0:
        return SCORING["VOTE_CONF_FLOOR"]
    frac = min(1.0, _math.log10(max(v, 1)) / SCORING["VOTE_CONF_FULL_LOG10"])
    return SCORING["VOTE_CONF_FLOOR"] + (1.0 - SCORING["VOTE_CONF_FLOOR"]) * frac


# ── The shared curve terms ───────────────────────────────────────────────────
# Every retention score — movie or season, Python or the page's JS mirror —
# is assembled from these four terms. The two scorers below differ ONLY in
# what they feed in (a movie's own plays vs a season's play-equivalents; a
# movie's watchers vs a season's) and in how the season clamps its enriched
# history side; the curves themselves exist once.

def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def usage_pts(plays) -> float:
    """The play-frequency log curve: worth USAGE_MAX_PTS, saturating at
    USAGE_FULL_PLAYS plays. Fractional plays (a season's movie-watch
    equivalents) ride the same curve."""
    p = max(_num(plays), 0.0)
    return (SCORING["USAGE_MAX_PTS"]
            * min(1.0, _math.log1p(p) / _math.log1p(SCORING["USAGE_FULL_PLAYS"])))


def multi_user_pts(users) -> float:
    """Distinct watchers: points per user, capped."""
    u = max(int(_num(users)), 0)
    return min(SCORING["MULTI_USER_PTS"] * u, SCORING["MULTI_USER_MAX_PTS"])


def recency_shelf_pts(recency_at, users, max_staleness_months, now):
    """(recency tier points, soft-shelf points) for a last-watch/added epoch.

    The tier day-thresholds scale to the Max staleness setting, and distinct
    watchers stretch the decay (recency AND shelf fade slower on a
    widely-watched title). The shelf continues the curve past the staleness
    cliff; its weighting (the blend tent) is each scorer's business."""
    users = max(int(_num(users)), 0)
    stale_scale = _num(max_staleness_months) / SCORING["RECENCY_DEFAULT_MONTHS"]
    decay_mult = min(SCORING["USER_DECAY_MAX_MULT"],
                     1.0 + SCORING["USER_DECAY_PER_USER"] * users)
    eff_scale = stale_scale * decay_mult
    rec_pts = 0.0
    shelf_pts = 0.0
    recency_at = int(_num(recency_at))
    if recency_at > 0:
        days_since = (now - recency_at) / 86400.0
        for max_days, pts in SCORING["RECENCY_TIERS"]:
            if days_since <= max_days * eff_scale:
                rec_pts = pts
                break
        cliff_days = SCORING["RECENCY_TIERS"][-1][0] * eff_scale
        span_days = cliff_days * SCORING["SHELF_SPAN_MULT"]
        if days_since > cliff_days and span_days > 0:
            frac = 1.0 - (days_since - cliff_days) / span_days
            shelf_pts = SCORING["SHELF_MAX_PTS"] * max(0.0, min(1.0, frac))
    return rec_pts, shelf_pts


def imdb_pts(rating, votes) -> float:
    """The quality side: rating × 10 × vote confidence, capped at 100.
    A missing rating is 0 — eligibility rules decide what THAT means."""
    if rating is None:
        return 0.0
    return min(_num(rating) * 10.0 * _vote_confidence(votes), 100.0)


def shelf_ramp(quality_weight) -> float:
    """The blend tent: 0 shelf at 100% watch history, full strength from the
    SHELF_RAMP_FULL_Q blend upward."""
    full_q = SCORING["SHELF_RAMP_FULL_Q"]
    return min(1.0, quality_weight / full_q) if full_q > 0 else 1.0


def movie_retention_score(rec: dict, *, history_weight: float,
                          quality_weight: float, max_staleness_months: float,
                          now: float):
    """RetentionScore for a normalized movie record — HIGHER = keep. The
    engine's compute_retention_score delegates here, so the movie and season
    curves live in one module and cannot drift.

    Returns (score, breakdown); breakdown values are already weighted by the
    balance weights (which sum to 1.0), so they sum to the 0–100 score.
    Record fields read: total_play_count, last_played_at, added_at,
    distinct_users_watched, imdb_rating, imdb_num_votes."""
    b = {}
    plays = max(int(_num(rec.get("total_play_count"))), 0)
    b["usage"] = usage_pts(plays) * history_weight

    users = max(int(_num(rec.get("distinct_users_watched"))), 0)
    # Recency reads the last watch, falling back to the added date: a
    # recently-added-but-unwatched movie still reads "fresh". Only recency
    # benefits; frequency and users stay 0 for a never-watched movie.
    last_played = int(_num(rec.get("last_played_at")))
    recency_at = last_played if last_played > 0 else int(_num(rec.get("added_at")))
    rec_pts, shelf_pts = recency_shelf_pts(recency_at, users,
                                           max_staleness_months, now)
    b["recency"] = rec_pts * history_weight
    b["multi_user"] = multi_user_pts(users) * history_weight
    b["imdb"] = imdb_pts(rec.get("imdb_rating"), rec.get("imdb_num_votes")) * quality_weight
    b["shelf"] = shelf_pts * history_weight * shelf_ramp(quality_weight)
    return sum(b.values()), b


def season_retention_score(season: dict, series: dict, *,
                           history_weight: float, quality_weight: float,
                           max_staleness_months: float,
                           series_watch_bump: float, now: float,
                           watch_weight: float = 1.0):
    """RetentionScore for one TV SEASON on the movie 0-100 scale — HIGHER =
    keep. The season is the deletion unit, so seasons and movies sort into ONE
    deletion order by this number and compute_retention_score's.

    Same curve family as the movie score, at season grain:

      usage       the MOVIE play curve on the season's play count expressed
                  as movie-watch equivalents: plays ÷ episodes × the watch
                  weight. Watching a whole 12-episode season once (12 plays)
                  equals ONE movie watch at the default weight of 1.0; 6
                  plays of it weigh half a watch. The TV watch weight knob
                  (1.0-2.0) sets what a full season-watch is worth — at 2.0
                  those 12 plays count like two movie watches.
      recency     the movie RECENCY_TIERS + soft shelf on this season's last
                  watch, falling back to the SEASON's own added date (its
                  newest episode file), then the series added date — a fresh
                  season of an old show reads fresh, an old show's untouched
                  seasons read stale. Distinct-watcher decay stretch applies,
                  from THIS season's watcher count — exactly as a movie's own
                  watchers stretch its decay.
      multi-user  the movie curve on THIS season's distinct watchers — a
                  unique user counts for a season what they count for a
                  movie.
      series bump every watched episode of the show lifts EVERY season a
                  little: series_watch_bump × a log curve of the show's
                  watched-episode count against its total, so one watched
                  episode is a subtle-but-real nudge (interest in the show)
                  and the lift grows toward the full knob as more of the
                  show is consumed — the untouched middle seasons of a
                  loved show outrank the seasons of a show nobody has
                  touched. The knob (TV_SERIES_WATCH_BUMP) sets the points
                  at a fully-watched series; 0 turns the lift off.
      quality     the movie IMDb side on the SERIES rating/votes.

    Rows written before seasons carried their own plays/users fall back to
    the nearest older fact (watched-episode count; the series' users) rather
    than to zero. The history side is clamped to 100 before weighting so the
    bump enriches the blend without pushing seasons onto a different scale
    than movies. Returns (score, breakdown); breakdown values are already
    weighted.
    """
    b = {}
    eps = max(_num(season.get("eps")), 0.0)
    watched = max(_num(season.get("eps_watched")), 0.0)
    plays = _num(season.get("plays"), -1.0)
    if plays < 0:
        plays = watched   # pre-plays rows: each watched episode was ≥1 play
    eff_plays = (plays / eps) * max(0.0, float(watch_weight)) if eps > 0 else 0.0
    usage = usage_pts(eff_plays)

    season_users = season.get("users")
    users = max(int(_num(season_users if season_users is not None
                         else series.get("users"))), 0)
    last_played = int(_num(season.get("last_played")))
    recency_at = (last_played if last_played > 0
                  else int(_num(season.get("added_at")))
                  or int(_num(series.get("added_at"))))
    rec_pts, shelf_pts = recency_shelf_pts(recency_at, users,
                                           max_staleness_months, now)

    series_eps = max(_num(series.get("eps")), 0.0)
    series_watched = max(_num(series.get("eps_watched")), 0.0)
    # Log curve, not a linear fraction: one watched episode of a big show must
    # read as a visible nudge, not as watched/total ≈ zero.
    series_frac = (min(1.0, _math.log1p(series_watched) / _math.log1p(series_eps))
                   if series_eps > 1 else (1.0 if series_watched > 0 else 0.0))
    bump = max(0.0, float(series_watch_bump)) * series_frac

    history_raw = min(100.0, usage + rec_pts + multi_user_pts(users) + bump)
    b["history"] = history_raw * history_weight
    b["imdb"] = imdb_pts(series.get("rating"), series.get("votes")) * quality_weight
    b["shelf"] = shelf_pts * history_weight * shelf_ramp(quality_weight)
    return sum(b.values()), b
